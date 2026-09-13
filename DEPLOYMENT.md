# Deployment

## Kubernetes (Helm) - the recommended path for "real" deployments

```bash
helm install agenthive helm/agenthive
```

The chart defaults to the published image
(`ghcr.io/polarpoint-io/agenthive`) at the chart's own `appVersion` - image
and chart are built and released together, see
`.github/workflows/release.yml` and `images.yml`. Point at a different
registry (an internal mirror, say) with `--set image.registry=... --set
image.repository=...`.

See `helm/agenthive/README.md` for the full walkthrough: SQLite vs.
Postgres, adding Redis for multi-replica rate limiting/caching, TLS
(native or via Ingress), and wiring Prometheus/Grafana. The chart
enforces the constraints below itself (e.g. it refuses to render more
than one replica against SQLite) rather than letting you discover them
at runtime.

The review UI is served at the app's own `/ui` by default - no separate
deploy. Set `ui.enabled=true` (plus `ui.publicUrl`/`ui.ingress.host`) to
run it as its own Deployment/Service/Ingress instead - see README.md's
"Splitting the review UI into its own container" and this chart's own
`values.yaml` `ui.*` block.

## Docker Compose - single host

```bash
docker compose up -d --build
curl http://localhost:8790/healthz
```

The database lives in the `agenthive_data` named volume, so it survives
container restarts and rebuilds. Back it up by copying that volume
(`docker run --rm -v agenthive_data:/data -v $PWD:/backup alpine \
tar czf /backup/agenthive-backup.tgz -C /data .`), or just copy the
SQLite file directly if you're not using Docker.

Add Postgres (`docker compose --profile postgres up -d`, then uncomment
`DATABASE_URL` on the `app` service) once more than one instance writes
concurrently, and Redis (`docker compose --profile redis up -d`, then
uncomment `REDIS_URL`) once you run more than one instance at all - see
"Known limitations" below for why that second one matters.

Add `docker compose --profile split-ui up -d` to run the review UI as
its own container (port 8080) instead of at the app's `/ui` - see
README.md's "Splitting the review UI into its own container".

## Running without Docker

```bash
python3 server.py --port 8790 --db agenthive.db
```

Or with Postgres, via environment variable instead of `--db`:

```bash
DATABASE_URL=postgresql://user:pass@host:5432/dbname python3 server.py
```

## systemd unit (bare-metal / VM)

