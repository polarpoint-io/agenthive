"""
Evidence: did memory actually help?

AgentHive never sees the task, so it cannot judge success on its own.
What it can do is thread a session id through retrieval and logging, let
a session report back, and let git say whether the work survived.

The two properties worth defending here are both about refusing to
overclaim. A session that reported nothing is UNKNOWN, never folded into
the denominator. And a comparison on thin data is refused outright - a
dashboard printing 100% from two sessions gets screenshotted and quoted;
one saying "not enough data" does not.
"""
import pytest


def _team(client_lib, live_server, name="Platform Team", session_id=""):
    team, admin = client_lib.create_team(live_server, name).values()
    return (client_lib.TeamMemoryClient(live_server, admin["token"], team["id"],
                                        session_id=session_id), team, admin)


def _as(client_lib, live_server, team, admin, session_id):
    return client_lib.TeamMemoryClient(live_server, admin["token"], team["id"],
                                       session_id=session_id)


def _seed(c, title="Pool exhaustion"):
    c.log_session(title=title, body="raise max_client_conn")
    node = next(n for n in c.list_pending()["pending"] if n["title"] == title)
    c.approve(node["id"])
    return node


def _metric(base_url, name, labels=""):
    import urllib.request
    with urllib.request.urlopen(base_url + "/metrics", timeout=10) as resp:
        for line in resp.read().decode().splitlines():
            if line.startswith(name + labels + " "):
                return float(line.split()[1])
    return 0.0


# --- the arms -----------------------------------------------------------

def test_sessions_are_split_by_whether_they_got_memory(live_server, client_lib):
    c, team, admin = _team(client_lib, live_server, session_id="setup")
    _seed(c)

    got = _as(client_lib, live_server, team, admin, "s-served")
    got.retrieve_context("Pool exhaustion")
    got.log_session(title="Fixed it", body="b", outcome="succeeded", turns=12)

    missed = _as(client_lib, live_server, team, admin, "s-none")
    missed.log_session(title="Did not ask", body="b", outcome="failed", turns=30)

    arms = {a["arm"]: a for a in c.evidence()["arms"]}
    assert arms["served"]["sessions"] == 1
    assert arms["served"]["success_rate"] == 1.0
    assert arms["no_memory"]["sessions"] >= 1
    assert arms["no_memory"]["success_rate"] == 0.0


def test_a_silent_session_is_unknown_not_a_failure(live_server, client_lib):
    """A rate computed over whoever bothered to answer measures who
    answers, not whether the thing works."""
    c, team, admin = _team(client_lib, live_server, session_id="setup")
    _seed(c)

    quiet = _as(client_lib, live_server, team, admin, "s-quiet")
    quiet.retrieve_context("Pool exhaustion")
    quiet.log_session(title="No verdict given", body="b")      # no outcome

    served = next(a for a in c.evidence()["arms"] if a["arm"] == "served")
    assert served["sessions"] == 1
    assert served["reported"] == 0
    assert served["silent"] == 1
    assert served["success_rate"] is None


def test_thin_data_is_refused_rather_than_reported(live_server, client_lib):
    c, team, admin = _team(client_lib, live_server, session_id="setup")
    _seed(c)
    one = _as(client_lib, live_server, team, admin, "s-1")
    one.retrieve_context("Pool exhaustion")
    one.log_session(title="One session", body="b", outcome="succeeded")

    report = c.evidence()
    assert report["verdict"] is None
    assert "not enough" in report["note"].lower()


def test_with_no_sessions_at_all_it_says_not_enough_data(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server, session_id="setup")
    report = c.evidence()
    assert report["verdict"] is None
    assert "not enough" in report["note"].lower()


