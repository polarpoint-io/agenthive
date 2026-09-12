# Onboarding a team onto AgentHive

A step-by-step walkthrough for getting a real team from zero to "our
agents share reviewed memory, and we can watch it happen." ~15-20 minutes
if you already have somewhere to run a container. Written the way you'd
onboard a team onto a shared service, not the way you'd read an API
reference - see [`README.md`](README.md) for the concept overview and
request/response shapes, [`DEPLOYMENT.md`](DEPLOYMENT.md) for
infrastructure detail, and [`ADR.md`](ADR.md) for why any of this is
shaped the way it is.

> 💡 If you only want to kick the tires alone before rolling this out to
> a team, skip straight to Step 1's "Path A" and Steps 2-6 - you don't
> need Docker, Postgres, or a second person for any of that.

## Prerequisites

| Need | Why |
|---|---|
| Python 3.9+ (Path A) OR Docker (Path B) OR a Kubernetes cluster + Helm 3 (Path C) | To run the service itself |
| `curl` or Python for the walkthrough below | The API is plain HTTP + JSON; no CLI to install |
| Nothing else | SQLite by default - Postgres/Redis are opt-in, see Step 7 |

## Step 1: Get AgentHive running

Pick the path that matches where this is headed. All three run the exact
same code; only the process/packaging differs.

**Path A - just try it, on your own machine:**

```bash
git clone <your-fork-of-this-repo> agenthive && cd agenthive
pip install -r requirements.txt   # optional extras only - see requirements.txt
python3 server.py --port 8790
```

**Path B - a single host, for a small team:**

```bash
docker compose up -d --build
curl http://localhost:8790/healthz   # {"status": "ok"}
```

**Path C - Kubernetes, for a team that already lives there:**

```bash
helm install agenthive helm/agenthive
kubectl port-forward svc/agenthive 8790:8790
```

That's the chart's own default image (`ghcr.io/polarpoint-io/agenthive`).

See [`helm/agenthive/README.md`](helm/agenthive/README.md) for
Postgres/Redis/Ingress/TLS on this path - the defaults here are SQLite,
single replica, no Ingress, which is enough to keep following along.

The rest of this doc assumes `AGENTHIVE_URL=http://localhost:8790`
(adjust for your host/port).

## Step 2: Create your team

One-time, unauthenticated call. This is the *only* endpoint that doesn't
need a token, because it's what creates the first one:

```bash
curl -sX POST $AGENTHIVE_URL/teams \
  -H 'Content-Type: application/json' \
  -d '{"name": "Platform Team", "owner_name": "you"}'
```

```json
{
  "team": {"id": "8f2a1c9d0b3e", "name": "Platform Team"},
  "user": {"id": "1a2b3c4d5e6f", "team_id": "8f2a1c9d0b3e",
           "name": "you", "role": "admin", "token": "ah-9f...c2"}
}
```

> ⚠️ **`token` is shown exactly once, right here.** It's hashed at rest
> (see `auth.py`) - there is no "forgot my token" recovery. Save it in a
> password manager or secret store now. If you lose it, an existing admin
> has to `rotate_token` you a new one (Step 3), or you re-run `POST
> /teams` and start a new team.

Save `team.id` and `user.token` - every call from here on needs both.

## Step 3: Add your teammates

Each person (or each service account an agent runs as) gets their own
token, not a shared secret - this is the thing v0 of this project got
wrong and v1 fixed (see `ADR.md`). Role is `admin` (can review pending
memory, manage users, configure auto-approve rules) or `member` (can
read/write memory, manage agents/tasks):

```bash
curl -sX POST $AGENTHIVE_URL/teams/$TEAM_ID/users \
  -H "X-API-Key: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "alice", "role": "member"}'
```

Hand `alice`'s returned token to Alice, not to yourself - it's shown once
here too. Repeat for everyone on the team. `admin.list_users()` /
`GET /teams/{id}/users` shows who's set up so far (never shows tokens
again, by design).

## Step 4: Register agents and tasks (optional)

