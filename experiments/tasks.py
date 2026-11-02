"""
Task loading, and the ORDER tasks run in.

Order is not a detail here. AgentHive accumulates memory across a
repository's sessions, so a run that shuffles tasks measures nothing
about it: every task would face a graph built from an arbitrary subset.
Tasks are grouped by repository and run in a fixed order, and both arms
see exactly the same sequence.
"""

import json
import pathlib

REQUIRED = ("task_id", "repo", "prompt")


class TaskError(ValueError):
    """A task file that cannot be trusted. Raised eagerly, before any
    model call: discovering a malformed task after forty minutes of paid
    agent time is a bad way to find out."""


def load(path) -> list:
    rows = []
    text = pathlib.Path(path).read_text()
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise TaskError(f"{path}:{n}: not valid JSON - {e}") from None
        missing = [k for k in REQUIRED if not row.get(k)]
        if missing:
            raise TaskError(f"{path}:{n}: missing {', '.join(missing)}")
        rows.append(row)

    if not rows:
        raise TaskError(f"{path}: no tasks")

    ids = [r["task_id"] for r in rows]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise TaskError(f"{path}: duplicate task_id: {', '.join(sorted(dupes))}")
    return rows


def in_run_order(tasks: list) -> list:
    """Grouped by repository, stable within each.

    Stable rather than sorted by id: a task file's own order is often
    chronological, and for the `repeated` protocol that is exactly the
    order worth preserving - memory should arrive the way it would have
    in real work."""
    by_repo = {}
    for t in tasks:
        by_repo.setdefault(t["repo"], []).append(t)
    out = []
    for repo in sorted(by_repo):
        out.extend(by_repo[repo])
    return out


def repos(tasks: list) -> list:
    seen = []
    for t in tasks:
        if t["repo"] not in seen:
            seen.append(t["repo"])
    return seen
