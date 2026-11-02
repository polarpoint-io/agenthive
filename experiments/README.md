# Experiments

A harness for the question a production deployment cannot answer: does
shared memory help **in general**, rather than just on this team's work?

Not part of the installed service. It shares `client.py` and nothing
else, and nothing here runs in CI — these runs cost real money in model
calls and take hours.

## Read this before running SWE-bench

SWE-bench is the recognised yardstick and worth having a number on. It
is also, for AgentHive specifically, close to the **weakest** case.

Each SWE-bench task is an independent bug in a different part of a
repository, attempted once by one agent. AgentHive's claim is about the
opposite situation: a teammate hit this last week and wrote it down, so
you do not rediscover it. That situation barely occurs in SWE-bench.

What memory *can* pick up across SWE-bench tasks in one repository is
environment knowledge — how the tests are run here, where the fixtures
live, which import is the slow one. Real, but a fraction of what the
system is for.

So read the outcomes accordingly:

| Result | What it means |
|---|---|
| Cost down, pass rate flat or up | Solid. The environment knowledge alone pays for itself. |
| No difference | Expected-ish. SWE-bench under-tests the mechanism; it is **not** evidence the mechanism fails. |
| Pass rate **down** | Take seriously. Memory is injecting something misleading, and that would show on real work too. |

The third row is the reason to run it. A benchmark is far better at
catching harm than at demonstrating benefit, and catching harm is worth
the spend on its own.

## The protocol that actually tests the claim

`repeated` is the one matching what AgentHive does: the same repository,
tasks that genuinely share context, run **in order** so memory
accumulates, with an identical run that has memory withheld.

```sh
python -m experiments.run --protocol repeated --tasks tasks/your-repo.jsonl \
    --agent claude --arms served,held_out --repeats 3
```

Build the task file from your own history — closed issues from one
repository over a quarter is a good source, and unlike SWE-bench those
tasks really do share context with each other.

## SWE-bench

```sh
python -m experiments.prepare_swebench --subset lite --repos astropy,django
python -m experiments.run --protocol swebench --tasks tasks/swebench-lite.jsonl \
    --agent claude --arms served,held_out
```

Tasks run in repository order with one AgentHive team per repository, so
memory accumulates the way it would in real use. Both arms see the same
tasks in the same order; only the memory differs.

## What is recorded

Per task: pass/fail from the repository's own tests, token counts split
by cache read / cache write / output, wall-clock, turn count, and what
AgentHive served. Written to `runs/<id>/results.jsonl`.

`python -m experiments.report runs/<id>` prints cost and pass rate per
arm with confidence intervals, and **refuses a verdict** when the
intervals overlap. Same rule as the live evidence endpoint: no answer
beats a wrong one.

## Try it before you spend anything

```sh
python -m experiments.run --tasks experiments/tasks/example.jsonl --dry-run
python -m experiments.report runs/<id>
```

`--dry-run` walks the whole pipeline with a stub agent: task ordering,
one team per repository and arm, both arms over the same sequence,
results written incrementally, and a report at the end. No model calls,
no money, no AgentHive required.

The served arm still differs from the held-out one in a dry run — it
carries the memory preamble — so this exercises the difference the real
run exists to measure, not just the plumbing around it.

## Status: what is proven and what is not

**Proven.** The orchestration runs end to end, verified by a dry run and
24 offline tests: task validation, run order, per-arm isolation,
cache-weighted cost, incremental results, one failing task not ending a
sweep, and a report that refuses a verdict on overlapping intervals.

**Not proven.** It has never driven a real agent. Three things stand
between this and a trustworthy number:

| Unverified | Why it matters |
|---|---|
| The `claude` and `codex` adapters | Their JSON output and usage shapes are from documentation, not from a run. If a usage block is missing the adapter raises rather than reporting zero — an arm that looks free would invert a result rather than spoil it — but that path has not been hit for real. |
| SWE-bench environment setup | Each repository needs its dependencies installed at the right commit. `prepare_swebench` clones and pins, and does not solve this. Run one task before a sweep. |
| Cost rates in `cost.py` | Defaults are the common shape, not your bill. Check them against an invoice once before quoting a figure. |

Treat the runner as a starting point to debug against a real agent. The
logic underneath it is tested; the contact with the outside world is
not.
