## What these numbers are, and what they are not

`agenthive_tokens_avoided_total` counts **tokens not sent**. It is not
money, and the docs used to let the two slide into each other.

Three reasons the gap matters, and they do not all point the same way:

1. **Tokens are estimated** at four characters each. Close enough for a
   trend, wrong for an invoice.
2. **Cached re-reads cost a fraction of a fresh read.** Providers charge
   roughly a tenth for a cache hit and a premium for the write.
3. **Retrieved content is paid for on every turn after.** It sits in the
   prompt for the rest of the session, so bytes you never send are bytes
   you never re-read.

Point 3 is the big one, and it means this metric almost certainly
**understates** the saving. A 2,300-token neighbourhood in a twenty-turn
session is one cache write plus nineteen cache reads, not one payment.
`agenthive_session_turns` is recorded so that multiplier is measurable
rather than assumed.

### Working out what it actually cost you

The server does not know your provider's prices and should not guess, so
it exports the raw counts and leaves the arithmetic here. Substitute
your own rates:

```promql
# cost-weighted tokens avoided, in units of fresh input tokens
#   $cache_read  ~ 0.1   (a cached re-read, as a fraction of fresh)
#   $cache_write ~ 1.25  (the premium on first send)
#   $turns       = median session length, from agenthive_session_turns
(
  increase(agenthive_tokens_avoided_total[7d])
  * ($cache_write + ($turns - 1) * $cache_read)
)
```

With `cache_read=0.1`, `cache_write=1.25` and a median of 12 turns, a
token not sent is worth about 2.35 fresh tokens — so the headline number
is roughly a third of the real saving.

Do not publish a currency figure from this without checking it against a
provider invoice at least once. An estimate derived from four-characters-
per-token and a median turn count is a direction of travel, not an
accounting record.

# Observability

AgentHive's whole premise is a bet: a curated, bounded, shared memory
graph sends fewer tokens to the model than re-deriving context every
session would, and it gets reused across teammates instead of every
agent rebuilding the same picture alone. This directory is what makes
that bet checkable instead of asserted.

## What's actually measured, and what isn't

Be precise about what these metrics claim, because it's easy to
overclaim here:

- **AgentHive never calls an LLM.** It has no idea what your agents'
  actual token usage or bill looks like. What it measures is the gap
  between "the whole team's approved memory" and "the bounded
  neighborhood this one retrieval returned" (`approx_tokens_full_team`
  vs `approx_tokens`, both from `retrieval.py`, counted as chars/4 - a
  rough approximation, not a real tokenizer). `agenthive_tokens_avoided_total`
  is the running sum of that gap **for retrievals that returned at
  least one note**. An empty result increments
  `agenthive_retrieval_empty_total` and adds nothing to tokens avoided:
  nothing was served, so nothing was spared. Read the avoided counter as
  "tokens NOT sent because retrieval was scoped instead of dumped," not
  as "tokens saved on your OpenAI/Anthropic bill" - the second claim
  would need this service to see your actual model calls, which it
  deliberately doesn't (see ADR.md's "Why not build an LLM-request proxy").
- **The retrieval cache reduces graph-traversal work and DB load, not
  model calls.** AgentHive doesn't sit in front of inference, so it has
  no calls to a model to cache or reduce. What the cache demonstrably
  does: when several agents ask for the same anchor around the same
  time (the whole team looking at the same incident), the second and
  third requests are served from cache instead of re-walking the graph.
  `agenthive_cross_user_cache_hits_total` - a cache hit served to a
  *different* user than the one who populated it - is the honest,
  countable version of "the team is sharing memory instead of each
  agent rebuilding its own copy."
