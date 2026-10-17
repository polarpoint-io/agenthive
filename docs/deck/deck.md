---
title: "AgentHive - shared memory for a team of coding agents"
marp: true
theme: agenthive
transition: fade
size: "16:9"
lang: en-GB
paginate: true
header: "AgentHive"
footer: "polarpoint-io/agenthive"
---

<!-- _class: lead -->
<!-- _paginate: false -->

# AgentHive

## Shared, reviewed memory for a team of coding agents

### How it works, and where the time and tokens actually go

---

# The problem it solves

Every agent session starts from nothing.

One engineer's agent spends forty minutes finding that pgbouncer's
`max_client_conn` was the cause. Next week, a teammate's agent spends
forty minutes finding the same thing.

The knowledge existed. It just had nowhere to live that the second
agent could reach.

> Per-agent memory makes an agent better at repeating itself.
> Team memory makes the **team** better.

---

# What it is

A **team / agent / task graph** of memory nodes, retrieved by walking
outward from an anchor.

Three properties that follow from that shape:

- **Scoped.** Memory never crosses a team boundary — enforced at the query layer.
- **Reviewed.** Everything an agent writes lands `pending` and is invisible until a human approves it.
- **Bounded.** Retrieval returns a neighbourhood and tells you what it costs, rather than a dump.

---

# Two calls, not every call

```python
# at the start of a session
context = client.retrieve_context("Postgres connection pool exhaustion")

# at the end
client.log_session(title="...", body="...", tags=["bugfix"])
```

There is **no proxy** in front of your LLM traffic. Nothing intercepts
or enriches every request.

That is a deliberate design decision, not a missing feature — it is
what keeps the system auditable and keeps its cost predictable.

---

# The gate is the point

Every write starts `pending`. It is invisible to every other agent, in
every session, until an admin approves it.

That sounds like friction. It is the thing that makes shared memory
safe to use:

- An agent cannot poison what every other agent will read.
- A wrong note is caught once, by a person, instead of silently
  compounding across a team.
- `auto_approve_rules` skip review for the categories you have learned
  you never reject.

---

<!-- _class: stat -->

# 2,300 → 500

## tokens for the same retrieval, when the agent only needs one of five notes

---

# Where the tokens go

![center w:900](../diagrams/retrieval-disclosure.svg)

---

# Progressive disclosure

`retrieve_context` answers *"what is near this anchor, and here is all
of it"* — one round trip, nothing wasted on a second call.

`mode=index` runs the **same traversal** and withholds the bodies:
ids, titles, tiers, tags, and what each body would cost. The agent
picks; `fetch_nodes` returns only those.

One cache entry serves both modes, so an index call and a full call
share cache hits — including **cross-user** hits, which is the figure
that evidences shared rather than per-agent memory.

---

<!-- _class: caveat -->

# When it saves nothing

An agent that indexes five nodes and then fetches all five has saved
**nothing** and spent an extra round trip.

So the metrics do not pretend otherwise:

- `tokens_avoided_total` is computed against the full neighbourhood in **both** modes.
- `index_tokens_withheld_total` minus `fetch_tokens_served_total` is the real saving.
- If that difference trends to zero, the layer is costing a round trip and buying nothing — and the dashboard says so.

A number that can only go up is not a measurement.

---

# Where the time goes

Tokens are the visible cost. The expensive one is a retrieval that
returns **nothing**.

A miss costs a round trip, then the agent does the work from scratch,
then it often logs a note duplicating one already in the graph under a
different title. Three costs, one failed lookup.

```python
client.report_gaps()   # what this team keeps looking for and never finds
```

Called at session start, it turns "nobody wrote this" into a known,
countable, closeable thing.

---

# Sessions that never log

`log_session` only runs when the model remembers to call it. The
sessions most worth recording are the ones that **crashed**, hit the
context limit, or ended when someone closed the terminal.

A lifecycle hook fires regardless — but a hook cannot call an MCP tool,
so it needs its own route to the API. That is `agenthive-log`.

It is a **backstop, not a second writer**: when the agent logged
properly, it writes nothing. When it did not, it records that the gap
exists so a human can see it.

---

# The graph improves itself

![center w:860](../diagrams/improvement-loop.svg)

---

# Four proposals, no model

| From | Proposal |
|---|---|
| Nodes co-retrieved but not linked | the edge the graph is missing |
| Who retrieves what, how often | tier promotion and demotion |
| Anchors that keep missing | gaps worth filling |
| Your own approve / reject history | auto-approve rules you already justified |

Everything here is **arithmetic over rows**. Nothing calls an LLM —
memory that rewrites itself with a model is memory you can no longer
audit.

And nothing applies itself. The system proposes; a human disposes.

---

# Automating the gate — half of it

**Rejection is safe to automate.** Its failure mode is a lost note
somebody can write again.

**Approval is not.** Its failure mode is a bad memory visible to every
agent for as long as it stays approved.

So: duplicates and rule-matched writes are rejected outright, the queue
is **triaged** by what is waiting on it rather than by age, and approval
still needs a rule a human wrote.

A reserved tag like `security_alert` is beyond automation in *both*
directions — auto-approving one hides it, auto-rejecting one discards it.

---

# Proving it, including when it fails

| Metric | Answers |
|---|---|
| `gaps_closed_total` | did the graph learn what people kept asking for |
| `index_tokens_withheld` − `fetch_tokens_served` | the real token saving, net |
| `cross_user_cache_hits_total` | is this shared memory or per-agent memory |
| `oldest_pending_age_seconds` | is the review queue silently degrading everyone |
| `anchor_resolution_total{fallback}` | do titles match how people actually phrase things |

The second-most-likely failure is proposals raised and ignored. It
errors nowhere, so there is a query for it.

---

# What it deliberately is not

- **No LLM-request proxy.** Two calls per session, not every call.
- **No model in the write path.** No summarisation, no compression, no LLM reviewer.
- **No cross-team sharing.** Not configurable, enforced at the query layer.
- **No automatic approval.** Only on a rule a human wrote.

Each of these is a decision recorded in `ADR.md`, not a gap in the
roadmap.

---

# Running it

```bash
python3 server.py --port 8790 --db agenthive.db   # that's the whole deploy
docker compose up -d --build                       # single host
helm install agenthive helm/agenthive              # Kubernetes
```

SQLite by default, Postgres when you need concurrent writers, Redis once
you run more than one replica. Same schema, same code path.

Cursor and Claude Code get the tools natively through
[`agenthive-mcp`](https://github.com/polarpoint-io/agenthive-mcp).

---

<!-- _class: lead -->
<!-- _paginate: false -->

# Start here

## `ONBOARDING.md` — zero to a team using it

### github.com/polarpoint-io/agenthive