Skip this if your team doesn't care about attributing memory to a
specific agent role or task yet - `agent_id`/`task_id` are optional on
every write. Add them later once you know you want the filter:

```bash
curl -sX POST $AGENTHIVE_URL/teams/$TEAM_ID/agents \
  -H "X-API-Key: $MEMBER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "bug-fix engineer", "description": "fixes prod incidents"}'
```

## Step 5: Point your coding agent at it

Add an instruction block to whatever your agent already reads at session
start (`CLAUDE.md`, `.cursor/rules`, a system prompt) telling it to call
the two functions in `client.py`:

```python
from client import TeamMemoryClient

client = TeamMemoryClient("$AGENTHIVE_URL", "$MEMBER_TOKEN", "$TEAM_ID")

# at the start of the session:
context = client.retrieve_context("Postgres connection pool exhaustion", hops=2)
# context["neighborhood"] -> bounded list of approved memory nodes to fold
# into your prompt; context["approx_tokens"] -> what that actually costs

# ... the agent does its actual work ...

# at the end of the session:
client.log_session(
    title="Postgres connection pool exhaustion",
    body="Root cause: pgbouncer max_client_conn too low. Fix: raised to 400.",
    tags=["postgres", "incident"],
)
```

That's the entire integration surface. There is no proxy to route
through and no per-client config format to get right (see `ADR.md`'s
"Why not build an LLM-request proxy") - any agent that can make an HTTP
call, or `import client`, can do this.

> 💡 Wrap both calls in `with client.traced_session("your-session-name"):`
> if you've turned tracing on (Step 8) and want to see them as one
> connected trace instead of two separate ones.

## Step 6: Review your first pending memory

`log_session` above wrote a node with `status: "pending"` - invisible to
everyone's `retrieve_context` until an admin reviews it. Open the review
UI:

```
$AGENTHIVE_URL/ui
```

Enter the base URL, your team ID, and an **admin** token. You'll see a
pending queue (title, body preview, agent/task if set) with Approve /
Reject buttons, plus tabs for users, auto-approve rules, and the metrics
summary (Step 8). Approve the node you just wrote - it's now visible to
every teammate's `retrieve_context` calls, scoped to this team only.

Scripting it instead: `admin.list_pending()` / `admin.approve(node_id)` -
see README.md's "Reviewing pending memory".

## Step 7: Turn on auto-approve rules (once you know your noise)

After a week or two you'll notice patterns in what gets rejected vs.
rubber-stamped. Skip the queue for the rubber-stamped kind:

```bash
curl -sX POST $AGENTHIVE_URL/teams/$TEAM_ID/auto-approve-rules \
  -H "X-API-Key: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"match_tag": "ci-noise"}'
```

Every auto-approved node still carries an audit trail (`auto_approved`,
`rule_id`) - it skipped the queue, not the record of *why*.

> ⚠️ Running more than one AgentHive replica? Set `DATABASE_URL`
> (Postgres) and `REDIS_URL` (Redis) - see Step 9 below and
> `DEPLOYMENT.md`'s "Shared rate limiting and caching across replicas."
> Without Redis, the rate limiter and retrieval cache are per-replica,
> not per-team - a soft guard, not a hard one, and reuse across
> teammates only counts within whichever replica happened to serve them.

## Step 8: Watch it work

This is the part a README can't show you, because it needs your team's
actual traffic. Two complementary views:

**Metrics (the trend over weeks):**

```bash
curl $AGENTHIVE_URL/metrics | grep agenthive_tokens_avoided_total
curl -H "X-API-Key: $ADMIN_TOKEN" $AGENTHIVE_URL/teams/$TEAM_ID/metrics/summary
```

Import `observability/grafana-dashboard.json` into Grafana for the
graphed version - tokens avoided, cache hit rate, cross-teammate reuse,
graph growth. Read `observability/README.md` first: it's explicit about
what "tokens avoided" does and doesn't mean (this service never calls an
LLM, so it's "not sent because retrieval was scoped," not a real billing
number).