- **Graph growth and reuse are read straight from the database** at
  scrape time (`agenthive_memory_nodes`, `agenthive_active_users`,
  `agenthive_teams` - see `metrics.py`'s `DbStatsCollector` and
  `db.py`'s `global_*` methods). No label carries a team_id or user_id
  on these, deliberately - unbounded label cardinality is how a metrics
  endpoint becomes its own incident. Per-team breakdowns (top reused
  nodes, retrieval count, active users for *that* team) live in
  `GET /teams/{id}/metrics/summary` and the review UI's Metrics tab
  instead.

## Wiring it up

1. Point Prometheus at `GET /metrics` on the service (plain text
   exposition format, via `prometheus_client`). In Kubernetes, the Helm
   chart does this two ways - see `helm/agenthive/README.md`'s "Metrics"
   section.
2. Import `grafana-dashboard.json` into Grafana (Dashboards -> Import ->
   Upload JSON), point it at your Prometheus data source when prompted.
3. Give it real usage. Every panel needs actual retrieve_context /
   log_session traffic to show anything - a fresh install's dashboard is
   correctly empty.

## Reading the dashboard

- **Tokens avoided (cumulative)** and **tokens served vs. avoided**: the
  headline pair. If "avoided" keeps climbing as the graph grows, bounded
  retrieval is doing its job - the alternative (no bound, dump
  everything approved) grows without limit as the team accumulates
  memory.
- **Average retrieval reduction %**: per-call average of
  `reduction_pct` from `retrieval.py`, **only when a note was returned**.
  Empty retrievals are left out. A number near 0% across many hits
  usually means `hops`/`hub_cutoff` are set too loose for this team's
  graph shape (worth tuning per ADR.md's traversal notes).
- **Anchor hit rate**: share of `retrieve_context` calls that returned a
  note (`agenthive_retrieval_hits_total` vs
  `agenthive_retrieval_empty_total`). This is separate from the cache
  hit rate. Which phrases missed, and whether a pending note would have
  matched, is on the review UI's Metrics tab (`top_missed_anchors`),
  not in Prometheus - anchor text would be an unbounded label.
- **Cache hit rate** and **cross-teammate cache hits**: the "shared
  memory, not N copies" evidence. A hit rate near zero with real traffic
  either means the TTL (`AGENTHIVE_CACHE_TTL_SECONDS`) is too short for
  how your team actually clusters retrievals in time, or genuinely means
  agents aren't asking about the same things - both are useful to know.
- **Memory nodes by status** and **auto-approved vs. manually reviewed**:
  "how this improves over time" isn't a number AgentHive computes for
  you - it's this dashboard's slope across weeks. A team that tunes its
  auto-approve rules (see README.md) should see the auto-approved line
  grow relative to the manual-approval line, without the rejected line
  growing too - if rejections climb alongside auto-approvals, a rule is
  too loose.
- **HTTP request rate / p95 latency by route**: standard service health,
  broken down by the route name (not raw path, so team/user/node IDs
  never end up as a label - see `server.py`'s `ROUTES` table).

## Metrics vs. tracing: two different questions

Everything above answers "in aggregate, over time, is this working."
Tracing (`tracing.py`, off by default - see `OTEL_EXPORTER_OTLP_ENDPOINT`
/ `AGENTHIVE_TRACING_CONSOLE` in `.env.example`) answers a narrower one:
"for THIS retrieve_context call, where did the time go, and what did the
server actually do?" A trace shows the rate-limit check, the auth
lookup, the cache lookup (hit or miss, and whether it was a cross-user
hit), the graph traversal, and - for a write - the memory-write and
approval-gate spans, all on one timeline for one call. Neither replaces
the other: metrics tell you the cache hit rate is climbing team-wide
this month; a trace tells you why one specific retrieve_context call
took 40ms instead of 2ms (a Redis round trip on a cache miss, say).

Two exporters: `AGENTHIVE_TRACING_CONSOLE=true` prints each finished
span as JSON to stdout - no infrastructure, good for confirming
instrumentation is working before wiring up a real backend.
`OTEL_EXPORTER_OTLP_ENDPOINT` sends spans to Jaeger (`docker compose
--profile jaeger up`, or the Helm chart's `tracing.otlpEndpoint`) or any
other OTLP/HTTP-compatible backend - this is the "web UI" for actually
browsing traces, separate from the review UI (`GET /ui`) and Grafana
(the metrics trend view). See `ONBOARDING.md`'s "Watch it work" step for
a guided first look, and `ADR.md`'s "Reliability and observability"
section for why it's
instrumented where it is (the `server.py` handler layer, not inside
`db.py`/`cache.py`/`retrieval.py`) and how cross-process trace
propagation was actually verified, not just assumed to work.

## Per-team detail (not in Grafana)

`GET /teams/{id}/metrics/summary` (admin-only) and the review UI's
Metrics tab answer team-specific questions the cross-team Prometheus
metrics deliberately can't: which nodes get reused across the most
teammates, how many retrievals *this* team has made, how many active
users *this* team has. Backed by `memory_access_log` in `db.py` - one
row per (retrieval, node, user), which is also what
`agenthive_cross_user_cache_hits_total` is corroborating from the cache
side. Empty retrievals are aggregated in `retrieval_misses` (one row per
normalised anchor) and returned as `retrieval_misses` /
`top_missed_anchors`, including a pending title that would have matched.


## Progressive disclosure: what the index/fetch counters do and don't claim

`mode=index` returns titles instead of bodies, so the response really is
small. It would be easy - and wrong - to let that inflate
`agenthive_tokens_avoided_total`: the number would jump the moment a
caller switched mode, and jump again in the same direction when it
fetched the bodies back. A caller that fetches every id has saved
nothing and spent an extra round trip.

So avoidance stays defined against the full neighborhood regardless of
mode, and the index/fetch pair is tracked separately:

| Metric | Meaning |
|---|---|
| `agenthive_index_tokens_withheld_total` | Body tokens not sent because the caller asked for the index. **Gross, not net.** |
| `agenthive_fetch_tokens_served_total` | Tokens returned by `/memory/fetch` - the half that costs. |
| `agenthive_retrievals_by_mode_total{mode}` | `full` vs `index`, so adoption is visible. |
| `agenthive_fetch_nodes_total{outcome}` | `served` vs `missing`. A rising `missing` means callers are holding stale ids. |

The real saving is the difference. In Grafana:

```promql
increase(agenthive_index_tokens_withheld_total[24h])
  - increase(agenthive_fetch_tokens_served_total[24h])
```

If that trends toward zero, progressive disclosure is costing a round
trip and buying nothing - agents are fetching everything they index, and
they should go back to `retrieve_context`. That is a real possible
outcome and the dashboard should be able to say so.

| `agenthive_auto_index_total` | Retrievals the **server** switched to index mode because the full neighborhood was over `AGENTHIVE_AUTO_INDEX_OVER_TOKENS`. Opt-in disclosure was never chosen by agents in practice, so the decision moved server-side; this counter is how you see it happening. |

## Environment facts

The one path that **replaces** work instead of adding to context. A fact
tagged `env-fact` is injected at session start with no anchor and no
tool call, so the session never runs the exploration that would have
discovered it. Everything else in AgentHive is additive; this is the
subtractive half, and it is where a reduced token bill actually comes
from.

| Metric | Meaning |
|---|---|
| `agenthive_facts_served_total` | Sessions that received the fact preamble. |
| `agenthive_fact_tokens_total` | Tokens spent on it. Capped per session - facts are length-limited at write time (200 characters), so this grows with sessions, not with the graph. |
| `agenthive_tokens_injected_total` | Everything AgentHive put **into** a context window, across all paths. |

`tokens_injected_total` exists to keep the other numbers honest. Read it
next to `tokens_avoided_total`, never alone:

```promql
increase(agenthive_tokens_avoided_total[24h])
  - increase(agenthive_tokens_injected_total[24h])
```

A positive result is a saving. A negative one means AgentHive is costing
this team tokens, which is a thing it is allowed to say.

## Tool usage, pooled

Reported by the plugin's hooks, not by the server - the server only
stores it. Pooling matters: one person not using a tool is a
preference, several people not using it across unrelated sessions is
evidence.

Per-team detail is on `GET /insights/tools`; nothing is removed from
anybody's config without an explicit `agenthive toolprune apply`, and
`restore` takes back exactly what was written and nothing else.

## Governance counters

| Metric | Meaning |
|---|---|
| `agenthive_private_blocks_stripped_total` | `<private>` blocks removed before storage. Never zero-by-design: zero means nobody is using the marker, not that nothing was sensitive. |
| `agenthive_auto_approve_blocked_total` | Writes that matched an auto-approve rule but were held for review anyway because of a reserved tag. A steady rate here means a rule is too broad. |


## Is the learning loop working?

The memory-processing layer (`insights.py`) raises proposals from how
the graph has been used. A self-improvement layer that cannot be shown
to improve anything is the same mistake as a retrieval layer that
asserts a token saving without measuring it, so these exist to answer
the question honestly - including when the answer is no.

| Metric | Meaning |
|---|---|
| `agenthive_gaps_closed_total` | **The headline.** An anchor that returned nothing now returns something. The graph learned a thing people kept asking for. |
| `agenthive_gap_misses_recovered_total` | Accumulated misses at the moment each gap closed. Divided by the above, it is the average number of failed lookups a gap cost before anyone filled it - the loop's latency, measured in wasted retrievals. |
| `agenthive_open_gaps` | Distinct anchors currently returning nothing (gauge, scrape time). |
| `agenthive_proposals_raised_total{kind}` | What the layer **found**: `gap`, `link`, `tier`, `stale`, `rule`, `noisy`. |
| `agenthive_tier_changes_applied_total{direction}` | What an admin **acted on**, `promote` vs `demote`. |
| `agenthive_memory_nodes_by_tier{tier}` | The graph stratifying by use (gauge, scrape time). |

### The three shapes worth alerting on

**The loop is working.** `gaps_closed_total` rising while `open_gaps`
stays flat or falls. Gaps are being found and filled at roughly the rate
they appear.

```promql
increase(agenthive_gaps_closed_total[7d])
```

**The loop is being ignored.** `proposals_raised_total` climbing while
`tier_changes_applied_total` stays flat and `open_gaps` grows. The layer
is doing its job and nobody is reading the output. This is the most
likely failure and the easiest to miss, because nothing errors.

```promql
increase(agenthive_open_gaps[30d]) > 0
  and increase(agenthive_gaps_closed_total[30d]) == 0
```

**The thresholds are wrong.** Promotions and demotions both high, on a
small graph - nodes oscillating between tiers rather than settling.
Raise the counts in `insights.TIER_RULES`.

```promql
rate(agenthive_tier_changes_applied_total{direction="promote"}[7d])
  and rate(agenthive_tier_changes_applied_total{direction="demote"}[7d])
```

A flat `agenthive_memory_nodes_by_tier{tier="L1"}` line holding every
node in the graph means tier proposals are being generated and never
applied - the same ignored-loop shape, visible from a different angle.

### What these do not claim

`gaps_closed_total` counts an anchor going from empty to non-empty. It
does not claim the note that closed it was any *good* - only that
somebody can now retrieve something for a phrase that used to return
nothing. Quality is what the approval gate is for, and no counter here
substitutes for it.


## Operational health

These exist because the failures they catch are **silent**. Nothing
errors, nothing 500s, and the only symptom is answers quietly getting
worse.

### The review queue

Pending memory is invisible to retrieval. A queue nobody is clearing
therefore degrades every agent's context with no error anywhere, which
makes this the thing to page on rather than merely graph.

| Metric | Meaning |
|---|---|
| `agenthive_oldest_pending_age_seconds` | Age of the longest-waiting unreviewed node (gauge). |
| `agenthive_review_latency_seconds` | Write-to-decision time, both approvals and rejections. |
| `agenthive_writes_by_reserved_tag_total{tag}` | What the queue is made of. A queue that is mostly one tag has one upstream cause. |

```promql
agenthive_oldest_pending_age_seconds > 3 * 24 * 3600
```

Reading `review_latency` alongside `memory_writes_total` tells you
whether the gate is a bottleneck or a formality: a p50 of minutes with
near-total approval means it is ceremony, and auto-approve rules are
being under-used.

### Retrieval quality

| Metric | Meaning |
|---|---|
| `agenthive_anchor_resolution_total{resolution}` | `exact`, `alias`, `fallback`, or `none`. |
| `agenthive_anchor_aliases` | Phrasings a human has mapped to a node (gauge). |
| `agenthive_retrieval_neighborhood_size` | Nodes returned per retrieval. |

A rising `fallback` share means titles no longer match how people phrase
things. That is a **naming** problem, and no amount of `hops` or
`hub_cutoff` tuning will fix it. The fix is better titles, or an alias -
and `resolution="alias"` rising while `fallback` falls is what recording
them paying off looks like.

```promql
sum(rate(agenthive_anchor_resolution_total{resolution="fallback"}[1h]))
  / sum(rate(agenthive_anchor_resolution_total[1h]))
```

`neighborhood_size` is the distribution to look at *before* changing
`hops` or `hub_cutoff`. A long right tail means traversal is pulling in
more than anyone reads, which is what `mode=index` exists for.

### Auth and storage

| Metric | Meaning |
|---|---|
| `agenthive_auth_failures_total{reason}` | `missing_header`, `unknown`, `revoked`, `expired`. |
| `agenthive_db_up` | 1 when the backend answers, 0 when it does not. |

The HTTP response deliberately refuses to say which case applies, so a
stolen-token probe learns nothing (see `db.user_by_token`). The metrics
port is a different audience: a spike in `expired` is a token-TTL
rollout biting, a spike in `revoked` is someone using a token you
killed, and the two want different responses.

`db_up` is emitted even when the DB-backed gauges throw, because that is
precisely when the backend is what is wrong and reporting nothing would
turn an outage into silence.


## Automated rejection

| Metric | Meaning |
|---|---|
| `agenthive_auto_rejected_total{cause}` | `duplicate`, `trivial`, or `rule`. |

Auto-rejection is defensible because a lost note can be written again.
That stops being true if a rule is too broad: it then discards knowledge
quietly, at volume, and nothing else in the system would say so. Watch
the rate, and read `GET /teams/{id}/memory/rejected` when it moves.

```promql
rate(agenthive_auto_rejected_total{cause="rule"}[1h])
```

A `duplicate` rate that keeps climbing is a different signal: agents are
relogging content that already exists, which usually means they are not
calling retrieval at the start of a session.

The reason string carries node and rule ids and is bucketed to three
label values before it reaches a metric. Ids in a label set is how a
metrics endpoint becomes a memory leak.


## Corroboration

| Metric | Meaning |
|---|---|
| `agenthive_memory_feedback_total{verdict}` | `confirmed` / `contradicted` from sessions that used a note. |
| `agenthive_disputed_nodes` | Approved nodes contradicted and unresolved (gauge). |

The shape to alert on is **contradictions rising while `disputed_nodes`
never falls**: people are reporting, nobody is resolving, and the graph
is knowingly serving notes somebody has already said are wrong. Unlike a
pending node, these are being read right now.

```promql
agenthive_disputed_nodes > 0
  and increase(agenthive_memory_feedback_total{verdict="contradicted"}[7d]) > 0
```

A healthy ratio of `confirmed` to `contradicted` is also the closest
thing here to an answer on whether the graph is any *good* — which is
otherwise unmeasured. It is a weak proxy and should not be mistaken for
one: it says what the people who bothered to report think, not what is
true.


## Did memory help?

The question AgentHive could never answer. It never sees the task, so it
cannot judge success on its own — but a session can report back, and git
can be asked whether the work survived.

| Metric | Meaning |
|---|---|
| `agenthive_session_outcomes_total{outcome,arm}` | What sessions said about themselves, split by whether memory was served. |
| `agenthive_work_survival_total{survived}` | Whether a session's commit was still an ancestor of HEAD later. Objective; nobody reports it about themselves. |
| `agenthive_session_turns` | Turns per session — the multiplier on every retrieved token. |
| `agenthive_holdout_retrievals_total` | Retrievals deliberately served nothing. Zero unless you turned the holdout on. |

`GET /teams/{id}/evidence` does the comparison. Read it with two things
in mind.

**A silent session is unknown, not a success.** Sessions that report no
outcome are counted separately and kept out of the denominator. A rate
computed over whoever bothered to answer measures who answers.

**Without a holdout, the verdict is correlation.** Sessions that reach
for memory differ from sessions that do not in ways nothing here
controls for — the people using it may simply be doing the kind of work
it suits. The endpoint labels this `causal: false` and says so in prose.

### Getting a causal answer

`AGENTHIVE_HOLDOUT_PERCENT` withholds memory from that percentage of
sessions, chosen deterministically per session, and tells each one it is
being held out. Then `served` versus `held_out` is a real experiment.

It is **off by default and should stay that way** unless you have
decided to run it. It buys evidence by deliberately making some sessions
worse, which is a trade only the team living with it can consent to.
Start at 5–10%, run it for a few weeks, and turn it off once the
comparison has enough sessions in both arms.

### What would settle it properly

A holdout answers "does this help us". It does not answer "does this
help anyone", and nothing in a production deployment can. That needs a
benchmark — the same tasks run with and without, repeated — which is
what `experiments/` is for.
