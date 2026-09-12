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
  is the running sum of that gap. Read it as "tokens NOT sent because
  retrieval was scoped instead of dumped," not as "tokens saved on your
  OpenAI/Anthropic bill" - the second claim would need this service to
  see your actual model calls, which it deliberately doesn't (see
  ADR.md's "Why not build what TencentDB built").
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
  `reduction_pct` from `retrieval.py`. A number near 0% across many
  retrievals usually means `hops`/`hub_cutoff` are set too loose for
  this team's graph shape (worth tuning per ADR.md's traversal notes).
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
a guided first look, and `ADR.md`'s "v3" section for why it's
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
side.
