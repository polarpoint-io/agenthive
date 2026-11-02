"""
The harness logic. Everything here runs offline with no model calls.

What these defend is the set of mistakes that would make a paid run
worthless without failing: tasks silently reordered, arms sharing
memory, usage quietly reading zero, a malformed task discovered forty
minutes into a sweep.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from experiments import agents, cost, hive, run, tasks  # noqa: E402


def _write(tmp_path, rows):
    p = tmp_path / "tasks.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return p


def _task(tid, repo="acme/widgets"):
    return {"task_id": tid, "repo": repo, "prompt": "do the thing"}


# --- task files are validated before anything is spent ------------------

def test_a_malformed_task_fails_before_any_model_call(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"task_id": "a", "repo": "r", "prompt": "p"}\n{not json}')
    with pytest.raises(tasks.TaskError, match="not valid JSON"):
        tasks.load(p)


def test_missing_fields_are_named(tmp_path):
    with pytest.raises(tasks.TaskError, match="prompt"):
        tasks.load(_write(tmp_path, [{"task_id": "a", "repo": "r"}]))


def test_duplicate_task_ids_are_refused(tmp_path):
    """Two rows with one id would overwrite each other in the results
    and nobody would notice which run won."""
    with pytest.raises(tasks.TaskError, match="duplicate"):
        tasks.load(_write(tmp_path, [_task("same"), _task("same")]))


def test_an_empty_task_file_is_refused(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("# only a comment\n\n")
    with pytest.raises(tasks.TaskError, match="no tasks"):
        tasks.load(p)


# --- order is the experiment --------------------------------------------

def test_tasks_are_grouped_by_repo_and_stable_within_it():
    """Memory accumulates per repository and in sequence. Shuffling
    would mean every task faced a graph built from an arbitrary subset,
    which measures nothing."""
    given = [_task("w1"), _task("g1", "acme/gadgets"), _task("w2"),
             _task("g2", "acme/gadgets")]
    ordered = [t["task_id"] for t in tasks.in_run_order(given)]
    assert ordered == ["g1", "g2", "w1", "w2"]


def test_repos_keeps_first_appearance_order():
    assert tasks.repos([_task("a", "z"), _task("b", "y"), _task("c", "z")]) == ["z", "y"]


# --- cost ----------------------------------------------------------------

def test_cached_reads_are_not_counted_as_fresh_tokens():
    """Counting raw tokens understates the gap between arms, in the
    direction that flatters the arm sending more."""
    usage = {"input_tokens": 100, "cache_read_input_tokens": 1000,
             "cache_creation_input_tokens": 100, "output_tokens": 10}
    weighted = cost.Pricing().weigh(usage)
    raw = sum(usage.values())
    assert weighted < raw
    assert weighted == pytest.approx(100 + 100 + 125 + 50)


def test_an_unmodelled_usage_field_is_ignored_not_guessed():
    """Too low is visible against a bill. Inflated by a fabricated rate
    is not."""
    assert cost.Pricing().weigh({"input_tokens": 10, "mystery_tokens": 999}) == 10


def test_totals_keep_the_raw_counts_beside_the_weighted_one():
    rows = [{"usage": {"input_tokens": 10, "output_tokens": 2}} for _ in range(3)]
    t = cost.totals(rows)
    assert t["input_tokens"] == 30
    assert t["raw_tokens"] == 36
    assert t["weighted_tokens"] == pytest.approx(60.0)


# --- arms ----------------------------------------------------------------

def test_a_held_out_arm_hands_the_agent_no_credentials():
    """The cleanest way to run without memory is for the tools to be
    absent, not present and refusing."""
    arm = hive.Arm(None, "http://x", {"id": "t"}, {"token": "k"}, enabled=False)
    assert arm.env("s") == {}


def test_a_served_arm_without_a_server_still_hands_over_nothing():
    arm = hive.Arm(None, "", {"id": ""}, {"token": ""}, enabled=True)
    assert arm.env("s") == {}


def test_a_served_arm_with_a_server_passes_the_team_through():
    arm = hive.Arm(None, "http://x", {"id": "team-1"}, {"token": "tok"}, enabled=True)
    env = arm.env("s")
    assert env["AGENTHIVE_TEAM_ID"] == "team-1"
    assert env["AGENTHIVE_URL"] == "http://x"


def test_missing_hive_says_how_to_start_one():
    with pytest.raises(RuntimeError, match="server.py"):
        hive.ping("http://127.0.0.1:9", timeout=1)


# --- agents --------------------------------------------------------------

def test_an_unknown_agent_lists_the_known_ones():
    with pytest.raises(agents.AgentError, match="stub"):
        agents.get("nonesuch")


def test_the_stub_reports_usage_so_the_pipeline_is_exercised():
    out = agents.Stub().run("a prompt", cwd=".", env={}, timeout=5)
    assert out["usage"]["input_tokens"] > 0
    assert out["turns"]


# --- the whole thing, offline --------------------------------------------

def test_a_dry_run_produces_both_arms_and_a_readable_report(tmp_path):
    task_file = _write(tmp_path, [_task("t1"), _task("t2"), _task("g1", "acme/gadgets")])
    out = tmp_path / "run"
    assert run.main(["--tasks", str(task_file), "--dry-run", "--out", str(out)]) == 0

    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 6
    assert {r["arm"] for r in rows} == {"served", "held_out"}
    # Every task appears once per arm, and nothing silently vanished.
    for arm in ("served", "held_out"):
        assert sorted(r["task_id"] for r in rows if r["arm"] == arm) == ["g1", "t1", "t2"]


def test_the_served_arm_really_differs_from_the_held_out_one(tmp_path):
    """A dry run that produced identical arms would prove the harness
    runs, not that it tests anything."""
    task_file = _write(tmp_path, [_task("t1")])
    out = tmp_path / "run"
    run.main(["--tasks", str(task_file), "--dry-run", "--out", str(out)])
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    by_arm = {r["arm"]: r for r in rows}
    assert by_arm["served"]["cost_tokens"] > by_arm["held_out"]["cost_tokens"]


def test_one_bad_task_does_not_end_the_run(tmp_path, monkeypatch):
    """A sweep that dies at task eighty should keep the first
    seventy-nine."""
    def explode(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(run, "run_task", explode)

    task_file = _write(tmp_path, [_task("t1"), _task("t2")])
    out = tmp_path / "run"
    assert run.main(["--tasks", str(task_file), "--dry-run", "--out", str(out)]) == 0
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 4
    assert all("harness error" in r["detail"] for r in rows)


def test_a_run_records_what_it_was(tmp_path):
    task_file = _write(tmp_path, [_task("t1")])
    out = tmp_path / "run"
    run.main(["--tasks", str(task_file), "--dry-run", "--out", str(out)])
    meta = json.loads((out / "run.json").read_text())
    assert meta["dry_run"] is True and meta["agent"] == "stub"
