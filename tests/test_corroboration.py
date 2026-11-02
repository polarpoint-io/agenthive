"""
Corroboration: what later sessions say about a note that already exists.

The hole this closes: tiers measure whether a note is READ, which is
popularity, not truth. A note that is wrong but useful-looking keeps
being retrieved, keeps climbing, and ends up ranked canonical - the
system confidently amplifying the one thing it should have caught.

The safety property that shapes the whole design: a contradiction
REPORTS, it never removes. Anything stronger hands every caller a way to
blank the graph one verdict at a time, which is the attack the approval
gate exists to prevent, arriving from the direction nobody watches.
"""
import pytest


def _team(client_lib, live_server, name="Platform Team"):
    team, admin = client_lib.create_team(live_server, name).values()
    return client_lib.TeamMemoryClient(live_server, admin["token"], team["id"]), team, admin


def _approved(c, title, body="body text here"):
    c.log_session(title=title, body=body)
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


# --- the core property ------------------------------------------------

def test_a_contradicted_note_stops_climbing_however_popular(live_server, client_lib):
    """The whole point. Retrieval earns a tier; being wrong revokes it."""
    c, team, _ = _team(client_lib, live_server)
    node = _approved(c, "Pool exhaustion", body="raise max_client_conn")

    # Make it popular: three distinct people, plenty of retrievals.
    readers = [c]
    for name in ("alice", "bob"):
        u = c.create_user(name, role="member")
        readers.append(client_lib.TeamMemoryClient(live_server, u["token"], team["id"]))
    for r in readers:
        for _ in range(3):
            r.retrieve_context("Pool exhaustion")

    promote = next(ch for ch in c.suggest_tiers()["changes"] if ch["id"] == node["id"])
    assert promote["proposed_tier"] == "L3" and promote["reason"] == "use"

    # One teammate says it is wrong. Popularity is unchanged.
    readers[1].contradict(node["id"], reason="max_client_conn was already 400")

    held = [ch for ch in c.suggest_tiers()["changes"] if ch["id"] == node["id"]]
    assert held == [] or held[0]["proposed_tier"] != "L3", (
        "a contradicted note must not be promoted on popularity")


def test_a_contradicted_note_is_still_served(live_server, client_lib):
    """Reporting is not removing. The note stays retrievable until a
    human decides otherwise - otherwise one verdict blanks the graph."""
    c, _, _ = _team(client_lib, live_server)
    node = _approved(c, "Disputed note")
    c.contradict(node["id"], reason="not true any more")

    result = c.retrieve_context("Disputed note")
    assert result["neighborhood_count"] == 1
    assert result["neighborhood"][0]["contradictions"] == 1


def test_verdicts_travel_with_the_note(live_server, client_lib):
    """A reader deciding how much to trust a note needs this next to the
    note, not in a dashboard somebody reads monthly."""
    c, team, _ = _team(client_lib, live_server)
    node = _approved(c, "Confirmed note")
    u = c.create_user("alice", role="member")
    other = client_lib.TeamMemoryClient(live_server, u["token"], team["id"])
    c.confirm(node["id"])
    other.confirm(node["id"])

    served = c.retrieve_context("Confirmed note")["neighborhood"][0]
    assert served["confirmations"] == 2
    assert served["contradictions"] == 0

    index = c.retrieve_index("Confirmed note")["neighborhood"][0]
    assert index["confirmations"] == 2


def test_sustained_contradiction_proposes_demotion(live_server, client_lib):
    """One dissenting voice holds a note where it is; two push it back
    down. It takes more to demote than to stop a climb, because a single
    objection should not override a team that relies on the note."""
    c, team, _ = _team(client_lib, live_server)
    node = _approved(c, "Wrong note")
    for _ in range(3):
        c.retrieve_context("Wrong note")
    change = next(ch for ch in c.suggest_tiers()["changes"] if ch["id"] == node["id"])
    c.apply_tiers([change])

    users = [c]
    for name in ("alice", "bob"):
        u = c.create_user(name, role="member")
        users.append(client_lib.TeamMemoryClient(live_server, u["token"], team["id"]))
    for r in users[:2]:
        r.contradict(node["id"], reason="tried it, no")

    demote = next(ch for ch in c.suggest_tiers()["changes"] if ch["id"] == node["id"])
    assert demote["direction"] == "demote"
    assert demote["reason"] == "contradicted"