def test_without_a_holdout_the_verdict_is_marked_observational(live_server, client_lib):
    """With enough data but no holdout, it still answers - and says
    plainly that the answer is correlation, not cause."""
    c, team, admin = _team(client_lib, live_server, session_id="setup")
    _seed(c)
    for i in range(3):
        got = _as(client_lib, live_server, team, admin, f"s-got-{i}")
        got.retrieve_context("Pool exhaustion")
        got.log_session(title=f"Served {i}", body="b", outcome="succeeded")
        skipped = _as(client_lib, live_server, team, admin, f"s-skip-{i}")
        skipped.log_session(title=f"Unserved {i}", body="b", outcome="failed")

    report = c._request("GET", f"/teams/{c.team_id}/evidence",
                        query={"min_per_arm": 3})
    assert report["holdout_running"] is False
    assert report["verdict"]["causal"] is False
    assert report["verdict"]["comparison"] == "served vs no_memory"
    assert report["verdict"]["delta"] == 1.0
    assert "observational" in report["note"].lower()
    assert "not a causal claim" in report["note"].lower()


def test_a_session_retrieving_many_times_is_still_one_session(live_server, client_lib):
    c, team, admin = _team(client_lib, live_server, session_id="setup")
    _seed(c)
    busy = _as(client_lib, live_server, team, admin, "s-busy")
    for _ in range(5):
        busy.retrieve_context("Pool exhaustion")
    busy.log_session(title="Busy", body="b", outcome="succeeded")

    served = next(a for a in c.evidence()["arms"] if a["arm"] == "served")
    assert served["sessions"] == 1


def test_an_empty_retrieval_does_not_count_as_served(live_server, client_lib):
    """Asking and getting nothing is not the same as being helped."""
    c, team, admin = _team(client_lib, live_server, session_id="setup")
    asked = _as(client_lib, live_server, team, admin, "s-empty")
    asked.retrieve_context("nothing answers this")
    asked.log_session(title="Found nothing", body="b", outcome="failed")

    arms = {a["arm"]: a for a in c.evidence()["arms"]}
    assert arms["served"]["sessions"] == 0
    assert arms["no_memory"]["sessions"] >= 1


# --- git survival -------------------------------------------------------

def test_work_survival_is_recorded_without_self_reporting(live_server, client_lib):
    """Objective, and nobody grades their own homework."""
    c, team, admin = _team(client_lib, live_server, session_id="setup")
    worker = _as(client_lib, live_server, team, admin, "s-commit")
    worker.log_session(title="Shipped something", body="b",
                       outcome="succeeded", commit_sha="abc123def")

    pending = c._request("GET", f"/teams/{c.team_id}/sessions/unverified")["sessions"]
    assert [p["session_id"] for p in pending] == ["s-commit"]
    assert pending[0]["commit_sha"] == "abc123def"

    c._request("POST", f"/teams/{c.team_id}/sessions/s-commit/verify",
               body={"survived": True})
    assert c._request("GET", f"/teams/{c.team_id}/sessions/unverified")["sessions"] == []

    served = next(a for a in c.evidence()["arms"] if a["arm"] == "no_memory")
    assert served["work_verified"] == 1
    assert served["work_survival_rate"] == 1.0
    assert _metric(live_server, "agenthive_work_survival_total", '{survived="yes"}') >= 1


def test_a_session_with_no_commit_is_never_queued_for_verification(live_server, client_lib):
    c, team, admin = _team(client_lib, live_server, session_id="setup")
    _as(client_lib, live_server, team, admin, "s-nocommit").log_session(
        title="No commit", body="b", outcome="partial")
    assert c._request("GET", f"/teams/{c.team_id}/sessions/unverified")["sessions"] == []


# --- turns --------------------------------------------------------------

def test_turns_are_observed_so_the_compounding_factor_is_measurable(live_server, client_lib):
    """Content sent once is re-read every turn after, so the saving from
    sending less compounds with this number."""
    c, team, admin = _team(client_lib, live_server, session_id="setup")
    before = _metric(live_server, "agenthive_session_turns_count")
    _as(client_lib, live_server, team, admin, "s-turns").log_session(
        title="Measured", body="b", outcome="succeeded", turns=17)
    assert _metric(live_server, "agenthive_session_turns_count") == before + 1


