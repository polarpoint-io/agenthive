"""
SWE-bench -> a task file this harness can run.

Needs `datasets` and clones each repository once. Kept separate from the
runner so a failure here costs nothing but time, and so a task file can
be written by hand or from your own issue tracker instead - which, for
what AgentHive actually claims, is the more honest source. See README.
"""

import argparse
import json
import pathlib
import subprocess

SUBSETS = {
    "lite": "princeton-nlp/SWE-bench_Lite",
    "verified": "princeton-nlp/SWE-bench_Verified",
    "full": "princeton-nlp/SWE-bench",
}


def _clone(repo: str, into: pathlib.Path) -> pathlib.Path:
    dest = into / repo.replace("/", "__")
    if not dest.exists():
        subprocess.run(["git", "clone", "-q", f"https://github.com/{repo}.git", str(dest)],
                       check=True)
    return dest


def build(subset: str, repos, out_path: pathlib.Path, checkouts: pathlib.Path,
          limit_per_repo: int = 0) -> int:
    try:
        from datasets import load_dataset
    except ImportError:
        raise SystemExit(
            "pip install datasets - kept out of requirements.txt because the "
            "service does not need it and this script is not part of it") from None

    rows = load_dataset(SUBSETS[subset], split="test")
    wanted = set(repos or [])
    checkouts.mkdir(parents=True, exist_ok=True)

    per_repo, written = {}, 0
    with out_path.open("w") as sink:
        for row in rows:
            repo = row["repo"]
            if wanted and repo not in wanted:
                continue
            if limit_per_repo and per_repo.get(repo, 0) >= limit_per_repo:
                continue

            # FAIL_TO_PASS is what the task is judged on. Running the
            # whole suite would score the repository, not the fix.
            fail_to_pass = json.loads(row.get("FAIL_TO_PASS") or "[]")
            if not fail_to_pass:
                continue

            sink.write(json.dumps({
                "task_id": row["instance_id"],
                "repo": repo,
                "repo_path": str(_clone(repo, checkouts)),
                "base_commit": row["base_commit"],
                "prompt": row["problem_statement"],
                "test_cmd": "python -m pytest -q " + " ".join(
                    t.split("::")[0] for t in sorted({f.split("::")[0] for f in fail_to_pass})),
                "fail_to_pass": fail_to_pass,
            }) + "\n")
            per_repo[repo] = per_repo.get(repo, 0) + 1
            written += 1
    return written


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--subset", default="lite", choices=sorted(SUBSETS))
    p.add_argument("--repos", default="", help="comma-separated, blank for all")
    p.add_argument("--out", default="tasks/swebench-lite.jsonl")
    p.add_argument("--checkouts", default="checkouts")
    p.add_argument("--limit-per-repo", type=int, default=0)
    args = p.parse_args(argv)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = build(args.subset, [r.strip() for r in args.repos.split(",") if r.strip()],
              out, pathlib.Path(args.checkouts), args.limit_per_repo)
    print(f"{n} tasks -> {out}")
    print("NOTE: SWE-bench environments usually need per-repo dependency setup. "
          "Check a single task runs before committing to a full sweep.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