**Tracing (one call's actual journey):**

Turn it on with zero infrastructure first, just to see it work:

```bash
AGENTHIVE_TRACING_CONSOLE=true python3 server.py --port 8790
```

Do a `log_session` + `retrieve_context` call (Step 5) and watch the
terminal - each finished span prints as one line of JSON: the rate-limit
check, the auth lookup, the cache lookup (hit or miss), the graph
traversal, the approval-gate write. That's "the entire journey mapped,"
literally - no proxy needed to see it, because every hop already goes
through this one small service.

For the real thing, point it at Jaeger instead - the web UI for actually
browsing traces:

```bash
docker compose --profile jaeger up -d      # or: helm's tracing.otlpEndpoint
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
python3 server.py --port 8790
open http://localhost:16686                 # Jaeger UI - pick service "agenthive"
```

Wrap `retrieve_context` + `log_session` in `with client.traced_session():`
(Step 5) to see a whole agent session as one trace instead of two.

## Troubleshooting / FAQ

**Q: My agent's `retrieve_context` isn't returning something I just wrote.**
A: It's still `pending`. Check `GET /teams/{id}/memory/pending` or the
review UI - nothing shows up in retrieval until an admin approves it (or
an auto-approve rule matches). This is the guardrail working as
designed, not a bug.

**Q: I get `401: missing X-API-Key header` or `invalid or revoked API key`.**
A: Either the header's missing, the token was rotated/revoked since you
saved it, or you copy-pasted a stray space/newline. Tokens start with
`ah-`; if yours doesn't, it's not a valid AgentHive token.

**Q: I get `403: this API key does not belong to this team`.**
A: The token is real but for a different team than the `{team_id}` in
the URL. Double-check `TEAM_ID` matches the team that token was created
under.

**Q: `agenthive_tokens_avoided_total` / the metrics summary is all zeros.**
A: Either no `retrieve_context` calls have happened yet, or the anchor
you're retrieving doesn't match any approved node's title (retrieval
returns an empty neighborhood, not an error, on a miss - check
`neighborhood_count` in the response).

**Q: I'm getting 429s almost immediately.**
A: `AGENTHIVE_RATE_LIMIT_PER_MINUTE` (default 120) is shared across
*all* calls from one API key/IP within the window - a tight local test
loop can hit it fast. Raise it, or run more than one replica **with
Redis** (see Step 7's warning) rather than assuming replicas divide the
limit for you (they don't, without Redis - see `DEPLOYMENT.md`).

**Q: Traces aren't showing up in Jaeger.**
A: Check, in order: (1) `AGENTHIVE_TRACING_CONSOLE=true` shows spans in
the server's own stdout at all - if not, tracing isn't enabled, check
`OTEL_EXPORTER_OTLP_ENDPOINT`/`AGENTHIVE_TRACING_CONSOLE` are actually
set in the process's environment; (2) the endpoint is reachable from
where `server.py` runs (`curl $OTEL_EXPORTER_OTLP_ENDPOINT` should at
least connect, not necessarily 200); (3) Jaeger's own UI search defaults
to the last hour and a specific service name (`agenthive`, or whatever
`OTEL_SERVICE_NAME` you set) - make sure both match.

**Q: Do I need Postgres/Redis/tracing to get started?**
A: No. Every one of them is opt-in and the service is fully functional
without any of them - SQLite, in-memory rate limiting/caching, and no
tracing are all sane defaults for a single team on a single node. Add
each only when its specific reason applies (see `DEPLOYMENT.md`'s
"Known limitations" and this doc's Step 7).

## Cleanup

```bash
docker compose down -v          # -v also drops the named volumes (data!)
# or
helm uninstall agenthive
```

## See also

- [`README.md`](README.md) - concepts, full request/response shapes, all endpoints
- [`DEPLOYMENT.md`](DEPLOYMENT.md) - Docker/Kubernetes/Postgres/Redis/TLS in depth
- [`ADR.md`](ADR.md) - why this is shaped the way it is, and what it isn't
- [`helm/agenthive/README.md`](helm/agenthive/README.md) - Kubernetes-specific walkthrough
- [`observability/README.md`](observability/README.md) - what each metric actually claims
