# Team Memory Service (v0)

A shared, reviewed memory graph for a team of coding agents, built after
reading TencentDB Agent Memory's install doc and deciding to keep the parts
that earn their cost (a team/agent/task graph, retrieval-by-traversal) and
drop the part that doesn't (a proxy that intercepts and enriches every
single LLM request). See [`ADR.md`](ADR.md) for the full reasoning.

No dependencies beyond the Python standard library. Everything here is
`http.server` + `sqlite3`.

## What it actually does

- **Guardrail:** every memory an agent writes lands as `pending`. It is
  invisible to every other agent, in every session, until a human (or a
  policy you configure later) approves it.
- **Hive mind, scoped:** once approved, any agent under the same team can
  retrieve it. Memory never crosses a team boundary - enforced at the
  query layer, verified in `test_service.py`.
- **Token control, not token addition:** retrieval walks the approved
  graph outward from an anchor note (same traversal + hub-cutoff logic as
  `obsidian-vault/scripts/retrieve.py`), returning a bounded neighborhood
  and its approximate token count. Nothing is injected automatically.
- **Two calls, not every call:** an agent session calls `retrieve_context`
  once at the start and `log_session` once at the end. There is no proxy
  sitting in front of your LLM traffic.

## Running it

```bash
cd team-memory-service
python3 server.py --port 8790 --db team_memory.db
```

That's the whole deploy. One process, one SQLite file. No Docker, no
`.env`, no LLM API key required by this service itself (it stores and
retrieves memory; it does not call any model).

## Setting up a team

```python
from client import TeamMemoryClient, create_team

team = create_team("http://127.0.0.1:8790", "Platform Team")
# team = {"id": "...", "name": "Platform Team", "api_key": "tm-..."}
# save team["api_key"] somewhere real - there is no recovery flow in v0

client = TeamMemoryClient("http://127.0.0.1:8790", team["api_key"], team["id"])
agent = client.create_agent("bug-fix engineer", description="fixes prod incidents")
task = client.create_task("Q4 incident backlog")
```

## What an agent session actually calls

At the start of a session (in a `CLAUDE.md` / `.cursor/rules` instruction,
same pattern as the Obsidian vault harness-logging setup):

```python
context = client.retrieve_context("Postgres connection pool exhaustion", hops=2)
# context["neighborhood"] -> bounded list of approved memory nodes
# context["approx_tokens"] -> what this actually costs before you send it
```

At the end of a session:

```python
client.log_session(
    title="Postgres connection pool exhaustion",
    body="Root cause: pgbouncer max_client_conn too low. Fix: raised to 400.",
    agent_id=agent["id"],
    task_id=task["id"],
    tags=["postgres", "incident"],
    links=["Pgbouncer Runbook"],
)
```

This writes a `pending` node. It does not become part of the shared graph
until someone reviews it.

## Reviewing pending memory

```python
pending = client.list_pending()
for node in pending["pending"]:
    print(node["id"], node["title"], node["body"][:80])

client.approve(node_id, reviewed_by="surj")
# or
client.reject(node_id, reviewed_by="surj", reason="not ready, needs discussion")
```

There's no review UI in v0 - this is a script-and-terminal workflow. A
small panel (list pending, approve/reject with one click) is the obvious
next thing to build once there's enough real volume to make scrolling a
terminal output annoying.

## Testing

```bash
python3 server.py --port 8791 --db /tmp/test_team_memory.db &
python3 test_service.py
```

`test_service.py` is a scripted walkthrough against a real running server,
not a mocked unit test suite - it proves the properties that matter: the
approval gate actually gates, team isolation actually isolates, and the
hub-cutoff actually stops traversal where it's supposed to.

## What's deliberately not here yet

See `ADR.md`'s "Open questions" section in full, but the short version:

- **No L1 -> L2 -> L3 promotion worker.** Everything sits at whatever tier
  it was written at. Building an LLM-driven summarization pass against
  zero real usage data would be guessing at a policy nobody has needed yet.
- **No auto-approve policy.** Every node needs a human reviewer today.
  Fine at low volume; will need a policy engine once your team's actual
  pending queue tells you what's safe to fast-track.
- **Auth is one shared API key per team.** No per-user identity, no
  rotation, no expiry. Do not point this at anything with real
  access-control requirements without fixing this first.
- **SQLite, single node.** Fine for one team's real usage. If this needs
  to serve many teams at real concurrent write volume, move to Postgres -
  the schema in `db.py` translates directly, this isn't a rewrite.
- **No LLM-request proxy.** This was a deliberate choice, not a missing
  feature - see `ADR.md`'s "Why not build what TencentDB built" section.

## Files

- `db.py` - SQLite schema and storage layer
- `retrieval.py` - the traversal algorithm, scoped to team + approved-only
- `server.py` - the HTTP API (stdlib `http.server`)
- `client.py` - what an agent session actually calls
- `test_service.py` - end-to-end proof against a real running server
- `ADR.md` - why this is shaped the way it is, and what it deliberately isn't
