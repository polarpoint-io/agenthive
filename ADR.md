# ADR: AgentHive

Status: v3, in use. Originally written 2026-09-12 in response to reading
TencentDB Agent Memory's install doc and wanting the same "shared memory
across agents" outcome without three of their tradeoffs: no LLM-intercepting
proxy, no per-request token tax, and a hard approval gate before anything
becomes shared memory. See "v1 - resolving the open questions" for what
changed from v0, "v2 - containerized for real, metrics, and closing the
remaining gaps" for what changed next, and "v3 - the entire journey,
mapped" at the bottom for what changed since.

## What this is for

Three goals, in order of how load-bearing they are to the design:

1. **Guardrails and structured context first.** Memory is scoped to a
   `team / agent / task` graph, same shape as TencentDB's, and nothing an
   agent writes becomes visible to other agents until an admin (or an
   explicit auto-approve policy) approves it. This is the part that matters
   even if the token story turns out to be a wash.
2. **Token reduction, not token addition.** Retrieval works by traversing
   the approved memory graph outward from an anchor, with a hub-cutoff so
   one heavily-linked node can't pull in the whole team's history. Nothing
   is injected by default; an agent asks for a bounded neighborhood and
   gets exactly that.
3. **No excessive calls.** There is deliberately no proxy sitting in front
   of every LLM request. An agent's session makes at most two extra HTTP
   calls: one at the start (retrieve context for this team/agent/task) and
   one at the end (log what happened, into the pending queue unless an
   auto-approve rule applies). Compare to TencentDB's design, where every
   single turn passes through their proxy's auth -> session-init ->
   injection pipeline.

## Why not build what TencentDB built

Their proxy-injection model is the more powerful design on paper: it works
across six different agent clients without each one needing custom
integration code, and it enriches every turn automatically. I looked at it
closely enough to write the previous post on it, and decided not to copy
the mechanism, for three reasons:

