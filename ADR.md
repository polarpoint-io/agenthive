# ADR: Team Memory Service (v0)

Status: draft, unreviewed. Written 2026-09-12 in response to reading TencentDB
Agent Memory's install doc and wanting the same "shared memory across agents"
outcome without three of their tradeoffs: no LLM-intercepting proxy, no
per-request token tax, and a hard approval gate before anything becomes
shared memory.

## What this is for

Three goals, in order of how load-bearing they are to the design:

1. **Guardrails and structured context first.** Memory is scoped to a
   `team / agent / task` graph, same shape as TencentDB's, and nothing an
   agent writes becomes visible to other agents until a human (or an
   explicit policy) approves it. This is the part that matters even if the
   token story turns out to be a wash.
2. **Token reduction, not token addition.** Retrieval works by traversing
   the approved memory graph outward from an anchor (same algorithm as
   `obsidian-vault/scripts/retrieve.py`, ported to query SQLite instead of
   markdown files), with a hub-cutoff so one heavily-linked node can't pull
   in the whole team's history. Nothing is injected by default; an agent
   asks for a bounded neighborhood and gets exactly that.
3. **No excessive calls.** There is deliberately no proxy sitting in front
   of every LLM request. An agent's session makes at most two extra HTTP
   calls: one at the start (retrieve context for this team/agent/task) and
   one at the end (log what happened, into a pending queue). Compare to
   TencentDB's design, where every single turn passes through their proxy's
   auth -> session-init -> injection pipeline.

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
- **Agent** — a role under a team (name, description, system prompt).
  Memory can optionally be filtered/attributed by agent, but any agent
  under a team can retrieve any approved memory under that team by
  default (that's the "hive mind" property Surj asked for).
- **Task** — optional. A memory written without a task still works; it
  just has no Task-level filter available later.
- **MemoryNode** — the actual content. Has a tier (L0 raw / L1 extracted /
  L2 scene / L3 persona, same ladder as TencentDB, though v0 only really
  uses L0/L1 — nothing here yet does the LLM-driven L1->L2->L3 promotion,
  see Open Questions), a status (`pending` / `approved` / `rejected`), and
  a set of typed links to other node titles (same wikilink-ish convention
  as the Obsidian tooling: unresolved links are allowed but never become
  traversable graph edges).

## The write path

1. An agent's session ends. The client library posts a `MemoryNode` with
   `status=pending` — title, body, tags, links, which team/agent/task it
   belongs to.
2. It is **not** visible to `retrieve` yet. It sits in a review queue.
3. Someone (a human reviewer, or later, a configured auto-approve policy
   for low-risk tags) calls approve or reject.
4. Only `approved` nodes are ever returned by traversal.

This is the guardrail. It's the same principle Surj already applied by
hand to his own Obsidian vault (don't bulk-link existing notes without
asking) — here it's structural instead of a memory-encoded instruction,
because a team of engineers can't be trusted to all remember the same
standing instruction the way one person's AI sessions can.

## The retrieve path

`GET /teams/{id}/memory/retrieve?anchor=<title>&hops=2&hub_cutoff=15`

BFS outward from an anchor node's resolved links, stopping traversal
(but not visitation) through any node whose out-degree exceeds
`hub_cutoff`, restricted to `status=approved` and the given `team_id`.
Returns the neighborhood plus an approximate token count, same shape as
`retrieve.py --show-tokens`.

## Auth (v0, honestly minimal)

One API key per team, checked against an `X-API-Key` header. This is
enough to keep teams from reading each other's memory by accident, and
nowhere near enough for a real multi-tenant production deployment — no
per-user identity, no key rotation, no rate limiting. Flagged in
Open Questions below rather than glossed over.

## Open questions / explicitly not solved in v0

- **L1 -> L2 -> L3 promotion.** TencentDB uses a background LLM worker to
  summarize L1 into scenes and personas. v0 here has no such worker —
  everything stays at whatever tier it was written at. Worth adding once
  there's enough real L1 volume to know what a good summary policy even
  looks like; premature to build it against zero real usage.
- **Auto-approve policy.** Manual approval is the safe default and the
  only thing implemented. A policy engine (auto-approve certain tags,
  certain agents, size thresholds) is a natural v1 addition once you know
  what your team actually rejects.
- **Per-user auth.** One key per team is a placeholder, not a design.
- **Multi-node / concurrent writers.** SQLite is fine for a single team's
  single-node deployment. If this needs to serve many teams concurrently
  at real write volume, Postgres is the obvious next step — SQLite was
  chosen here for zero-dependency, easy-to-run-anywhere v0, matching the
  same reasoning as `retrieve.py` and `log_session.py`.
