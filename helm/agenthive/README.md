# AgentHive Helm chart

## Quick start (SQLite, single replica, no Ingress)

```bash
helm install agenthive helm/agenthive \
  --set image.repository=your-registry/agenthive \
  --set image.tag=1.0.0

kubectl port-forward svc/agenthive 8790:8790
open http://127.0.0.1:8790/ui
```

This is the whole thing for a small team on a single cluster: one pod,
one PVC, SQLite. `helm test agenthive` hits `/healthz` and `/readyz`
to confirm it came up.

## Postgres + Redis + Ingress (multiple replicas)

SQLite is a single file - this chart refuses to render more than one
replica against it (see `templates/_helpers.tpl`'s `agenthive.validate`).
Once you need more than one replica, switch to Postgres, and add Redis
so the rate limiter and retrieval cache are shared instead of
per-replica:

```bash
helm install agenthive helm/agenthive \
  -f helm/agenthive/values-postgres-example.yaml \
  --set image.repository=your-registry/agenthive \
  --set image.tag=1.0.0 \
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
`redis.existingSecret`, `ingress.*`, `resources`, `autoscaling.*`.