- **It's the least mature part of their system.** Their own docs list
  session-ID handling as broken for two of six clients (Hermes, OpenClaw
  need `x-task-id` just to avoid a form those clients can't render), and
  Codex needs a manual Plan-mode workaround because its default mode
  auto-executes the very tool call the picker depends on. Protocol
  translation across Anthropic Messages / OpenAI Chat / OpenAI Responses is
  real, ongoing surface area, not a solved problem.
- **It hides the token cost.** Injection happens on every turn whether or
  not that turn needed the extra context. An explicit retrieve-at-start
  call means the token cost is visible and the agent (or the human running
  it) decided to pay it.
- **It's harder to build correctly in the time I have.** A proxy that
  needs to stay wire-compatible with three different LLM API shapes is a
  much bigger, longer-lived engineering commitment than a small HTTP API
  a client calls explicitly. Explicit calls are also just easier to reason
  about when something goes wrong.

## Core entities

- **Team** — owns everything. Memory never crosses a team boundary; this
  is enforced at the query layer, not by convention.
- **User** — a person with a personal token and a role (`admin` or
  `member`), scoped to one team. Replaces v0's single shared team API key
  (see "v1" below).
- **Agent** — a role under a team (name, description, system prompt).
  Memory can optionally be filtered/attributed by agent, but any user
  under a team can retrieve any approved memory under that team by
  default (that's the "hive mind" property this was built for).
- **Task** — optional. A memory written without a task still works; it
  just has no Task-level filter available later.
- **MemoryNode** — the actual content. Has a tier (L0 raw / L1 extracted /
  L2 scene / L3 persona, same ladder as TencentDB, though this still only
  really uses L0/L1 — nothing here does the LLM-driven L1->L2->L3
  promotion, see Open Questions), a status (`pending` / `approved` /
  `rejected`), and a set of typed links to other node titles (unresolved
  links are allowed but never become traversable graph edges).
- **AutoApproveRule** — an admin-configured rule (match by tag or by
  agent) that causes a newly-written node to skip the pending queue
  entirely. Every auto-approved node keeps `auto_approved` and `rule_id`
  set, so the audit trail says *why* it was approved and by which rule.

## The write path

1. A user's agent session ends. The client library posts a `MemoryNode`
   — title, body, tags, links, which team/agent/task it belongs to.
2. If an active auto-approve rule matches (by tag or by agent), the node
   is immediately `approved` and visible to retrieval, with the audit
   trail pointing at the rule.
3. Otherwise it is `pending`, invisible to retrieval, until an admin
   calls approve or reject.
4. Only `approved` nodes are ever returned by traversal.

This is the guardrail. A team of engineers can't be trusted to all
remember the same standing instruction the way one person's AI sessions
can, so the gate is structural rather than a memory-encoded instruction.

## The retrieve path

`GET /teams/{id}/memory/retrieve?anchor=<title>&hops=2&hub_cutoff=15`

BFS outward from an anchor node's resolved links, stopping traversal
(but not visitation) through any node whose out-degree exceeds
`hub_cutoff`, restricted to `status=approved` and the given `team_id`.
Returns the neighborhood plus an approximate token count.

## Auth (v1)

Per-user personal tokens (`X-API-Key` header), hashed at rest (SHA-256,
since tokens are high-entropy random values from `secrets`, not
low-entropy passwords, so a fast hash is the right tradeoff here). Every
user has a role: `admin` (review pending memory, manage users, configure
auto-approve rules) or `member` (log/retrieve memory, manage
agents/tasks). Tokens can be rotated (old one stops working immediately)
or revoked. See `auth.py` and `tests/test_auth.py`.

## v1 — resolving the open questions

The original v0 draft flagged five things as deliberately unsolved. Status
of each, now:

- **Per-user auth.** ~~One key per team is a placeholder, not a design.~~
  Resolved: personal tokens, roles, rotation, revocation. See "Auth (v1)"
  above.
- **Auto-approve policy.** ~~Manual approval is the only thing
  implemented.~~ Resolved: tag- or agent-scoped rules, with an audit
  trail on every auto-approved node. Still deliberately simple: no regex
  matching, no size thresholds, no per-rule expiry. Add those if the
  team's actual usage shows a need for finer-grained rules.
- **Multi-node / concurrent writers.** ~~SQLite is fine for a single
  team's single-node deployment.~~ Resolved for the storage layer:
  `DATABASE_URL` switches to Postgres with no code changes, same schema.
  Not fully resolved for the process itself: rate limiting is still
  in-memory per-process (see DEPLOYMENT.md's "Known limitations"), so a
  multi-replica deployment behind a load balancer has per-replica, not
  global, rate limits. Fine as a soft guard against a runaway agent; not
  a hard multi-tenant quota. A shared limiter (Redis) is the next step if
  that turns out to matter.
- **L1 -> L2 -> L3 promotion.** Still not built. Deliberately deferred:
  building an LLM-driven summarization pass against limited real L1
  volume is still guessing at a policy nobody has needed yet. Revisit
  once there's enough real usage to know what a good summary policy looks
  like.
- **No LLM-request proxy.** Still a deliberate choice, not a missing
  feature — see "Why not build what TencentDB built" above. Unchanged.

Also added in v1, not originally called out as an open question but
necessary for "point this at a real team" use: structured JSON request
logging, `/healthz` + `/readyz` health endpoints, a rate limiter, and a
small review UI (`GET /ui`) so approve/reject doesn't require a terminal
and a Python REPL.

## v2 — containerized for real, metrics, and closing the remaining gaps

v1's DEPLOYMENT.md flagged two things as known limitations rather than
solved problems: no built-in TLS, and a rate limiter that couldn't be
shared across replicas. Both are closed now, and a metrics/caching layer
was added on top - not because the ADR asked for it, but because a
service whose whole pitch is "sends fewer tokens, gets reused across the
team" should be able to show that happening instead of asserting it.

- **TLS.** `server.py` wraps its socket in an `ssl.SSLContext` when
  `AGENTHIVE_TLS_CERT_FILE`/`AGENTHIVE_TLS_KEY_FILE` are set (see
  `tests/test_tls.py`). Most deployments should still terminate TLS at a
  reverse proxy or the Helm chart's Ingress - native support exists for
  the deployments that have neither in front of them.
- **Shared rate limiting.** `auth.RedisRateLimiter` replaces the
  in-memory limiter when `REDIS_URL` is set - a fixed-window counter
  (`INCR` + `EXPIRE`) shared across every replica pointed at the same
  Redis, proven with two real server processes in
  `tests/test_redis_backends.py::test_redis_rate_limit_shared_state`.
  Redis being unreachable degrades to "allow" rather than failing
  requests - a rate limiter should never be a new outage cause. Unset
  `REDIS_URL` and it's exactly v1's in-memory limiter, unchanged.
- **Retrieval cache.** New: `cache.py` caches `retrieve()`'s result per
  `(team, anchor, hops, hub_cutoff)`, in-memory by default or in Redis
  when `REDIS_URL` is set (same flag as the rate limiter — one Redis,
  two uses). A write or review for a team invalidates that team's
  entries immediately; the TTL (`AGENTHIVE_CACHE_TTL_SECONDS`) is a
  backstop, not the primary invalidation path. The reason this exists
  isn't raw performance - it's that a cache hit served to a *different*
  user than the one who populated it is direct, countable proof the
  graph is shared memory rather than N private copies. See
  `agenthive_cross_user_cache_hits_total` below.
- **Metrics.** New: `GET /metrics` (Prometheus format, `metrics.py`), no
  team_id or user_id labels (unbounded cardinality on a metrics endpoint
  is its own incident waiting to happen). Headline series:
  `agenthive_tokens_avoided_total` (the running sum of full-team tokens
  minus what was actually returned, across every retrieval - read it as
  "tokens not sent because retrieval was scoped," not as a real LLM
  billing number, since this service never calls a model - see
  `observability/README.md`'s "what's actually measured" section for the
  full honesty pass on this), `agenthive_cache_hits_total` /
  `agenthive_cross_user_cache_hits_total` (shared-reuse evidence), and
  DB-backed gauges (`agenthive_memory_nodes`, `agenthive_active_users`,
  `agenthive_teams`) computed fresh at scrape time via a custom
  collector rather than kept in sync by hand. Per-team detail (which
  nodes get reused across the most teammates, this team's retrieval
  count) lives in `GET /teams/{id}/metrics/summary` instead, backed by a
  new `memory_access_log` table (`db.py`) - one row per
  (retrieval, node, user). "How this improves over time" is answered by
  graphing these in Grafana (`observability/grafana-dashboard.json`),
  not by a number this service computes for you.
- **Containerization, for real.** The Dockerfile now installs
  `prometheus_client` (hard dependency), `psycopg2-binary`, and `redis`
  (both optional, lazily imported) at build time, and its `HEALTHCHECK`
  is TLS-aware. `docker-compose.yml` gained `postgres` and `redis`
  Compose profiles alongside the existing default (SQLite only, single
  container).
- **Helm chart.** `helm/agenthive/` — SQLite+single-replica or
  Postgres+N-replicas (the chart refuses to render SQLite with more than
  one replica, and refuses `autoscaling.enabled` without Postgres,
  rather than let either fail confusingly at runtime — see
  `templates/_helpers.tpl`'s `agenthive.validate`), optional Redis
  wiring, Ingress or native TLS, and either a `ServiceMonitor`
  (Prometheus Operator) or plain `prometheus.io/*` pod annotations for
  metrics scraping. See `helm/agenthive/README.md`.

Open questions carried forward unchanged from v1: no L1→L2→L3 promotion
worker, no LLM-request proxy (still deliberate), no token expiry policy,
and the Redis rate limiter's fixed-window approximation (documented in
DEPLOYMENT.md rather than treated as a defect - a request-rate guard
doesn't need billing-grade precision).

## v3 - the entire journey, mapped

v2 answered "is this working, in aggregate, over time" with metrics.
This round answers a narrower, more immediate question a team actually
asks while debugging or just curious: "for the retrieve_context call my
agent just made, where did the time go, and what did the server
actually do?" - plus made getting a new team from zero to using this at
all a documented, ordered process instead of README archaeology.

- **OpenTelemetry tracing (`tracing.py`).** Off by default -
  `OTEL_EXPORTER_OTLP_ENDPOINT`/`AGENTHIVE_TRACING_CONSOLE` both unset
  means zero cost, same pattern as `REDIS_URL`/`DATABASE_URL` being
  unset. Instrumented at the `server.py` handler layer only (not inside
  `db.py`/`cache.py`/`retrieval.py` internals) so those modules stay
  free of a cross-cutting concern and keep their own test story simple -
  the root span per request plus child spans for the rate-limit check,
  auth lookup, cache lookup, graph traversal, the memory write, and the
  approval-gate review together already show the whole journey without
  needing tracing wired through every layer. `client.py` propagates the
  current span's W3C `traceparent` header on every call (case-lowering
  headers on the receiving end - stdlib `http.server` and `urllib`
  capitalize header names on the wire, which would otherwise silently
  break the standard propagator's case-sensitive lookup); wrapping
  `retrieve_context`+`log_session` in `client.traced_session(...)` links
  a whole agent session into one trace. Verified two ways:
  `tests/test_tracing.py` uses the zero-infrastructure console exporter
  against a real running server (including a real cross-process
  propagation check - two separate `server.py`/script processes, same
  pattern as the Redis rate-limiter test); separately, a real Jaeger
  container was run during development and queried via its own API to
  confirm OTLP export actually lands with the full expected span set,
  not just that the code compiles.
- **No hard dependency added.** `opentelemetry-api`/`-sdk`/`-exporter-otlp-proto-http`
  are lazily imported (same pattern as `psycopg2`/`redis`) - a plain
  `python3 server.py` with none of them installed still runs, with
  `TRACER` becoming a tiny local no-op shim. The OTLP exporter is the
  HTTP/protobuf variant specifically to avoid a `grpc` dependency.
- **Graceful shutdown flushes traces.** The OTLP exporter batches spans
  (~5s default export interval); SIGTERM previously had no handler in
  `server.py` at all, so a container being stopped (the normal
  Docker/Kubernetes shutdown path) would silently drop whatever hadn't
  been flushed yet. `main()` now converts SIGTERM into the same
  graceful-shutdown path as Ctrl+C and calls
  `tracing.shutdown_tracing()` on the way out.
- **`ONBOARDING.md`.** A single ordered walkthrough (structured after
  TencentDB Agent Memory's own INSTALL.md, minus the parts that don't
  apply here since this isn't a multi-client proxy) for taking a team
  from "nothing running" to "reviewing memory and watching traces" -
  install, create team, add users, wire up an agent, review, auto-approve
  rules, then metrics/tracing. README.md stays the concept/reference
  doc; this is the "do this, in this order" doc a new team actually
  follows.
- **Jaeger as the trace-visualization web UI.** Interpreted "a web UI"
  for "the entire journey mapped and visualised" as the natural pairing
  with tracing rather than a second, redundant dashboard next to the
  review UI (`GET /ui`, unchanged) and Grafana (already the metrics
  trend view) - a team that turns tracing on needs somewhere to actually
  browse traces, and Jaeger's all-in-one image is that, wired as an
  opt-in `docker compose --profile jaeger` service and a Helm
  `tracing.otlpEndpoint` value, never bundled into the chart itself
  (same reasoning as not bundling Postgres/Redis).

Open questions carried forward: everything from v1/v2's lists, plus no
trace sampling policy (every request is traced when tracing is enabled -
fine at today's traffic, worth revisiting before very high throughput).
