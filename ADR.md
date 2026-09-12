# ADR: AgentHive

Status: in use.

A "shared memory across agents" design that avoids three tradeoffs common
to proxy-based agent memory systems: no LLM-intercepting proxy, no
per-request token tax, and a hard approval gate before anything becomes
shared memory.

## What this is for

Three goals, in order of how load-bearing they are to the design:

1. **Guardrails and structured context first.** Memory is scoped to a
   `team / agent / task` graph, and nothing an
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
   auto-approve rule applies). Compare to a proxy-injection design, where
   every single turn passes through an auth -> session-init -> injection
   pipeline before it ever reaches the model.

## Why not build an LLM-request proxy

A proxy-injection model is the more powerful design on paper: it can work
across many different agent clients without each one needing custom
integration code, and it enriches every turn automatically. I considered
that shape and decided not to build it, for three reasons:

- **It's the hardest part to get right, and the least necessary.**
  Different agent clients disagree on session identity, tool-call
  rendering, and how eagerly they auto-execute a tool call versus wait for
  a picker - so a proxy has to special-case each one. Protocol translation
  across Anthropic Messages / OpenAI Chat / OpenAI Responses is real,
  ongoing surface area, not a solved problem.
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
- **User** — a person or agent with a personal token and a role (`admin`
  or `member`), scoped to one team.