# --- counting -----------------------------------------------------------

def test_one_verdict_per_person_not_per_call(live_server, client_lib):
    """One agent confirming the same note fifty times across an
    afternoon is one person's opinion fifty times."""
    c, _, _ = _team(client_lib, live_server)
    node = _approved(c, "Repeated note")
    for _ in range(5):
        c.confirm(node["id"])
    assert c.retrieve_context("Repeated note")["neighborhood"][0]["confirmations"] == 1


def test_changing_your_mind_replaces_the_verdict(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    node = _approved(c, "Reconsidered")
    c.confirm(node["id"])
    c.contradict(node["id"], reason="spoke too soon")

    served = c.retrieve_context("Reconsidered")["neighborhood"][0]
    assert served["confirmations"] == 0
    assert served["contradictions"] == 1


# --- safety -------------------------------------------------------------

def test_only_an_approved_node_can_be_judged(live_server, client_lib):
    """A pending note was never visible, so nobody has used it and a
    verdict would be an opinion rather than experience."""
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Unreviewed", body="b")
    pending = c.list_pending()["pending"][0]
    with pytest.raises(RuntimeError):
        c.confirm(pending["id"])


def test_feedback_cannot_cross_a_team_boundary(live_server, client_lib):
    c1, _, _ = _team(client_lib, live_server, "One")
    theirs = _approved(c1, "Team one note")
    c2, _, _ = _team(client_lib, live_server, "Two")
    with pytest.raises(RuntimeError):
        c2.contradict(theirs["id"])


def test_an_unknown_verdict_is_refused(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    node = _approved(c, "A note")
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        f"{live_server}/teams/{c.team_id}/memory/{node['id']}/feedback",
        data=json.dumps({"verdict": "deleted"}).encode(), method="POST")
    req.add_header("X-API-Key", c.api_key)
    req.add_header("Content-Type", "application/json")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(req, timeout=10)


# --- the disputed queue -------------------------------------------------

def test_disputed_lists_what_is_already_in_the_graph(live_server, client_lib):
    """The review queue has only ever looked at what is trying to get
    IN. This is what is already in and should probably come out."""
    c, _, _ = _team(client_lib, live_server)
    good = _approved(c, "Good note")
    bad = _approved(c, "Bad note")
    c.contradict(bad["id"], reason="pgbouncer was not the cause")

    disputed = c.list_disputed()["disputed"]
    assert [d["title"] for d in disputed] == ["Bad note"]
    assert disputed[0]["reason"] == "pgbouncer was not the cause"
    assert disputed[0]["reported_by"]
    assert good["id"] not in [d["node_id"] for d in disputed]


def test_a_vouched_for_note_is_not_pruned_as_stale(live_server, client_lib):
    """Unread is not unwanted. A prune list that ignores that is how a
    team loses the note nobody needed until the day they did."""
    c, _, _ = _team(client_lib, live_server)
    cold = _approved(c, "Cold but correct")
    c.confirm(cold["id"], reason="still accurate, just rarely needed")

    stale = c.suggest_tiers(stale_after_days=0)["stale"]
    assert cold["id"] not in [s["id"] for s in stale]


def test_feedback_is_counted_by_verdict(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    node = _approved(c, "Measured note")
    before = _metric(live_server, "agenthive_memory_feedback_total", '{verdict="contradicted"}')
    c.contradict(node["id"])
    assert _metric(live_server, "agenthive_memory_feedback_total",
                   '{verdict="contradicted"}') == before + 1
    assert _metric(live_server, "agenthive_disputed_nodes") >= 1
