"""
Progressive disclosure (mode=index + POST /memory/fetch) and the
write-path governance rules that make the tag vocabulary worth having.

The properties that matter, and would be easy to break silently:

1. The index really is cheaper than the full neighborhood, on the SAME
   traversal - otherwise the whole layer is two round trips for nothing.
2. The default is byte-identical to before. Every existing caller
   (the MCP server, the review UI, client.py, a .cursor/rules block)
   must not notice this route learned a second mode.
3. fetch cannot cross the approval gate or a team boundary. An index
   entry is an id the caller has seen; it is not a capability.
4. tokens_avoided does not inflate when a caller switches to index mode.
   That counter is AgentHive's headline claim, and a mode flag silently
   multiplying it would make the dashboard lie in our favour.
5. A reserved tag beats an auto-approve rule, including a rule that
   matched on agent_id rather than on a tag.
6. <private> never reaches storage, so it never reaches the review queue.
"""
import json
import urllib.request

import pytest


def _post(base_url, path, token, body):
    req = urllib.request.Request(
        base_url + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("X-API-Key", token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _metric(base_url, name):
    with urllib.request.urlopen(base_url + "/metrics", timeout=10) as resp:
        for line in resp.read().decode().splitlines():
            if line.startswith(name + " "):
                return float(line.split()[1])
    return 0.0


def _seed(client_lib, live_server, n=3):
    team, admin = client_lib.create_team(live_server, "Platform Team").values()
    c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])
    titles = []
    for i in range(n):
        title = f"Incident {i}"
        titles.append(title)
        c.log_session(title=title, body=("detail " * 200) + str(i),
                      tags=["bugfix"] if i == 0 else ["decision"])
    for node in c.list_pending()["pending"]:
        c.approve(node["id"])
    return c, team, admin, titles


def test_index_is_cheaper_than_full_on_the_same_traversal(live_server, client_lib):
    c, _, _, titles = _seed(client_lib, live_server)

    full = c.retrieve_context(titles[0])
    index = c.retrieve_index(titles[0])

    # Same walk, same nodes - only the payload differs.
    assert index["neighborhood_count"] == full["neighborhood_count"]
    assert [n["id"] for n in index["neighborhood"]] == [n["id"] for n in full["neighborhood"]]

    assert index["approx_tokens"] < full["approx_tokens"]
    assert index["approx_tokens_full_neighborhood"] == full["approx_tokens"]
    assert all("body" not in n for n in index["neighborhood"])
    # Priced per node, so the caller can choose rather than guess.
    assert all(n["approx_tokens"] > 0 for n in index["neighborhood"])


def test_default_mode_is_unchanged(live_server, client_lib):
    c, _, _, titles = _seed(client_lib, live_server)
    result = c.retrieve_context(titles[0])
    assert "mode" not in result          # the old shape gained no field
    assert all("body" in n for n in result["neighborhood"])


def test_fetch_returns_only_the_requested_bodies(live_server, client_lib):
    c, team, admin, titles = _seed(client_lib, live_server)
    index = c.retrieve_index(titles[0])
    assert index["neighborhood_count"] >= 1

    wanted = index["neighborhood"][0]["id"]
    fetched = c.fetch_nodes([wanted])

    assert fetched["node_count"] == 1
    assert fetched["nodes"][0]["id"] == wanted
    assert fetched["nodes"][0]["body"]
    assert fetched["missing"] == []
    # The point of the exercise: index + one body < every body.
    assert index["approx_tokens"] + fetched["approx_tokens"] < index["approx_tokens_full_neighborhood"] \
        or index["neighborhood_count"] == 1


def test_fetch_cannot_cross_the_approval_gate(live_server, client_lib):
    team, admin = client_lib.create_team(live_server, "Platform Team").values()
    c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])
    c.log_session(title="Unreviewed finding", body="should never be fetchable")
    pending_id = c.list_pending()["pending"][0]["id"]

    result = c.fetch_nodes([pending_id])
    assert result["node_count"] == 0
    assert result["missing"] == [pending_id]

    # Still invisible once explicitly rejected.
    c.reject(pending_id, reason="no")
    assert c.fetch_nodes([pending_id])["node_count"] == 0


