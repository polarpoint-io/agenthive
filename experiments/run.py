"""
The runner.

For each repository, for each arm, every task in the same order. The
served arm has AgentHive wired in and accumulates memory as it goes; the
held-out arm is identical in every other respect and has no memory at
all.

What this deliberately does NOT do is decide whether a task passed. That
is the repository's own test command, run after the agent stops, because
a harness that grades the work it commissioned is not evidence.
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import time
import uuid

from . import agents, cost, hive, tasks as taskmod

PROMPT = """{problem}

When you are done, stop. Do not ask follow-up questions.
"""

MEMORY_PREAMBLE = """You have access to shared team memory through the
agenthive tools. Call retrieve_context with a short description of this
task before you start, and log_session when you finish, including
whether it worked.

"""


def _run_tests(task: dict, cwd: str, timeout: int) -> tuple:
    """The repository's own verdict. No test command means the task is
    recorded as unscored rather than silently passing."""
    cmd = task.get("test_cmd")
    if not cmd:
        return None, "no test_cmd"
    try:
        done = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                              text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, "test timeout"
    return done.returncode == 0, (done.stdout + done.stderr)[-2000:]


def _prepare(task: dict, workdir: pathlib.Path) -> pathlib.Path:
    """Put the repository at the task's base commit."""
    dest = workdir / task["task_id"]
    if dest.exists():
        shutil.rmtree(dest)
    source = task.get("repo_path")
    if not source:
        raise RuntimeError(
            f"{task['task_id']}: no repo_path. prepare_swebench writes one; a "
            "hand-written task file needs it too.")
    shutil.copytree(source, dest, symlinks=True)
    if task.get("base_commit"):
        subprocess.run(["git", "checkout", "-q", "--force", task["base_commit"]],
                       cwd=dest, check=True, capture_output=True)
    return dest


def run_task(agent, task, arm, workdir, timeout, dry_run=False):
    session_id = uuid.uuid4().hex
    started = time.time()

    prompt = PROMPT.format(problem=task["prompt"])
    if arm.enabled:
        prompt = MEMORY_PREAMBLE + prompt

    cwd = str(workdir) if dry_run else str(_prepare(task, workdir))
    result = agent.run(prompt, cwd=cwd, env=arm.env(session_id), timeout=timeout)

    passed, detail = (None, "dry run") if dry_run else _run_tests(task, cwd, timeout)

    return {
        "task_id": task["task_id"],
        "repo": task["repo"],
        "arm": "served" if arm.enabled else "held_out",
        "session_id": session_id,
        "passed": passed,
        "detail": detail,
        "usage": result["usage"],
        "turns": result.get("turns"),
        "seconds": round(time.time() - started, 1),
        "cost_tokens": round(cost.Pricing().weigh(result["usage"]), 1),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Does shared memory help an agent?")
    p.add_argument("--tasks", required=True)
    p.add_argument("--agent", default="stub")
    p.add_argument("--arms", default="served,held_out")
    p.add_argument("--protocol", default="repeated", choices=("repeated", "swebench"))
    p.add_argument("--hive-url", default="http://127.0.0.1:8790")
    p.add_argument("--repo-root", default=str(pathlib.Path(__file__).resolve().parent.parent))
    p.add_argument("--out", default=None)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--limit", type=int, default=0, help="first N tasks per repo")
    p.add_argument("--dry-run", action="store_true",
                   help="exercise the orchestration with no model calls")
    args = p.parse_args(argv)

    all_tasks = taskmod.in_run_order(taskmod.load(args.tasks))
    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]
    agent = agents.get("stub" if args.dry_run else args.agent)

    if "served" in arm_names and not args.dry_run:
        hive.ping(args.hive_url)

    run_id = args.out or f"runs/{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir = pathlib.Path(run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    work.mkdir(exist_ok=True)
    results_path = out_dir / "results.jsonl"

    (out_dir / "run.json").write_text(json.dumps({
        "tasks": args.tasks, "agent": agent.name, "arms": arm_names,
        "protocol": args.protocol, "dry_run": args.dry_run,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2))

    written = 0
    with results_path.open("w") as sink:
        for repo in taskmod.repos(all_tasks):
            repo_tasks = [t for t in all_tasks if t["repo"] == repo]
            if args.limit:
                repo_tasks = repo_tasks[:args.limit]

            # In a dry run the served arm is still "enabled", so it still
            # gets the memory preamble and the arms still differ. Only the
            # credentials are missing, so nothing reaches a server.
            arms = ({n: hive.Arm(None, "", {"id": ""}, {"token": ""},
                                 enabled=(n != "held_out"))
                     for n in arm_names} if args.dry_run
                    else hive.make_arms(args.hive_url, args.repo_root, repo, arm_names))

            for arm_name in arm_names:
                # Each arm walks the repository's tasks from the start,
                # in the same order, so the served arm's memory grows the
                # way it would in real use and the held-out arm faces the
                # identical sequence.
                for task in repo_tasks:
                    try:
                        row = run_task(agent, task, arms[arm_name], work,
                                       args.timeout, dry_run=args.dry_run)
                    except Exception as e:  # noqa: BLE001 - one task must not end the run
                        row = {"task_id": task["task_id"], "repo": repo,
                               "arm": arm_name, "passed": None,
                               "detail": f"harness error: {e!r}", "usage": {},
                               "cost_tokens": 0}
                    row["arm"] = arm_name
                    sink.write(json.dumps(row) + "\n")
                    sink.flush()       # a run that dies at task 80 keeps 79
                    written += 1
                    print(f"  {arm_name:<9} {task['task_id']:<28} "
                          f"passed={row['passed']} cost={row['cost_tokens']}")

    print(f"\n{written} results -> {results_path}")
    print(f"python -m experiments.report {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
