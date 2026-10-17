"""
Anchor aliases - a human recording that a phrasing means a note.

The problem they solve: resolution is exact-title-or-token-overlap, so
"pg pool dying" finds nothing even when a note titled "Postgres
connection pool exhaustion" answers it exactly. That miss costs a round
trip, then the work from scratch, then often a duplicate note under yet
another title.

An alias is the cheap approximation of semantic search - no embeddings,
no model, because a person did the matching once while clearing the gap
and everybody gets the answer afterwards.
"""
import pytest


def _team(client_lib, live_server, name="Platform Team"):
    team, admin = client_lib.create_team(live_server, name).values()
    return client_lib.TeamMemoryClient(live_server, admin["token"], team["id"]), team, admin


def _approved(c, title, body="body text here", links=None):
    c.log_session(title=title, body=body, links=links or [])
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


# --- the point of the feature -----------------------------------------

def test_an_alias_turns_a_miss_into_a_hit(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    node = _approved(c, "Postgres connection pool exhaustion",
                     body="pgbouncer max_client_conn too low")

    assert c.retrieve_context("pg pool dying")["neighborhood_count"] == 0

    c.create_alias("pg pool dying", node["id"])

    result = c.retrieve_context("pg pool dying")
    assert result["neighborhood_count"] == 1
    assert result["neighborhood"][0]["id"] == node["id"]
    assert result["resolution"] == "alias"


def test_aliasing_closes_the_gap_it_was_created_from(live_server, client_lib):
    """The loop closing on itself: a reviewer clears a gap by recording
    what answers it, and the next retrieval of that phrasing both
    succeeds and removes it from the gap list."""
    c, _, _ = _team(client_lib, live_server)
    node = _approved(c, "Postgres connection pool exhaustion")

    for _ in range(4):
        c.retrieve_context("pg pool dying")
    assert [g["anchor"] for g in c.report_gaps()["gaps"]] == ["pg pool dying"]

    before = _metric(live_server, "agenthive_gaps_closed_total")
    c.create_alias("pg pool dying", node["id"])
    c.retrieve_context("pg pool dying")

    assert c.report_gaps()["gaps"] == []
    assert _metric(live_server, "agenthive_gaps_closed_total") == before + 1


def test_an_alias_outranks_a_token_fallback(live_server, client_lib):
    """The fallback is a guess that is right often enough to keep. An
    alias is somebody saying what the phrase means. Letting the guess win
    would make resolution worse the more it learned."""
    c, _, _ = _team(client_lib, live_server)
    _approved(c, "Rollout checklist")              # token-overlaps "rollout"
    target = _approved(c, "Deployment runbook")

    guessed = c.retrieve_context("rollout")
    assert guessed["resolution"] == "fallback"

    c.create_alias("rollout", target["id"])
    resolved = c.retrieve_context("rollout")
    assert resolved["resolution"] == "alias"
    assert resolved["neighborhood"][0]["id"] == target["id"]


def test_an_exact_title_still_wins(live_server, client_lib):
    """An alias must not be able to shadow a real title."""
    c, _, _ = _team(client_lib, live_server)
    real = _approved(c, "Deployment runbook")
    other = _approved(c, "Something else")
    c.create_alias("Deployment runbook", other["id"])

    result = c.retrieve_context("Deployment runbook")
    assert result["resolution"] == "exact"
    assert result["neighborhood"][0]["id"] == real["id"]


# --- safety -----------------------------------------------------------

def test_cannot_alias_to_a_node_that_is_not_approved(live_server, client_lib):
    """A pending node is invisible to retrieval, so the alias would
    resolve to nothing and look like the feature was broken."""
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Unreviewed", body="b")
    pending = c.list_pending()["pending"][0]
    with pytest.raises(RuntimeError):
        c.create_alias("some phrase", pending["id"])


def test_cannot_alias_to_another_teams_node(live_server, client_lib):
    c1, _, _ = _team(client_lib, live_server, "One")
    theirs = _approved(c1, "Team one note")
    c2, _, _ = _team(client_lib, live_server, "Two")
    with pytest.raises(RuntimeError):
        c2.create_alias("anything", theirs["id"])


def test_a_stale_alias_falls_through_instead_of_returning_nothing(live_server, client_lib):
    """If the target is rejected after the alias was made, resolution
    degrades to the fallback rather than failing outright."""
    c, _, _ = _team(client_lib, live_server)
    keep = _approved(c, "Rollout checklist")
    doomed = _approved(c, "Doomed note")
    c.create_alias("rollout", doomed["id"])
    assert c.retrieve_context("rollout")["resolution"] == "alias"

    # Un-approve the target by rejecting a fresh copy is not possible, so
    # reject the node directly through the review path.
    c.reject(doomed["id"], reason="wrong")

    result = c.retrieve_context("rollout")
    assert result["resolution"] == "fallback"
    assert result["neighborhood"][0]["id"] == keep["id"]


def test_aliases_do_not_cross_teams(live_server, client_lib):
    c1, _, _ = _team(client_lib, live_server, "One")
    node = _approved(c1, "Team one note")
    c1.create_alias("shared phrasing", node["id"])

    c2, _, _ = _team(client_lib, live_server, "Two")
    _approved(c2, "Team two note")
    assert c2.retrieve_context("shared phrasing")["neighborhood_count"] == 0


def test_realiasing_replaces_rather_than_accumulating(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    first = _approved(c, "First target")
    second = _approved(c, "Second target")
    c.create_alias("the phrase", first["id"])
    c.create_alias("the phrase", second["id"])

    assert len(c.list_aliases()["aliases"]) == 1
    assert c.retrieve_context("the phrase")["neighborhood"][0]["id"] == second["id"]


def test_deleting_an_alias_restores_the_previous_behaviour(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    node = _approved(c, "Postgres connection pool exhaustion")
    alias = c.create_alias("pg pool dying", node["id"])
    assert c.retrieve_context("pg pool dying")["neighborhood_count"] == 1

    c.delete_alias(alias["anchor_key"])
    assert c.retrieve_context("pg pool dying")["neighborhood_count"] == 0


def test_aliases_are_admin_only(live_server, client_lib):
    """It changes what retrieval returns for everyone on the team."""
    c, team, _ = _team(client_lib, live_server)
    node = _approved(c, "A note")
    member = c.create_user("alice", role="member")
    mc = client_lib.TeamMemoryClient(live_server, member["token"], team["id"])
    with pytest.raises(RuntimeError, match="403"):
        mc.create_alias("phrase", node["id"])


# --- observability ----------------------------------------------------

def test_alias_resolutions_are_counted_separately(live_server, client_lib):
    """Aliases paying off has to be distinguishable from titles simply
    being better, or there is no way to tell whether recording them is
    worth a reviewer's time."""
    c, _, _ = _team(client_lib, live_server)
    node = _approved(c, "Postgres connection pool exhaustion")
    c.create_alias("pg pool dying", node["id"])

    before = _metric(live_server, "agenthive_anchor_resolution_total",
                     '{resolution="alias"}')
    c.retrieve_context("pg pool dying")
    assert _metric(live_server, "agenthive_anchor_resolution_total",
                   '{resolution="alias"}') == before + 1
    assert _metric(live_server, "agenthive_anchor_aliases") >= 1
