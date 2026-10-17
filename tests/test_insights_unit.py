"""
The memory-processing layer in isolation - pure functions over rows, no
database and no running server, same spirit as test_retrieval_unit.py.

What matters here is the judgement encoded in the thresholds, not the
arithmetic: when a proposal is NOT made is the behaviour worth pinning,
because a suggestion engine that fires on thin evidence trains people to
ignore it.
"""
import insights


def _stat(node_id, title, tier="L1", hits=0, users=0, created="2026-01-01T00:00:00+00:00",
          last=None):
    return {"id": node_id, "title": title, "tier": tier, "created_at": created,
            "access_count": hits, "distinct_users": users, "last_accessed_at": last}


# --- gaps -------------------------------------------------------------

def test_a_single_miss_is_not_a_gap():
    """One miss is someone phrasing an anchor badly. Repeated misses are
    the graph being incomplete."""
    rows = [{"anchor": "typo anchor", "miss_count": 1, "last_missed_at": "x"}]
    assert insights.rank_gaps(rows, []) == []


def test_an_answer_already_in_the_queue_sorts_first():
    """A pending node that would have answered the anchor is a queue
    problem, not a knowledge problem - different owner, different fix."""
    rows = [
        {"anchor": "Unknown thing", "miss_count": 9, "last_missed_at": "x"},
        {"anchor": "Postgres pool", "miss_count": 2, "last_missed_at": "x"},
    ]
    pending = [{"id": "p1", "title": "Postgres pool", "body": "b", "tier": "L1", "links": []}]

    gaps = insights.rank_gaps(rows, pending)
    assert gaps[0]["anchor"] == "Postgres pool"      # despite a lower count
    assert gaps[0]["awaiting_review"] is True
    assert gaps[0]["pending_id"] == "p1"
    assert gaps[1]["awaiting_review"] is False


# --- links ------------------------------------------------------------

def _node(node_id, title, links=()):
    return {"id": node_id, "title": title, "links": list(links), "body": "", "tier": "L1"}


def test_links_that_already_exist_are_not_suggested():
    nodes = [_node("a", "Alpha", ["Beta"]), _node("b", "Beta")]
    pairs = [{"a_id": "a", "b_id": "b", "shared": 10}]
    assert insights.suggest_links(pairs, nodes) == []


def test_an_existing_link_counts_in_either_direction():
    """build_graph makes adjacency bidirectional, so a one-way link is
    already a two-way edge and re-suggesting it is noise."""
    nodes = [_node("a", "Alpha"), _node("b", "Beta", ["Alpha"])]
    pairs = [{"a_id": "a", "b_id": "b", "shared": 10}]
    assert insights.suggest_links(pairs, nodes) == []


def test_a_genuinely_missing_edge_is_suggested():
    nodes = [_node("a", "Alpha"), _node("b", "Beta")]
    pairs = [{"a_id": "a", "b_id": "b", "shared": 4}]
    out = insights.suggest_links(pairs, nodes)
    assert len(out) == 1
    assert out[0]["a_title"] == "Alpha" and out[0]["b_title"] == "Beta"
    assert out[0]["shared_retrievals"] == 4


def test_one_shared_retrieval_is_a_coincidence_not_a_relationship():
    nodes = [_node("a", "Alpha"), _node("b", "Beta")]
    assert insights.suggest_links([{"a_id": "a", "b_id": "b", "shared": 1}], nodes) == []


def test_a_pair_whose_node_was_rejected_since_is_dropped():
    nodes = [_node("a", "Alpha")]          # b is no longer approved
    pairs = [{"a_id": "a", "b_id": "b", "shared": 9}]
    assert insights.suggest_links(pairs, nodes) == []


# --- tiers ------------------------------------------------------------

def test_tiers_are_earned_by_distinct_people_not_repeat_hits():
    """Five retrievals by one person is one person's workflow. Three
    different teammates is the graph being shared, which is the thing
    tiers are meant to record."""
    solo = insights.propose_tiers([_stat("a", "A", hits=20, users=1)])
    assert solo[0]["proposed_tier"] == "L2"

    shared = insights.propose_tiers([_stat("b", "B", hits=6, users=3)])
    assert shared[0]["proposed_tier"] == "L3"


def test_tiers_demote_as_well_as_promote():
    """Computed from current stats rather than ratcheted, so a node that
    was useful and is now dead drops back on its own."""
    out = insights.propose_tiers([_stat("a", "A", tier="L3", hits=0, users=0)])
    assert out[0]["direction"] == "demote"
    assert out[0]["proposed_tier"] == "L1"


def test_nodes_already_at_the_right_tier_are_not_proposed():
    assert insights.propose_tiers([_stat("a", "A", tier="L1", hits=0, users=0)]) == []


def test_stale_means_old_AND_never_retrieved():
    now = "2026-06-01T00:00:00+00:00"
    old_unused = _stat("a", "A", hits=0, created="2026-01-01T00:00:00+00:00")
    old_used = _stat("b", "B", hits=5, users=2, created="2026-01-01T00:00:00+00:00")
    new_unused = _stat("c", "C", hits=0, created="2026-05-28T00:00:00+00:00")

    stale = insights.find_stale([old_unused, old_used, new_unused], now_iso=now)
    assert [s["id"] for s in stale] == ["a"]


# --- rules ------------------------------------------------------------

def _decision(status, tags=(), agent=None, auto=False):
    return {"id": "x", "agent_id": agent, "tags": list(tags),
            "status": status, "auto_approved": auto, "rule_id": None}


def test_a_consistently_approved_tag_becomes_a_candidate_rule():
    decisions = [_decision("approved", ["ci-noise"]) for _ in range(8)]
    found = insights.mine_rules(decisions)
    assert found["rules"][0]["value"] == "ci-noise"
    assert found["rules"][0]["precision"] == 1.0


def test_thin_evidence_is_not_enough():
    """A rule skips human review entirely, so the bar is higher than
    'usually fine'."""
    decisions = [_decision("approved", ["rare"]) for _ in range(3)]
    assert insights.mine_rules(decisions)["rules"] == []


def test_reserved_tags_are_never_proposed_however_good_the_record():
    """The whole point of the guardrail is that a security_alert gets a
    human regardless of how routine it has looked so far."""
    decisions = [_decision("approved", ["security_alert"]) for _ in range(50)]
    found = insights.mine_rules(decisions)
    assert [r["value"] for r in found["rules"]] == []


def test_auto_approved_nodes_are_not_evidence_for_more_rules():
    """Counting a rule's own output as justification would let one hasty
    rule license its successors."""
    decisions = [_decision("approved", ["ci-noise"], auto=True) for _ in range(40)]
    assert insights.mine_rules(decisions)["rules"] == []


def test_a_rule_that_already_exists_is_not_proposed_again():
    decisions = [_decision("approved", ["ci-noise"]) for _ in range(10)]
    existing = [{"match_tag": "ci-noise", "match_agent_id": None}]
    assert insights.mine_rules(decisions, existing)["rules"] == []


def test_a_mostly_rejected_writer_is_surfaced_as_noisy_not_as_a_rule():
    """The fix for that agent is upstream in its prompt, not anywhere in
    the review queue."""
    decisions = ([_decision("rejected", agent="agent-7") for _ in range(9)]
                 + [_decision("approved", agent="agent-7")])
    found = insights.mine_rules(decisions)
    assert found["rules"] == []
    assert found["noisy"][0]["value"] == "agent-7"
    assert found["noisy"][0]["precision"] == 0.1
