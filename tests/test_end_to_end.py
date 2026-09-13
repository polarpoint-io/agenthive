"""
End-to-end behavior against a real running server, same properties the
original v0 test_service.py proved, kept as pytest so they run in CI:

1. A newly logged memory is pending and invisible to retrieval.
2. Approving it makes it visible; rejecting it keeps it invisible forever.
3. Two teams cannot see each other's memory, even with identical anchors.
4. A hub node is reachable directly but doesn't pull in everything beyond it.
"""
import pytest


def test_pending_memory_is_invisible_until_approved(live_server, client_lib):
    team, admin = client_lib.create_team(live_server, "Platform Team").values()
    c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])
    agent = c.create_agent("bug-fix engineer", "fixes prod incidents")

    c.log_session(
        title="Postgres connection pool exhaustion",
        body="Root cause: pgbouncer max_client_conn too low. Fix: raised to 400.",
        agent_id=agent["id"], tags=["postgres", "incident"],
    )

    result = c.retrieve_context("Postgres connection pool exhaustion")
    assert result["neighborhood_count"] == 0

    pending = c.list_pending()["pending"]
    assert len(pending) == 1

    c.approve(pending[0]["id"])
    result = c.retrieve_context("Postgres connection pool exhaustion")
    assert result["neighborhood_count"] == 1
    assert result["approx_tokens"] > 0


def test_rejected_memory_stays_invisible(live_server, client_lib):
    team, admin = client_lib.create_team(live_server, "Platform Team").values()
    c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])

    c.log_session(title="Speculative idea", body="Half-formed, should never surface.")
    pending = c.list_pending()["pending"]
    c.reject(pending[0]["id"], reason="not ready")

    result = c.retrieve_context("Speculative idea")
    assert result["neighborhood_count"] == 0


def test_teams_cannot_see_each_others_memory(live_server, client_lib):
    team_a, admin_a = client_lib.create_team(live_server, "Team A").values()
    team_b, admin_b = client_lib.create_team(live_server, "Team B").values()
    ca = client_lib.TeamMemoryClient(live_server, admin_a["token"], team_a["id"])
    cb = client_lib.TeamMemoryClient(live_server, admin_b["token"], team_b["id"])

    node_a = ca.log_session(title="Same Title", body="Team A content")
    ca.approve(node_a["id"])
    node_b = cb.log_session(title="Same Title", body="Team B content")
    cb.approve(node_b["id"])

    result_a = ca.retrieve_context("Same Title")
    result_b = cb.retrieve_context("Same Title")
    assert "Team A content" in result_a["neighborhood"][0]["body"]
    assert "Team B content" in result_b["neighborhood"][0]["body"]


def test_cross_team_token_is_rejected(live_server, client_lib):
    team_a, admin_a = client_lib.create_team(live_server, "Team A").values()
    team_b, _ = client_lib.create_team(live_server, "Team B").values()
    ca = client_lib.TeamMemoryClient(live_server, admin_a["token"], team_b["id"])

    with pytest.raises(RuntimeError, match="403"):
        ca.retrieve_context("anything")


def test_hub_cutoff_end_to_end(live_server, client_lib):
    team, admin = client_lib.create_team(live_server, "Hub Test Team").values()
    c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])

    leaves = [f"Leaf {i}" for i in range(1, 7)]
    hub = c.log_session(title="Hub", body="hub", links=["Anchor"] + leaves)
    anchor = c.log_session(title="Anchor", body="anchor", links=["Hub"])
    leaf_nodes = [c.log_session(title=leaf, body=leaf, links=["Hub"]) for leaf in leaves]

    for n in [hub, anchor] + leaf_nodes:
        c.approve(n["id"])

    loose = c.retrieve_context("Anchor", hops=2, hub_cutoff=15)
    tight = c.retrieve_context("Anchor", hops=2, hub_cutoff=3)

    assert loose["neighborhood_count"] == 1 + 1 + len(leaves)
    assert tight["neighborhood_count"] == 2


def test_healthz_and_readyz(live_server):
    import json
    import urllib.request

    with urllib.request.urlopen(live_server + "/healthz") as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["status"] == "ok"

    with urllib.request.urlopen(live_server + "/readyz") as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["status"] == "ready"


def test_ui_is_served(live_server):
    import urllib.request

    with urllib.request.urlopen(live_server + "/ui") as resp:
        assert resp.status == 200
        body = resp.read().decode("utf-8")
        assert "AgentHive" in body


def test_cors_enabled_by_default(live_server):
    """A UI hosted on a different origin (see ui/Dockerfile) needs both a
    successful preflight and Access-Control-Allow-Origin on the actual
    response - CORS_ORIGIN defaults to "*", safe here specifically
    because auth is an X-API-Key header a page has to add itself, never a
    cookie a browser attaches automatically (see ADR.md)."""
    import urllib.request

    req = urllib.request.Request(live_server + "/healthz", method="OPTIONS")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 204
        assert resp.headers["Access-Control-Allow-Origin"] == "*"
        assert "OPTIONS" in resp.headers["Access-Control-Allow-Methods"]
        assert "X-API-Key" in resp.headers["Access-Control-Allow-Headers"]

    with urllib.request.urlopen(live_server + "/healthz") as resp:
        assert resp.headers["Access-Control-Allow-Origin"] == "*"