- **Agent** — a role under a team (name, description, system prompt).
  Memory can optionally be filtered/attributed by agent, but any user
  under a team can retrieve any approved memory under that team by
  default (that's the "hive mind" property this was built for).
- **Task** — optional. A memory written without a task still works; it
  just has no Task-level filter available later.
- **MemoryNode** — the actual content. Has a tier (L0 raw / L1 extracted /
  L2 scene / L3 persona - a common tiering scheme for agent memory: raw
  capture, extracted facts, summarized scenes, persona-level distillation
  - though this still only really uses L0/L1 — nothing here does the
  LLM-driven L1->L2->L3
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

## Authentication

Two independent login paths, because agents and humans have different
needs here:

**Personal API tokens (agents and humans alike).** A per-user token
(`X-API-Key` header), hashed at rest (SHA-256, since tokens are
high-entropy random values from `secrets`, not low-entropy passwords, so a
fast hash is the right tradeoff here). Every user has a role: `admin`
(review pending memory, manage users, configure auto-approve rules) or
`member` (log/retrieve memory, manage agents/tasks). Tokens can be rotated
(old one stops working immediately) or revoked. Optional expiry
(`AGENTHIVE_TOKEN_TTL_SECONDS`, 0 = never expires, the default): set it and
every token minted or rotated from then on carries a `token_expires_at`;
`user_by_token` rejects an expired token exactly like a revoked one (same
401, no distinguishing message - deliberate, so a stolen token doesn't
tell whoever holds it "this existed and expired" versus "this never
existed"). Only applies going forward - an existing token keeps working
until it's next rotated, so turning this on doesn't retroactively lock out
a team. See `auth.py`, `db.py`, `tests/test_auth.py`.

**Azure AD (Microsoft Entra ID) sign-in, humans only.** The review UI
(`GET /ui`) additionally supports signing in with Azure AD via the
standard OAuth2 Authorization Code flow with PKCE
(`GET /auth/azure/login` / `GET /auth/azure/callback`, `oidc.py`). This is
a second login path bolted onto the same user model, not a replacement for
personal tokens - agents keep using their existing opaque tokens
unchanged, since Azure AD sign-in is meaningless for a non-interactive
client. Design choices worth calling out:

- **Server-side token exchange.** The authorization code exchange and the
  PKCE `code_verifier` both stay server-side (`db.py`'s `oidc_states`
  table holds the verifier between the login and callback requests) rather
  than in browser JavaScript - a more standard confidential-client pattern
  for a backend service than a public-client SPA flow, and it keeps the
  client secret out of anything the browser can read.
- **No auto-provisioning.** A successful Azure sign-in only ever produces
  a session for an AgentHive user that already exists and has been
  explicitly linked to that Azure AD object id (`oid` claim - stable per
  user, unlike `email`) by an admin, via `POST
  /teams/{id}/users/{id}/link-azure`. Signing in with an Azure account
  that isn't linked to anything gets a clear "not linked yet, here's your
  object id, ask an admin" response rather than silently creating an
  account. This keeps team membership an explicit, auditable admin action
  regardless of which login path a human used, and avoids reproducing
  Azure AD's whole tenant membership as an auto-provisioning surface this
  service would then have to trust.
- **Sessions, not tokens.** A successful sign-in mints a row in a new
  `sessions` table - functionally identical to a personal token from
  `server.py`'s point of view (the same `X-API-Key` header, the same
  `_authenticate` path) but short-lived on purpose
  (`AGENTHIVE_AZURE_SESSION_TTL_SECONDS`, default 1 hour) and revoked
  wholesale by deleting the row rather than living forever like an agent's
  token. A human re-authenticating against Azure AD periodically is normal
  and expected; an agent doing so is not, which is the whole reason these
  are two different mechanisms instead of one.
- **JWKS-verified, not trust-on-first-use.** The `id_token` Azure AD
  returns is verified against Microsoft's published signing keys
  (`https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys`,
  cached and refreshed on a key-id miss or after 24h) before any claim in
  it is trusted - issuer, audience, expiry, and the presence of `oid` are
  all checked before a session is minted.
- **One new optional dependency.** PyJWT (with its `[crypto]` extra) is
  lazily imported (`oidc.py`), same pattern as `psycopg2`/`redis` - a
  deployment that never sets `AGENTHIVE_AZURE_*` never needs it installed.

## Storage

Two backends share one schema: SQLite (default - single file, zero
dependencies, correct for a single-node deployment) and Postgres (used
automatically when `DATABASE_URL` is set - needed once multiple replicas
are writing concurrently). The schema is written to be valid SQL in both
(`TEXT`-typed columns, app-generated ids, `CREATE TABLE/INDEX IF NOT
EXISTS`), and every column added after the original schema (`token_expires_at`,
`azure_oid`, plus the new `oidc_states`/`sessions` tables) migrates on
additively via `ALTER TABLE ... ADD COLUMN` on every backend startup,
rather than requiring a fresh database. See `db.py`.

## Reliability and observability

- **TLS.** `server.py` wraps its socket in an `ssl.SSLContext` when
  `AGENTHIVE_TLS_CERT_FILE`/`AGENTHIVE_TLS_KEY_FILE` are set (see
  `tests/test_tls.py`). Most deployments should still terminate TLS at a
  reverse proxy or the Helm chart's Ingress - native support exists for
  the deployments that have neither in front of them.
- **Shared rate limiting.** A sliding-window limiter, per API key,
  in-memory by default (correct for a single node). `auth.RedisRateLimiter`
  replaces it with a fixed-window counter (`INCR` + `EXPIRE`) shared across
  every replica when `REDIS_URL` is set, proven with two real server
  processes in `tests/test_redis_backends.py::test_redis_rate_limit_shared_state`.
  Redis being unreachable degrades to "allow" rather than failing requests
  - a rate limiter should never be a new outage cause.
- **Retrieval cache.** `cache.py` caches `retrieve()`'s result per
  `(team, anchor, hops, hub_cutoff)`, in-memory by default or in Redis
  when `REDIS_URL` is set (same flag as the rate limiter — one Redis, two
  uses). A write or review for a team invalidates that team's entries
  immediately; the TTL (`AGENTHIVE_CACHE_TTL_SECONDS`) is a backstop, not
  the primary invalidation path. The reason this exists isn't raw
  performance - it's that a cache hit served to a *different* user than
  the one who populated it is direct, countable proof the graph is shared
  memory rather than N private copies. See
  `agenthive_cross_user_cache_hits_total` below.
- **Metrics.** `GET /metrics` (Prometheus format, `metrics.py`), no
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
  `agenthive_teams`) computed fresh at scrape time via a custom collector
  rather than kept in sync by hand. Per-team detail (which nodes get
  reused across the most teammates, this team's retrieval count) lives in
  `GET /teams/{id}/metrics/summary` instead, backed by a
  `memory_access_log` table (`db.py`) - one row per (retrieval, node,
  user). "How this improves over time" is answered by graphing these in
  Grafana (`observability/grafana-dashboard.json`), not by a number this
  service computes for you.
- **Distributed tracing (`tracing.py`).** Off by default -
  `OTEL_EXPORTER_OTLP_ENDPOINT`/`AGENTHIVE_TRACING_CONSOLE` both unset
  means zero cost, same pattern as `REDIS_URL`/`DATABASE_URL` being unset.
  Instrumented at the `server.py` handler layer only (not inside
  `db.py`/`cache.py`/`retrieval.py` internals) so those modules stay free
  of a cross-cutting concern - the root span per request plus child spans
  for the rate-limit check, auth lookup, cache lookup, graph traversal,
  the memory write, and the approval-gate review together already show
  the whole journey without needing tracing wired through every layer.
  `client.py` propagates the current span's W3C `traceparent` header on
  every call; wrapping `retrieve_context`+`log_session` in
  `client.traced_session(...)` links a whole agent session into one
  trace. `opentelemetry-api`/`-sdk`/`-exporter-otlp-proto-http` are
  lazily imported (same pattern as `psycopg2`/`redis`) - a plain `python3
  server.py` with none of them installed still runs, with `TRACER`
  becoming a tiny local no-op shim. SIGTERM is converted into the same
  graceful-shutdown path as Ctrl+C and flushes the OTLP exporter's batch
  on the way out, so a container being stopped doesn't silently drop
  whatever hadn't been exported yet.
- **Trace sampling (`AGENTHIVE_TRACE_SAMPLE_RATIO`, `tracing.py`).** 1.0
  (default) traces every request. Set below 1.0 and
  `ParentBased(TraceIdRatioBased(ratio))` makes the sampling decision once
  at the root span; every child span inherits it, so a sampled-in trace is
  never left with only some of its spans exported. Head-based, not
  tail-based - the decision is made before the request runs, so it can't
  react to "actually this one was slow, keep it" the way a tail sampler
  could. Fine for the stated goal (control cost/volume at high throughput,
  not surface anomalies) - a team that needs the latter should put a
  tail-sampling collector (Jaeger, an OTel Collector) in front of the OTLP
  endpoint rather than rebuilding that logic here.
- **Containerization.** The Dockerfile installs `prometheus_client` (hard
  dependency), plus `psycopg2-binary`, `redis`, the OpenTelemetry
  packages, and `PyJWT[crypto]` (all optional, lazily imported) at build
  time, and its `HEALTHCHECK` is TLS-aware. `docker-compose.yml` has
  `postgres`, `redis`, and `jaeger` Compose profiles alongside the default
  (SQLite only, single container).
- **Helm chart.** `helm/agenthive/` — SQLite+single-replica or
  Postgres+N-replicas (the chart refuses to render SQLite with more than
  one replica, and refuses `autoscaling.enabled` without Postgres, rather
  than let either fail confusingly at runtime — see
  `templates/_helpers.tpl`'s `agenthive.validate`), optional Redis wiring,
  Ingress or native TLS, optional Azure AD, and either a `ServiceMonitor`
  (Prometheus Operator) or plain `prometheus.io/*` pod annotations for
  metrics scraping. See `helm/agenthive/README.md`.

## Open questions

- **No L1→L2→L3 promotion worker.** Everything sits at whatever tier it
  was written at. This needs an LLM-driven summarization policy designed
  against real usage data this service doesn't have yet - building it now
  means guessing at what a good summary looks like. Revisit once there's
  enough real L1 volume to know what a good policy looks like.
- **No LLM-request proxy.** Not a missing feature - see "Why not build an
  LLM-request proxy" above. Building one would reverse the central
  decision this whole document argues for; closing it as a checklist item
  would mean undoing the reason AgentHive is shaped the way it is, not
  finishing it.

Both are deliberately deferred until there's real usage data to design
against, rather than built speculatively now.
