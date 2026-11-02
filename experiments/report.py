"""
Turn a run's results into a verdict, or refuse to.

Deliberately conservative: a benchmark number gets quoted far from its
context, so this reports a difference only when the arms' confidence
intervals do not overlap. An overlapping result is reported as "no
measurable difference", never as a small win.
"""

import json
import math
import pathlib


def _wilson(successes: int, n: int, z: float = 1.96) -> tuple:
    """Wilson score interval - behaves sensibly at small n and at rates
    near 0 or 1, where the normal approximation produces bounds outside
    [0, 1] and quietly flatters whichever arm is near the edge."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def summarise(rows: list) -> dict:
    arms = {}
    for row in rows:
        arm = arms.setdefault(row["arm"], {"n": 0, "passed": 0, "cost": 0.0, "turns": []})
        arm["n"] += 1
        arm["passed"] += 1 if row.get("passed") else 0
        arm["cost"] += float(row.get("cost_tokens") or 0)
        if row.get("turns"):
            arm["turns"].append(row["turns"])

    out = {}
    for name, a in arms.items():
        lo, hi = _wilson(a["passed"], a["n"])
        out[name] = {
            "tasks": a["n"],
            "passed": a["passed"],
            "pass_rate": round(a["passed"] / a["n"], 3) if a["n"] else None,
            "pass_rate_ci": [round(lo, 3), round(hi, 3)],
            "mean_cost_tokens": round(a["cost"] / a["n"], 1) if a["n"] else None,
            "median_turns": sorted(a["turns"])[len(a["turns"]) // 2] if a["turns"] else None,
        }
    return out


def verdict(summary: dict, a: str = "served", b: str = "held_out") -> dict:
    if a not in summary or b not in summary:
        return {"verdict": None, "note": f"need both {a} and {b} arms"}

    x, y = summary[a], summary[b]
    overlap = not (x["pass_rate_ci"][0] > y["pass_rate_ci"][1]
                   or y["pass_rate_ci"][0] > x["pass_rate_ci"][1])

    cost_delta = None
    if x["mean_cost_tokens"] and y["mean_cost_tokens"]:
        cost_delta = round((x["mean_cost_tokens"] - y["mean_cost_tokens"])
                           / y["mean_cost_tokens"], 3)

    if overlap:
        note = (
            f"No measurable difference in pass rate: the intervals overlap "
            f"({x['pass_rate_ci']} vs {y['pass_rate_ci']}). Report this as "
            "no difference, not as a small win - at these sample sizes the "
            "sign of the gap is noise.")
        return {"verdict": None, "pass_rate_delta": None,
                "cost_delta": cost_delta, "note": note}

    return {
        "verdict": "better" if x["pass_rate"] > y["pass_rate"] else "worse",
        "pass_rate_delta": round(x["pass_rate"] - y["pass_rate"], 3),
        "cost_delta": cost_delta,
        "note": f"Intervals do not overlap ({x['pass_rate_ci']} vs {y['pass_rate_ci']}).",
    }


def load(run_dir: str) -> list:
    path = pathlib.Path(run_dir) / "results.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv=None):
    import sys

    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m experiments.report runs/<id>")
        return 2
    rows = load(argv[0])
    summary = summarise(rows)
    print(json.dumps({"arms": summary, **verdict(summary)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
