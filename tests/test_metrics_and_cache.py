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


def _prom_counter(live_server, name):
    with urllib.request.urlopen(live_server + "/metrics") as resp:
        body = resp.read().decode("utf-8")
    prefix = name + " "
    for line in body.splitlines():
        if line.startswith(prefix):
            return float(line.split()[-1])
    return 0.0


def test_empty_retrieval_does_not_count_as_tokens_avoided(live_server, client_lib):
    team, c = _admin_client(live_server, client_lib, name="Miss metrics")
    served = c.log_session(title="Served Note", body="x" * 40)
    other = c.log_session(title="Other Note", body="y" * 80)
    c.approve(served["id"])
    c.approve(other["id"])

    avoided_before = _prom_counter(live_server, "agenthive_tokens_avoided_total")
    empty_before = _prom_counter(live_server, "agenthive_retrieval_empty_total")
    hits_before = _prom_counter(live_server, "agenthive_retrieval_hits_total")
    missed = c.retrieve_context("no such note", hops=0)
    assert missed["neighborhood_count"] == 0
    assert missed["reduction_pct"] == 0
    assert _prom_counter(live_server, "agenthive_tokens_avoided_total") == avoided_before
    assert _prom_counter(live_server, "agenthive_retrieval_empty_total") == empty_before + 1

    hit = c.retrieve_context("Served Note", hops=0)
    assert hit["neighborhood_count"] == 1
    assert hit["approx_tokens"] > 0
    assert _prom_counter(live_server, "agenthive_tokens_avoided_total") > avoided_before
    assert _prom_counter(live_server, "agenthive_retrieval_hits_total") == hits_before + 1


def test_metrics_summary_lists_missed_anchors_and_a_pending_title(live_server, client_lib):
    team, c = _admin_client(live_server, client_lib, name="Miss list")
    c.retrieve_context("Key Vault")
    c.retrieve_context("key vault")
    pending = c.log_session(title="Key Vault rotation", body="approve me")

    summary = c._request("GET", "/teams/" + team["id"] + "/metrics/summary")
    assert summary["retrieval_hits"] == 0
    assert summary["retrieval_misses"] == 2
    top = summary["top_missed_anchors"][0]
    assert top["anchor"].lower() == "key vault"
    assert top["miss_count"] == 2
    assert top["pending_id"] == pending["id"]
    assert top["pending_title"] == "Key Vault rotation"


def test_member_cannot_read_metrics_summary(live_server, client_lib):
    team, admin_c = _admin_client(live_server, client_lib)
    member = admin_c.create_user("teammate", role="member")
    member_c = client_lib.TeamMemoryClient(live_server, member["token"], team["id"])

    import pytest
    with pytest.raises(RuntimeError, match="403"):
        member_c._request("GET", "/teams/" + team["id"] + "/metrics/summary")
