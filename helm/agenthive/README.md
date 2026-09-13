# AgentHive Helm chart

## Quick start (SQLite, single replica, no Ingress)

```bash
helm install agenthive helm/agenthive

kubectl port-forward svc/agenthive 8790:8790
open http://127.0.0.1:8790/ui
```

This is the whole thing for a small team on a single cluster: one pod,
one PVC, SQLite, and the chart's own default image
(`ghcr.io/polarpoint-io/agenthive`, built and released alongside the
chart - see `.github/workflows/release.yml`/`images.yml`). `helm test
agenthive` hits `/healthz` and `/readyz` to confirm it came up. Point at
a different registry with `--set image.registry=... --set
image.repository=...`.

## Postgres + Redis + Ingress (multiple replicas)

SQLite is a single file - this chart refuses to render more than one
replica against it (see `templates/_helpers.tpl`'s `agenthive.validate`).
Once you need more than one replica, switch to Postgres, and add Redis
so the rate limiter and retrieval cache are shared instead of
per-replica:

```bash
helm install agenthive helm/agenthive \
  -f helm/agenthive/values-postgres-example.yaml \
  --set database.postgres.existingSecret=agenthive-db \
  --set redis.existingSecret=agenthive-redis \
  --set ingress.host=agenthive.yourcompany.com
```

Create the secrets first (don't pass real credentials via `--set` in a
real deployment - they end up in shell history and Helm's release
metadata in plaintext):

```bash
kubectl create secret generic agenthive-db \
  --from-literal=database-url='postgresql://user:pass@your-postgres:5432/agenthive'

kubectl create secret generic agenthive-redis \
  --from-literal=redis-url='redis://your-redis:6379/0'
```

`values-postgres-example.yaml` also turns on `autoscaling` (HPA on CPU,
requires `database.type=postgres` for the same single-writer reason) and
`podDisruptionBudget`.

## TLS

Two independent options - pick whichever fits your setup, or neither if
your mesh/network already handles it:

- **Ingress TLS** (the normal path): `ingress.tls.enabled=true` +
  `ingress.tls.secretName` (or let cert-manager manage it via
  `ingress.annotations`, as in `values-postgres-example.yaml`).
- **Native pod TLS**: `tls.enabled=true` + `tls.secretName` pointing at a
  `kubernetes.io/tls` Secret. The pod serves HTTPS directly on the same
  port - use this when there's no Ingress in front, or a service mesh
  expects an HTTPS backend. The chart wires the cert into
  `AGENTHIVE_TLS_CERT_FILE`/`AGENTHIVE_TLS_KEY_FILE` and switches the
  liveness/readiness probes to HTTPS automatically.

## Metrics

`metrics.enabled=true` (the default) exposes `GET /metrics`. Two ways to
get it into Prometheus:

- **Prometheus Operator**: `metrics.serviceMonitor.enabled=true` creates
  a `ServiceMonitor` (requires the operator's CRDs in-cluster).
- **Plain Prometheus**: leave `serviceMonitor.enabled=false` (default) -
  pods carry `prometheus.io/scrape`, `prometheus.io/port`,
  `prometheus.io/path` annotations for a Prometheus configured to
  discover pods that way.

See `../../observability/README.md` for the metrics reference and a
starter Grafana dashboard.

## Tracing

Off by default - `tracing.otlpEndpoint` (unset) means the pod pays
nothing for this. Point it at any OTLP/HTTP-compatible backend already
running in (or reachable from) your cluster:

```bash
helm upgrade agenthive helm/agenthive --reuse-values \
  --set tracing.otlpEndpoint=http://jaeger-collector.observability:4318
```

`tracing.console=true` prints each finished span as JSON to the pod's
stdout instead (or as well) - useful for confirming spans are actually
being produced (`kubectl logs`) before wiring up a real backend, with no
extra infrastructure. See `../../ONBOARDING.md`'s "Watch it work" step
and `../../observability/README.md` for what gets instrumented (the
rate-limit check, auth lookup, cache lookup, graph traversal, and the
approval gate itself) and why it's a trace per call, not a proxy.

`tracing.sampleRatio` (default `1.0` - trace everything) is a head-based
sampling knob for a high-throughput deployment: turn it down instead of
paying for and storing a trace per request. See `../../ADR.md`'s
"Reliability and observability" section and `../../DEPLOYMENT.md`'s
"Known limitations" for why it's head-based, not tail-based.

## Token expiry

`auth.tokenTtlSeconds` (default `0` - never expires, the original
behavior). Set it and every token minted or rotated from then on expires
automatically; an expired token fails auth exactly like a revoked one.
Existing tokens keep working until next rotated - this doesn't
retroactively lock a team out. See `../../ADR.md`'s "Authentication" section.

## Azure AD sign-in (humans only)

Off by default. Agents keep using their personal tokens either way - this
only adds an optional Microsoft sign-in button to the review UI:

```bash
helm upgrade agenthive helm/agenthive --reuse-values \
  --set auth.azureAd.enabled=true \
  --set auth.azureAd.tenantId=... \
  --set auth.azureAd.clientId=... \
  --set auth.azureAd.existingSecret=agenthive-azure-ad \
  --set auth.azureAd.redirectUri=https://agenthive.yourcompany.com/auth/azure/callback
```

Create the secret first (same reasoning as the database/redis secrets
above - don't pass a real client secret via `--set`):

```bash
kubectl create secret generic agenthive-azure-ad \
  --from-literal=client-secret='...'
```

Or set `auth.azureAd.clientSecret` directly and let the chart create the
Secret for you (fine for a quick test, not for a real deployment - same
tradeoff as `database.postgres.url`/`redis.url` above). `redirectUri`
must exactly match a Redirect URI registered on the Azure AD App
Registration. Signing in with Microsoft never auto-creates an AgentHive
user - an admin links an existing one to an Azure AD object id from the
Users card, or `POST /teams/{id}/users/{id}/link-azure`. See
`../../ADR.md`'s "Authentication" section and `../../README.md`'s "Azure
AD sign-in" section.

## Standalone review UI

Off by default - `GET /ui` on the app's own Service keeps working with
nothing else to install. Turn this on if you'd rather the review UI not
share an origin/port with the API (its own Ingress host, its own scaling,
a stricter NetworkPolicy on the API pod, etc.):

```bash
helm upgrade agenthive helm/agenthive --reuse-values \
  --set ui.enabled=true \
  --set ui.apiUrl=https://agenthive.yourcompany.com \
  --set ui.publicUrl=https://agenthive-ui.yourcompany.com \
  --set ui.ingress.enabled=true \
  --set ui.ingress.host=agenthive-ui.yourcompany.com
```

This renders a second, independent Deployment/Service/Ingress
(`templates/ui-*.yaml`) from `ghcr.io/polarpoint-io/agenthive-ui` (the
image built from the repo root's `ui/Dockerfile`, same release cadence
as the main image - see `.github/workflows/images.yml`). `ui.publicUrl`
is what makes Azure AD sign-in and cross-origin API calls work once
you've split it out this way - it sets the app's `AGENTHIVE_UI_URL` and
`AGENTHIVE_CORS_ORIGIN` for you; see `../../README.md`'s "Splitting the
review UI into its own container" for what those actually do.

## What's deliberately NOT in this chart

- **No bundled Postgres or Redis.** Point `database.postgres.url` /
  `redis.url` (or the `existingSecret` variants) at ones you already run
  - a Helm chart that bundles a database as a subchart is usually the
  wrong default for anything beyond a demo. Use Bitnami's
  `postgresql`/`redis` charts (or your cloud provider's managed
  offering) alongside this one if you don't have either yet.
- **No bundled Jaeger (or any other tracing backend).** Same reasoning -
  `tracing.otlpEndpoint` points at one you already run. Jaeger's own
  Helm chart (or `docker compose --profile jaeger` for local use, see
  the top-level `docker-compose.yml`) stands one up in about a minute if
  you don't have one yet.
- **No NetworkPolicy.** Add one in your own umbrella chart / GitOps repo
  if your cluster requires default-deny - it depends too much on your
  CNI and existing policies to guess correctly here.

## Values reference

See `values.yaml` - every key has an explanatory comment. The ones you
are most likely to touch on a real install: `image.repository`/`tag`,
`database.type`, `database.postgres.existingSecret`,
`redis.existingSecret`, `ingress.*`, `resources`, `autoscaling.*`,
`ui.*` (see "Standalone review UI" above).