```ini
[Unit]
Description=AgentHive
After=network.target

[Service]
Type=simple
User=agenthive
WorkingDirectory=/opt/agenthive
EnvironmentFile=/opt/agenthive/.env
ExecStart=/usr/bin/python3 server.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

Copy `.env.example` to `.env` there and adjust `AGENTHIVE_HOST`,
`AGENTHIVE_PORT`, `DATABASE_URL` / `AGENTHIVE_DB_PATH`, `REDIS_URL`, and
the rate-limit / logging / TLS settings.

## TLS

Two options, same as the Helm chart:

- **Reverse proxy in front** (most deployments): terminate TLS in Caddy,
  nginx, or your cloud load balancer, and leave AgentHive on plain HTTP
  behind it. Caddy example:

  ```
  agenthive.your-internal-domain.com {
      reverse_proxy 127.0.0.1:8790
  }
  ```

- **Native TLS**: set `AGENTHIVE_TLS_CERT_FILE` and `AGENTHIVE_TLS_KEY_FILE`
  and the process serves HTTPS directly (`server.py` wraps the socket in
  an `ssl.SSLContext` - see `tests/test_tls.py`). Use this when there's
  no proxy in front at all, or a service mesh sidecar expects an HTTPS
  backend.

## Shared rate limiting and caching across replicas (Redis)

Running more than one AgentHive instance (multiple Docker Compose
replicas, or a Helm deployment with `replicaCount` > 1) needs
`DATABASE_URL` pointed at Postgres (SQLite is one file, one writer) -
and should also get `REDIS_URL` set, or two things silently become
per-replica instead of shared:

- **Rate limiting**: without Redis, each replica enforces
  `AGENTHIVE_RATE_LIMIT_PER_MINUTE` independently, so N replicas add up
  to N times the intended limit for any client hitting them round-robin.
  With `REDIS_URL` set, `auth.RedisRateLimiter` shares one counter across
  every replica (see `tests/test_redis_backends.py`'s
  `test_redis_rate_limit_shared_state`, which runs two real server
  processes against one Redis and confirms the limit is shared, not
  doubled).
- **Retrieval cache**: without Redis, a cache hit on one replica is
  invisible to the others - the same context gets re-traversed once per
  replica it happens to land on. With Redis, a hit on any replica is a
  hit for all of them, and `agenthive_cross_user_cache_hits_total`
  reflects reuse across the whole team, not just within one pod's
  lifetime.

Both fall back to in-memory, per-process versions automatically when
`REDIS_URL` is unset - correct behavior for a single replica, and also
what happens transparently if Redis becomes unreachable (both
implementations degrade to "allow"/"miss" rather than fail the request -
see `auth.py` / `cache.py`).

## Metrics

`GET /metrics` (Prometheus exposition format) is on by default. See
`observability/README.md` for what each metric actually claims (and
doesn't), and `observability/grafana-dashboard.json` for a starter
dashboard - token reduction, cache reuse across teammates, and graph
growth over time, the parts of this service's value that a single API
response can't show on its own.

## Tracing

Off by default - `OTEL_EXPORTER_OTLP_ENDPOINT` and `AGENTHIVE_TRACING_CONSOLE`
are both unset, so a fresh install pays nothing for this. Set either to
map the write -> review -> retrieve journey as OpenTelemetry traces:

- **`AGENTHIVE_TRACING_CONSOLE=true`** prints each finished span as one
  line of JSON to stdout. No infrastructure, no exporter to stand up -
  the fastest way to confirm the instrumentation is actually working
  (`docker logs` / `kubectl logs`), and what `tests/test_tracing.py`
  uses.
- **`OTEL_EXPORTER_OTLP_ENDPOINT`** (the standard OTel env var, no
  `AGENTHIVE_` prefix) sends spans to any OTLP/HTTP-compatible backend.
  `docker compose --profile jaeger up -d` runs Jaeger's all-in-one image
  locally (web UI on :16686, OTLP/HTTP receiver on :4318); the Helm
  chart's `tracing.otlpEndpoint` value wires the same thing in
  Kubernetes without bundling a tracing backend into the chart (same
  reasoning as Postgres/Redis - see `helm/agenthive/README.md`).

Both `psycopg2`/`redis`-style optional dependencies apply here too:
`opentelemetry-api`/`-sdk`/`-exporter-otlp-proto-http` are only imported
if tracing is actually turned on, and the Docker image installs them
regardless so there's nothing to rebuild when you flip the switch.

`client.py` propagates the current trace's `traceparent` header on every
call, so a server's spans for one call are children of that call's
client-side span rather than the start of a disconnected trace. Wrap
`retrieve_context(...)` and `log_session(...)` in
`with client.traced_session("..."):` to link a whole agent session (both
calls, and everything the server did for each) into one trace instead of
two. See `tracing.py`'s docstring for the full design and
`ONBOARDING.md`'s "Watch it work" step for a guided first look.

On graceful shutdown (SIGTERM - how Docker/Kubernetes stop a container,
or Ctrl+C) `server.py` flushes any spans the OTLP exporter's batch
processor hasn't sent yet before exiting, so a rolling deploy doesn't
silently drop the last few seconds of traces.

## Health checks

- `GET /healthz` - liveness. Always 200 if the process is up.
- `GET /readyz` - readiness. Checks the database is reachable; 503 if not.

Point your load balancer / orchestrator's health check at `/healthz` for
liveness and `/readyz` for readiness. The Helm chart wires both in
automatically, using HTTPS for them when native TLS is enabled.

## Environment variables

See `.env.example` for the full list with defaults. The ones that matter
most for a production deploy:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Set to a `postgresql://` URL to use Postgres instead of SQLite (required for >1 replica) |
| `AGENTHIVE_DB_PATH` | SQLite file path, used only when `DATABASE_URL` is unset |
| `REDIS_URL` | Shares rate limiting + retrieval cache across replicas (recommended for >1 replica) |
| `AGENTHIVE_TLS_CERT_FILE` / `AGENTHIVE_TLS_KEY_FILE` | Serve HTTPS natively instead of via a reverse proxy |
| `AGENTHIVE_HOST` / `AGENTHIVE_PORT` | Bind address |
| `AGENTHIVE_RATE_LIMIT_PER_MINUTE` | Per-API-key request budget (0 disables) |
| `AGENTHIVE_CACHE_TTL_SECONDS` | Retrieval cache TTL (invalidated early on any write/review for that team regardless) |
| `AGENTHIVE_METRICS_ENABLED` | Set to `false` to disable `/metrics` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Sends traces to an OTLP/HTTP backend (Jaeger, Tempo, ...); unset means no tracing |
| `AGENTHIVE_TRACING_CONSOLE` | `true` prints each finished span as JSON to stdout - no backend needed |
| `AGENTHIVE_TRACE_SAMPLE_RATIO` | Head-based sampling ratio, `0.0`-`1.0` (default `1.0` - trace everything) |
| `AGENTHIVE_TOKEN_TTL_SECONDS` | Tokens minted/rotated after this is set expire after this many seconds (default `0` - never) |
| `AGENTHIVE_AZURE_TENANT_ID` / `_CLIENT_ID` / `_CLIENT_SECRET` / `_REDIRECT_URI` | Azure AD sign-in for the review UI, humans only - off unless all four are set (see ADR.md's "Authentication" section) |
| `AGENTHIVE_AZURE_SESSION_TTL_SECONDS` | How long a session minted by Azure sign-in lasts (default `3600`) |
| `AGENTHIVE_LOG_JSON` | `true` for JSON lines (recommended behind a log aggregator) |

## Known limitations

Gaps this project used to carry and has since closed (see ADR.md for the
reasoning behind each): TLS natively, rate limiting/caching shared across
replicas when `REDIS_URL` is set, token expiry via
`AGENTHIVE_TOKEN_TTL_SECONDS`, and head-based trace sampling via
`AGENTHIVE_TRACE_SAMPLE_RATIO`. What's still true:

- **The Redis-backed rate limiter is a fixed window, not a true sliding
  window** (one `INCR` + one `EXPIRE` per call, see `auth.py`) - up to a
  2x burst is possible right at a window boundary. An accepted
  approximation for a request-rate guard, not something to rely on for
  hard billing-level quotas.
- **`/metrics` has no auth.** Standard for Prometheus endpoints, but it
  means anything that can reach the pod/port can read it. Restrict at
  the network layer (NetworkPolicy, security group) rather than relying
  on obscurity - it carries no per-team or per-user data regardless
  (see `observability/README.md`).
- **Trace sampling is head-based, not tail-based.** The decision is made
  before the request runs, so a sampler set below `1.0` can't react to
  "actually this one was slow, keep it" - it can only control volume/cost
  at a fixed rate. A team that needs tail-based sampling should put a
  collector capable of it (an OTel Collector, Jaeger's own) in front of
  the OTLP endpoint rather than expecting this ratio to do that job.
- **Linking an Azure AD account is a manual admin step, on purpose.**
  Signing in with Microsoft never auto-creates or auto-links an AgentHive
  user (see ADR.md's "Authentication" section) - a person who hasn't been
  linked yet sees their Azure object id and has to ask an admin to run
  `link-azure`. This is deliberately not self-service; there is no
  "request access" flow.
