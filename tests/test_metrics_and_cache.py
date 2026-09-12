"""
Covers the v2 additions: the retrieval cache (and its cross-user hit
signal), the /metrics endpoint, and the per-team metrics summary.
"""
import urllib.request


def _admin_client(live_server, client_lib, name="Team"):
    data = client_lib.create_team(live_server, name)
    team, admin = data["team"], data["user"]
    return team, client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])


def test_metrics_endpoint_is_prometheus_text(live_server):
    with urllib.request.urlopen(live_server + "/metrics") as resp:
        assert resp.status == 200
        assert "text/plain" in resp.headers.get("Content-Type", "")
        body = resp.read().decode("utf-8")
        assert "agenthive_retrievals_total" in body
        assert "agenthive_tokens_avoided_total" in body
        assert "agenthive_cache_hits_total" in body


def test_metrics_reflect_a_write_and_a_review(live_server, client_lib):
    team, c = _admin_client(live_server, client_lib)
    node = c.log_session(title="Note", body="body")
    c.approve(node["id"])

    with urllib.request.urlopen(live_server + "/metrics") as resp:
        body = resp.read().decode("utf-8")
    assert 'agenthive_memory_writes_total{status="pending"} 1.0' in body
    assert 'agenthive_memory_reviews_total{decision="approved"} 1.0' in body


def test_second_identical_retrieval_is_a_cache_hit(live_server, client_lib):
    team, c = _admin_client(live_server, client_lib)
    node = c.log_session(title="Cached Note", body="body")
    c.approve(node["id"])

    c.retrieve_context("Cached Note")  # miss, populates cache
    c.retrieve_context("Cached Note")  # hit

    with urllib.request.urlopen(live_server + "/metrics") as resp:
        body = resp.read().decode("utf-8")
    assert "agenthive_cache_hits_total 1.0" in body
    assert "agenthive_cache_misses_total 1.0" in body


def test_cache_hit_from_a_different_user_counts_as_cross_user(live_server, client_lib):
    team, admin_c = _admin_client(live_server, client_lib)
    node = admin_c.log_session(title="Shared Note", body="body")
    admin_c.approve(node["id"])
    admin_c.retrieve_context("Shared Note")  # miss, populated by admin

    member = admin_c.create_user("teammate", role="member")
    member_c = client_lib.TeamMemoryClient(live_server, member["token"], team["id"])
    member_c.retrieve_context("Shared Note")  # hit, but a DIFFERENT user

    with urllib.request.urlopen(live_server + "/metrics") as resp:
        body = resp.read().decode("utf-8")
    assert "agenthive_cross_user_cache_hits_total 1.0" in body


def test_writing_new_memory_invalidates_the_cache(live_server, client_lib):
    team, c = _admin_client(live_server, client_lib)
    hub = c.log_session(title="Hub", body="hub", links=["Anchor"])
    anchor = c.log_session(title="Anchor", body="anchor", links=["Hub"])
    c.approve(hub["id"])
    c.approve(anchor["id"])

    first = c.retrieve_context("Anchor", hops=2)
    assert first["neighborhood_count"] == 2

    leaf = c.log_session(title="Leaf", body="leaf", links=["Hub"])
    c.approve(leaf["id"])  # should invalidate the cached "Anchor" neighborhood

    second = c.retrieve_context("Anchor", hops=2)
    assert second["neighborhood_count"] == 3


def test_team_metrics_summary_tracks_reuse(live_server, client_lib):
    team, admin_c = _admin_client(live_server, client_lib)
    node = admin_c.log_session(title="Popular Note", body="body")
    admin_c.approve(node["id"])
    admin_c.retrieve_context("Popular Note")

    member = admin_c.create_user("teammate", role="member")
    member_c = client_lib.TeamMemoryClient(live_server, member["token"], team["id"])
    member_c.retrieve_context("Popular Note")

    summary = admin_c._request("GET", "/teams/" + team["id"] + "/metrics/summary")
    assert summary["node_counts"]["approved"] == 1
    assert summary["total_retrievals"] == 2
    assert summary["active_users"] == 2
    top = summary["top_reused_nodes"][0]
    assert top["title"] == "Popular Note"
    assert top["distinct_users"] == 2


def test_member_cannot_read_metrics_summary(live_server, client_lib):
    team, admin_c = _admin_client(live_server, client_lib)
    member = admin_c.create_user("teammate", role="member")
    member_c = client_lib.TeamMemoryClient(live_server, member["token"], team["id"])

    import pytest
    with pytest.raises(RuntimeError, match="403"):
        member_c._request("GET", "/teams/" + team["id"] + "/metrics/summary")