# --- safety -------------------------------------------------------------

def test_an_unknown_outcome_is_refused(live_server, client_lib):
    c, team, admin = _team(client_lib, live_server, session_id="setup")
    bad = _as(client_lib, live_server, team, admin, "s-bad")
    with pytest.raises(RuntimeError):
        bad.log_session(title="Bad outcome", body="b", outcome="brilliant")


def test_everything_still_works_without_a_session_id(live_server, client_lib):
    """Sessions are opt-in. A caller that sends none loses the evidence
    and nothing else."""
    c, _, _ = _team(client_lib, live_server)          # no session_id
    _seed(c)
    assert c.retrieve_context("Pool exhaustion")["neighborhood_count"] == 1
    c.log_session(title="Still fine", body="b")
    assert c.evidence()["arms"][0]["sessions"] == 0


def test_evidence_does_not_leak_across_teams(live_server, client_lib):
    c1, team1, admin1 = _team(client_lib, live_server, "One", session_id="setup")
    _as(client_lib, live_server, team1, admin1, "s-one").log_session(
        title="Team one", body="b", outcome="succeeded")
    c2, _, _ = _team(client_lib, live_server, "Two", session_id="setup")
    assert all(a["sessions"] == 0 for a in c2.evidence()["arms"])


# --- the holdout --------------------------------------------------------

def test_a_held_out_session_is_told_so_not_silently_starved(live_server_holdout, client_lib):
    """A silently empty result looks like a graph with nothing in it,
    and the caller has no way to tell the difference."""
    c, team, admin = _team(client_lib, live_server_holdout, session_id="setup")
    _seed(c)

    held = _as(client_lib, live_server_holdout, team, admin, "s-held")
    result = held.retrieve_context("Pool exhaustion")

    assert result["held_out"] is True
    assert result["resolution"] == "held_out"
    assert result["neighborhood_count"] == 0
    assert "withheld deliberately" in result["note"]


def test_the_holdout_is_per_session_not_per_retrieval(live_server_holdout, client_lib):
    """A session flipping in and out halfway through would measure
    nothing, so selection is deterministic on the session id."""
    c, team, admin = _team(client_lib, live_server_holdout, session_id="setup")
    _seed(c)
    held = _as(client_lib, live_server_holdout, team, admin, "s-stable")
    assert all(held.retrieve_context("Pool exhaustion")["held_out"] for _ in range(5))


def test_held_out_sessions_form_their_own_arm(live_server_holdout, client_lib):
    c, team, admin = _team(client_lib, live_server_holdout, session_id="setup")
    _seed(c)
    held = _as(client_lib, live_server_holdout, team, admin, "s-arm")
    held.retrieve_context("Pool exhaustion")
    held.log_session(title="Managed anyway", body="b", outcome="partial")

    arms = {a["arm"]: a for a in c.evidence()["arms"]}
    assert arms["held_out"]["sessions"] == 1
    assert arms["served"]["sessions"] == 0


def test_a_running_holdout_makes_the_verdict_causal(live_server_holdout, client_lib):
    c, team, admin = _team(client_lib, live_server_holdout, session_id="setup")
    report = c._request("GET", f"/teams/{c.team_id}/evidence", query={"min_per_arm": 0})
    assert report["holdout_running"] is True
    assert report["holdout_percent"] == 100


def test_the_holdout_is_off_unless_someone_turns_it_on(live_server, client_lib):
    """It buys evidence by degrading real sessions. Only the team
    running it can consent to that."""
    c, team, admin = _team(client_lib, live_server, session_id="setup")
    _seed(c)
    normal = _as(client_lib, live_server, team, admin, "s-normal")
    result = normal.retrieve_context("Pool exhaustion")
    assert result.get("held_out") is None
    assert result["neighborhood_count"] == 1