def test_fetch_cannot_cross_a_team_boundary(live_server, client_lib):
    t1, a1 = client_lib.create_team(live_server, "Team One").values()
    c1 = client_lib.TeamMemoryClient(live_server, a1["token"], t1["id"])
    c1.log_session(title="Team one secret", body="only ours")
    nid = c1.list_pending()["pending"][0]["id"]
    c1.approve(nid)

    t2, a2 = client_lib.create_team(live_server, "Team Two").values()
    c2 = client_lib.TeamMemoryClient(live_server, a2["token"], t2["id"])

    result = c2.fetch_nodes([nid])
    # Indistinguishable from any other unknown id - no existence oracle.
    assert result["node_count"] == 0
    assert result["missing"] == [nid]


def test_fetch_deduplicates_and_bounds_input(live_server, client_lib):
    c, _, _, titles = _seed(client_lib, live_server)
    nid = c.retrieve_index(titles[0])["neighborhood"][0]["id"]

    assert c.fetch_nodes([nid, nid, nid])["requested_count"] == 1

    with pytest.raises(Exception):
        c.fetch_nodes([])
    with pytest.raises(Exception):
        c.fetch_nodes([f"id-{i}" for i in range(101)])


def test_index_mode_does_not_inflate_tokens_avoided(live_server, client_lib):
    c, _, _, titles = _seed(client_lib, live_server)

    c.retrieve_context(titles[0])
    after_full = _metric(live_server, "agenthive_tokens_avoided_total")

    c.retrieve_index(titles[0])
    after_index = _metric(live_server, "agenthive_tokens_avoided_total")

    # Identical traversal, so identical avoidance - the mode flag must not
    # be worth anything to the headline number.
    assert after_index - after_full == pytest.approx(after_full, rel=0.01)
    assert _metric(live_server, "agenthive_index_tokens_withheld_total") > 0


def test_tag_filter_narrows_without_changing_the_walk(live_server, client_lib):
    c, _, _, titles = _seed(client_lib, live_server)
    unfiltered = c.retrieve_index(titles[0])
    filtered = c.retrieve_index(titles[0], tags=["bugfix"])

    assert filtered["neighborhood_count"] <= unfiltered["neighborhood_count"]
    for node in filtered["neighborhood"]:
        assert "bugfix" in node["tags"]


def test_reserved_tag_beats_an_auto_approve_rule(live_server, client_lib):
    team, admin = client_lib.create_team(live_server, "Platform Team").values()
    c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])
    agent = c.create_agent("scanner")

    # A rule matching on AGENT, not on tag - the case a tag-only check
    # inside the matcher would have let through.
    c.create_auto_approve_rule(match_agent_id=agent["id"])

    c.log_session(title="Routine noise", body="fine", agent_id=agent["id"],
                  tags=["ci-noise"])
    c.log_session(title="Leaked key in log", body="needs eyes",
                  agent_id=agent["id"], tags=["security_alert"])

    pending = [n["title"] for n in c.list_pending()["pending"]]
    assert "Leaked key in log" in pending
    assert "Routine noise" not in pending
    assert _metric(live_server, "agenthive_auto_approve_blocked_total") >= 1


def test_private_blocks_never_reach_storage(live_server, client_lib):
    team, admin = client_lib.create_team(live_server, "Platform Team").values()
    c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])

    c.log_session(
        title="Rotated the <private>AKIAREALKEY</private> credential",
        body="Fixed by rotating. Token was <private>glsa_supersecret</private> "
             "and the runbook is updated.",
    )
    node = c.list_pending()["pending"][0]

    # Not merely absent from retrieval - absent from the review queue, which
    # is the earlier and stricter claim.
    assert "AKIAREALKEY" not in node["title"]
    assert "glsa_supersecret" not in node["body"]
    assert "[redacted]" in node["title"]
    assert "[redacted]" in node["body"]
    # The surrounding prose survives; this is redaction, not truncation.
    assert "runbook is updated" in node["body"]
    assert _metric(live_server, "agenthive_private_blocks_stripped_total") >= 2


def test_unclosed_private_tag_fails_closed(live_server, client_lib):
    team, admin = client_lib.create_team(live_server, "Platform Team").values()
    c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])
    c.log_session(title="Oops", body="before <private>never closed SECRETVALUE")
    node = c.list_pending()["pending"][0]
    assert "SECRETVALUE" not in node["body"]
    assert node["body"].startswith("before ")
