"""
The memory-processing endpoints against a real running server.

The unit tests cover the judgement; these cover the things only a real
database can get wrong - the co-access self-join, the LEFT JOIN that has
to keep never-retrieved nodes, team scoping on every one of these new
reads, and the single endpoint here that writes.
"""


def _team(client_lib, live_server, name="Platform Team"):
    team, admin = client_lib.create_team(live_server, name).values()
    return client_lib.TeamMemoryClient(live_server, admin["token"], team["id"]), team, admin


def _approved(client, title, body="body text", tags=None, links=None):
    client.log_session(title=title, body=body, tags=tags or [], links=links or [])
    node = next(n for n in client.list_pending()["pending"] if n["title"] == title)
    client.approve(node["id"])
    return node


# --- gaps -------------------------------------------------------------

def test_repeated_misses_become_a_gap(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    for _ in range(3):
        c.retrieve_context("Kafka consumer lag")

    gaps = c.report_gaps()["gaps"]
    assert [g["anchor"] for g in gaps] == ["Kafka consumer lag"]
    assert gaps[0]["miss_count"] == 3
    assert gaps[0]["awaiting_review"] is False


def test_a_gap_answerable_from_the_queue_says_so(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    for _ in range(2):
        c.retrieve_context("Kafka consumer lag")
    c.log_session(title="Kafka consumer lag", body="raise max.poll.interval")

    gaps = c.report_gaps()["gaps"]
    assert gaps[0]["awaiting_review"] is True
    assert gaps[0]["pending_title"] == "Kafka consumer lag"


def test_gaps_do_not_leak_across_teams(live_server, client_lib):
    c1, _, _ = _team(client_lib, live_server, "One")
    for _ in range(3):
        c1.retrieve_context("Team one secret anchor")
    c2, _, _ = _team(client_lib, live_server, "Two")
    assert c2.report_gaps()["gaps"] == []


def test_gaps_are_member_level(live_server, client_lib):
    """An agent has to be able to ask this at session start - that is
    the whole point - so it must not be admin-gated."""
    c, team, _admin = _team(client_lib, live_server)
    for _ in range(2):
        c.retrieve_context("Something missing")
    member = c.create_user("alice", role="member")
    mc = client_lib.TeamMemoryClient(live_server, member["token"], team["id"])
    assert len(mc.report_gaps()["gaps"]) == 1


# --- links ------------------------------------------------------------

def test_co_retrieved_but_unlinked_nodes_are_suggested(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    hub = _approved(c, "Incident hub", links=["Alpha detail", "Beta detail"])
    _approved(c, "Alpha detail")
    _approved(c, "Beta detail")

    # Each retrieval from the hub pulls both leaves into one retrieval_id.
    for _ in range(4):
        c.retrieve_context("Incident hub")

    links = c.suggest_links(min_shared=3)["links"]
    pairs = {frozenset((row["a_title"], row["b_title"])) for row in links}
    # The two leaves ride together every time but are not linked to
    # each other - only to the hub.
    assert frozenset(("Alpha detail", "Beta detail")) in pairs
    # The hub's own edges already exist and must not be re-suggested.
    assert frozenset(("Incident hub", "Alpha detail")) not in pairs
    assert hub["id"]


def test_link_suggestions_respect_the_threshold(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    _approved(c, "Incident hub", links=["Alpha detail", "Beta detail"])
    _approved(c, "Alpha detail")
    _approved(c, "Beta detail")
    c.retrieve_context("Incident hub")          # once only

    assert c.suggest_links(min_shared=3)["links"] == []


# --- tiers ------------------------------------------------------------

def test_tier_proposals_come_from_real_access(live_server, client_lib):
    c, team, _admin = _team(client_lib, live_server)
    node = _approved(c, "Hot note")

    member = c.create_user("alice", role="member")
    mc = client_lib.TeamMemoryClient(live_server, member["token"], team["id"])
    for _ in range(3):
        c.retrieve_context("Hot note")
        mc.retrieve_context("Hot note")

    changes = c.suggest_tiers()["changes"]
    hot = [ch for ch in changes if ch["id"] == node["id"]]
    assert hot and hot[0]["direction"] == "promote"
    assert hot[0]["distinct_users"] == 2


def test_never_retrieved_nodes_still_appear_in_the_stats(live_server, client_lib):
    """The LEFT JOIN matters: a node nobody has touched is the most
    interesting row, and an INNER JOIN would hide exactly those."""
    c, _, _ = _team(client_lib, live_server)
    _approved(c, "Cold note")
    payload = c.suggest_tiers(stale_after_days=0)
    assert [s["title"] for s in payload["stale"]] == ["Cold note"]


def test_applying_tiers_changes_what_retrieval_returns(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    node = _approved(c, "Promote me")
    for _ in range(3):
        c.retrieve_context("Promote me")

    changes = [ch for ch in c.suggest_tiers()["changes"] if ch["id"] == node["id"]]
    result = c.apply_tiers([{"id": changes[0]["id"],
                             "proposed_tier": changes[0]["proposed_tier"]}])
    assert result["applied"][0]["id"] == node["id"]

    served = c.retrieve_context("Promote me")["neighborhood"][0]
    assert served["tier"] == changes[0]["proposed_tier"]


def test_apply_tiers_rejects_an_unknown_tier(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    node = _approved(c, "Some note")
    out = c.apply_tiers([{"id": node["id"], "proposed_tier": "L9"}])
    assert out["applied"] == []
    assert out["skipped"][0]["reason"] == "invalid tier"


def test_apply_tiers_cannot_reach_another_teams_node(live_server, client_lib):
    c1, _, _ = _team(client_lib, live_server, "One")
    node = _approved(c1, "Team one note")
    c2, _, _ = _team(client_lib, live_server, "Two")

    # Reported as applied (the id is opaque), but the UPDATE is scoped by
    # team so nothing actually changed.
    c2.apply_tiers([{"id": node["id"], "proposed_tier": "L3"}])
    assert c1.retrieve_context("Team one note")["neighborhood"][0]["tier"] == "L1"


# --- rules ------------------------------------------------------------

def test_a_consistently_approved_tag_is_proposed_as_a_rule(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    for i in range(6):
        _approved(c, f"Routine {i}", tags=["ci-noise"])

    rules = c.suggest_rules()["rules"]
    assert any(r["match"] == "tag" and r["value"] == "ci-noise" for r in rules)


def test_a_reserved_tag_is_never_proposed(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    for i in range(8):
        _approved(c, f"Alert {i}", tags=["security_alert"])

    rules = c.suggest_rules()["rules"]
    assert all(r["value"] != "security_alert" for r in rules)


def test_rejections_surface_a_noisy_writer(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    agent = c.create_agent("chatty")
    for i in range(6):
        c.log_session(title=f"Low value {i}", body="meh", agent_id=agent["id"])
    for node in c.list_pending()["pending"]:
        c.reject(node["id"], reason="not useful")

    found = c.suggest_rules()
    assert found["rules"] == []
    assert found["noisy"][0]["value"] == agent["id"]


def test_suggestions_are_admin_only(live_server, client_lib):
    """Rules and tiers change how the gate behaves, so they stay with
    the humans who own the gate."""
    import pytest
    c, team, _ = _team(client_lib, live_server)
    member = c.create_user("alice", role="member")
    mc = client_lib.TeamMemoryClient(live_server, member["token"], team["id"])

    for call in (mc.suggest_rules, mc.suggest_tiers, mc.suggest_links):
        with pytest.raises(RuntimeError, match="403"):
            call()


# --- the learning loop, and whether it can be seen ---------------------

def _metric(base_url, name, labels=""):
    import urllib.request
    target = name + labels
    with urllib.request.urlopen(base_url + "/metrics", timeout=10) as resp:
        for line in resp.read().decode().splitlines():
            if line.startswith(target + " "):
                return float(line.split()[1])
    return 0.0


def test_a_filled_gap_stops_being_a_gap(live_server, client_lib):
    """The bug this guards: retrieval_misses only ever incremented, so an
    anchor stayed on the gap list after somebody filled it and
    report_gaps kept asking for a note that already existed."""
    c, _, _ = _team(client_lib, live_server)
    for _ in range(3):
        c.retrieve_context("Kafka consumer lag")
    assert len(c.report_gaps()["gaps"]) == 1

    _approved(c, "Kafka consumer lag", body="raise max.poll.interval.ms")
    c.retrieve_context("Kafka consumer lag")          # now a hit

    assert c.report_gaps()["gaps"] == []


def test_closing_a_gap_is_counted_with_the_pain_it_cost(live_server, client_lib):
    """gaps_closed_total is the headline 'is the loop working' number;
    misses_recovered divided by it is how many failed lookups a gap cost
    before anyone filled it."""
    c, _, _ = _team(client_lib, live_server)
    before_closed = _metric(live_server, "agenthive_gaps_closed_total")
    before_recovered = _metric(live_server, "agenthive_gap_misses_recovered_total")

    for _ in range(4):
        c.retrieve_context("Pgbouncer saturation")
    _approved(c, "Pgbouncer saturation", body="max_client_conn")
    c.retrieve_context("Pgbouncer saturation")

    assert _metric(live_server, "agenthive_gaps_closed_total") == before_closed + 1
    assert _metric(live_server, "agenthive_gap_misses_recovered_total") == before_recovered + 4


def test_a_hit_that_was_never_a_gap_closes_nothing(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    _approved(c, "Always present")
    before = _metric(live_server, "agenthive_gaps_closed_total")
    c.retrieve_context("Always present")
    assert _metric(live_server, "agenthive_gaps_closed_total") == before


def test_promotions_and_demotions_are_counted_separately(live_server, client_lib):
    """Promotions without demotions means the thresholds only ratchet,
    which is the failure the tier design is meant to avoid - so the two
    have to be distinguishable."""
    c, _, _ = _team(client_lib, live_server)
    node = _approved(c, "Busy note")
    for _ in range(3):
        c.retrieve_context("Busy note")

    before_up = _metric(live_server, 'agenthive_tier_changes_applied_total',
                        '{direction="promote"}')
    promote = next(ch for ch in c.suggest_tiers()["changes"] if ch["id"] == node["id"])
    assert promote["direction"] == "promote"
    c.apply_tiers([promote])
    after_up = _metric(live_server, 'agenthive_tier_changes_applied_total',
                       '{direction="promote"}')
    assert after_up == before_up + 1

    # And the reverse direction is a different series, not the same one.
    before_down = _metric(live_server, 'agenthive_tier_changes_applied_total',
                          '{direction="demote"}')
    c.apply_tiers([{"id": node["id"], "proposed_tier": "L1", "direction": "demote"}])
    assert _metric(live_server, 'agenthive_tier_changes_applied_total',
                   '{direction="demote"}') == before_down + 1
    assert _metric(live_server, 'agenthive_tier_changes_applied_total',
                   '{direction="promote"}') == after_up


def test_open_gaps_and_tier_distribution_are_visible_at_scrape_time(live_server, client_lib):
    """Proposals raised but never acted on is a real outcome, and the
    dashboard has to be able to show it: open_gaps climbing while
    gaps_closed stays flat, or every node sitting at L1 forever."""
    c, _, _ = _team(client_lib, live_server)
    node = _approved(c, "Tiered note")
    for _ in range(2):
        c.retrieve_context("Nothing answers this")

    assert _metric(live_server, "agenthive_open_gaps") >= 1
    assert _metric(live_server, "agenthive_memory_nodes_by_tier", '{tier="L1"}') >= 1

    for _ in range(3):
        c.retrieve_context("Tiered note")
    change = next(ch for ch in c.suggest_tiers()["changes"] if ch["id"] == node["id"])
    c.apply_tiers([change])

    # The node moved tier, so the distribution moved with it.
    assert _metric(live_server, "agenthive_memory_nodes_by_tier",
                   '{tier="' + change["proposed_tier"] + '"}') >= 1


def test_proposals_are_counted_by_kind(live_server, client_lib):
    """What the layer FOUND, against what was acted on. A high rule
    count with no rules ever created is the loop being ignored."""
    c, _, _ = _team(client_lib, live_server)
    for i in range(6):
        _approved(c, f"Routine {i}", tags=["ci-noise"])

    before = _metric(live_server, "agenthive_proposals_raised_total", '{kind="rule"}')
    c.suggest_rules()
    assert _metric(live_server, "agenthive_proposals_raised_total",
                   '{kind="rule"}') > before
