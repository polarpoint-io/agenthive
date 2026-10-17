"""
Automating the review gate - one half of it.

The asymmetry this file exists to pin: rejection is safe to automate
because its failure mode is a lost note somebody can write again, while
approval's failure mode is a bad memory visible to every agent on the
team for as long as it stays approved. So auto-reject liberally,
auto-approve only on a rule a human wrote, and never let either touch a
node a reserved tag says needs eyes.

Triage is the third thing, and it decides nothing: it reorders the queue
so a reviewer sees what is actually waiting on them first.
"""


def _team(client_lib, live_server, name="Platform Team"):
    team, admin = client_lib.create_team(live_server, name).values()
    return client_lib.TeamMemoryClient(live_server, admin["token"], team["id"]), team, admin


def _status_of(client, title):
    for status_list in ("pending",):
        for node in client.list_pending()[status_list]:
            if node["title"] == title:
                return "pending", node
    return None, None


# --- deterministic auto-rejection -------------------------------------

def test_an_identical_node_is_rejected_without_a_human(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Pgbouncer fix", body="raise max_client_conn to 400")
    c.approve(c.list_pending()["pending"][0]["id"])

    c.log_session(title="Pgbouncer fix", body="raise max_client_conn to 400")
    assert c.list_pending()["pending"] == []


def test_the_same_title_with_a_better_body_is_not_a_duplicate(live_server, client_lib):
    """An agent relogging a title with more detail is an improvement.
    Discarding it would lose the improvement and leave the weaker note
    in place, which is why the key is title AND body."""
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Pgbouncer fix", body="raise max_client_conn")
    c.approve(c.list_pending()["pending"][0]["id"])

    c.log_session(title="Pgbouncer fix",
                  body="raise max_client_conn to 400; also bump pool_mode")
    assert len(c.list_pending()["pending"]) == 1


def test_a_duplicate_of_something_still_pending_is_not_rejected(live_server, client_lib):
    """Only an APPROVED node is settled enough to deduplicate against.
    Something still in the queue may yet be rejected."""
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Same thing", body="same body")
    c.log_session(title="Same thing", body="same body")
    assert len(c.list_pending()["pending"]) == 2


def test_an_auto_rejected_node_is_recoverable_not_deleted(live_server, client_lib):
    """The safety property that makes auto-rejection acceptable at all."""
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Keeper", body="body")
    first = c.list_pending()["pending"][0]["id"]
    c.approve(first)
    c.log_session(title="Keeper", body="body")

    # Invisible to retrieval, but the row and the reason survive.
    rejected = c.list_rejected()["rejected"]
    assert len(rejected) == 1
    assert "identical to approved" in rejected[0]["reject_reason"]
    assert rejected[0]["body"] == "body"          # content intact, not blanked

    # And an admin can still approve it, which is what makes automating
    # rejection defensible in the first place.
    c.approve(rejected[0]["id"])
    assert c.retrieve_context("Keeper")["neighborhood_count"] >= 1


# --- rule-driven auto-rejection ---------------------------------------

def test_a_reject_rule_keeps_a_noisy_agent_out_of_the_queue(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    agent = c.create_agent("chatty")
    c.create_auto_reject_rule(match_agent_id=agent["id"], reason="prompt needs fixing")

    c.log_session(title="Noise", body="whatever", agent_id=agent["id"])
    c.log_session(title="From a human-run session", body="real content")

    titles = [n["title"] for n in c.list_pending()["pending"]]
    assert titles == ["From a human-run session"]


def test_rejection_wins_when_both_rules_match(live_server, client_lib):
    """A node matching an approve rule AND a reject rule is a config
    error. Resolving it toward 'does not enter the graph' is the
    conservative reading."""
    c, _, _ = _team(client_lib, live_server)
    c.create_auto_approve_rule(match_tag="ci-noise")
    c.create_auto_reject_rule(match_tag="ci-noise", reason="changed our minds")

    c.log_session(title="Contested", body="body", tags=["ci-noise"])
    assert c.retrieve_context("Contested")["neighborhood_count"] == 0
    assert c.list_pending()["pending"] == []


def test_a_reserved_tag_is_beyond_automation_in_both_directions(live_server, client_lib):
    """Auto-approving a security_alert hides it; auto-rejecting one
    discards it. Both are worse than a slow queue."""
    c, _, _ = _team(client_lib, live_server)
    agent = c.create_agent("scanner")
    c.create_auto_reject_rule(match_agent_id=agent["id"])
    c.create_auto_approve_rule(match_agent_id=agent["id"])

    c.log_session(title="Leaked key", body="needs eyes",
                  agent_id=agent["id"], tags=["security_alert"])

    pending = [n["title"] for n in c.list_pending()["pending"]]
    assert pending == ["Leaked key"]


def test_a_deactivated_reject_rule_stops_applying(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    rule = c.create_auto_reject_rule(match_tag="drop-me")
    c.delete_auto_reject_rule(rule["id"])
    c.log_session(title="Still wanted", body="body", tags=["drop-me"])
    assert len(c.list_pending()["pending"]) == 1


def test_reject_rules_do_not_cross_teams(live_server, client_lib):
    c1, _, _ = _team(client_lib, live_server, "One")
    c1.create_auto_reject_rule(match_tag="drop-me")
    c2, _, _ = _team(client_lib, live_server, "Two")
    c2.log_session(title="Team two note", body="body", tags=["drop-me"])
    assert len(c2.list_pending()["pending"]) == 1


# --- triage -----------------------------------------------------------

def test_the_queue_is_ordered_by_what_is_waiting_on_it(live_server, client_lib):
    """A node answering an anchor missed nine times outranks forty older
    backstop nodes. Age is the wrong order the moment the queue has
    anything in it."""
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Written first, nobody wants it", body="body")
    for _ in range(9):
        c.retrieve_context("Kafka consumer lag")
    c.log_session(title="Kafka consumer lag", body="raise max.poll.interval.ms")

    triaged = c.list_pending_triaged()
    assert triaged["pending"][0]["title"] == "Kafka consumer lag"
    assert triaged["pending"][0]["closes_gap"] is True
    assert triaged["pending"][0]["gap_miss_count"] == 9
    assert triaged["closes_gap"] == 1

    # And the default ordering is untouched for every existing caller.
    assert c.list_pending()["pending"][0]["title"] == "Written first, nobody wants it"


def test_triage_returns_everything_it_reorders(live_server, client_lib):
    """Triage decides nothing. Every node still comes back."""
    c, _, _ = _team(client_lib, live_server)
    for i in range(4):
        c.log_session(title=f"Note {i}", body="body")
    assert len(c.list_pending_triaged()["pending"]) == 4


def test_triage_flags_what_automation_must_not_touch(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Routine", body="body")
    c.log_session(title="Leaked key", body="body", tags=["security_alert"])

    flags = {n["title"]: n["needs_human"] for n in c.list_pending_triaged()["pending"]}
    assert flags["Leaked key"] is True
    assert flags["Routine"] is False


# --- metrics ----------------------------------------------------------

def test_auto_rejection_is_counted_by_cause(live_server, client_lib):
    """A too-broad rule discards knowledge quietly; nothing else would
    say so."""
    import urllib.request

    def value(name, labels):
        with urllib.request.urlopen(live_server + "/metrics", timeout=10) as resp:
            for line in resp.read().decode().splitlines():
                if line.startswith(name + labels + " "):
                    return float(line.split()[1])
        return 0.0

    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Dupe", body="body")
    c.approve(c.list_pending()["pending"][0]["id"])
    c.log_session(title="Dupe", body="body")
    assert value("agenthive_auto_rejected_total", '{cause="duplicate"}') >= 1

    agent = c.create_agent("noisy")
    c.create_auto_reject_rule(match_agent_id=agent["id"])
    c.log_session(title="Noise", body="body", agent_id=agent["id"])
    assert value("agenthive_auto_rejected_total", '{cause="rule"}') >= 1


def test_node_ids_never_become_metric_labels(live_server, client_lib):
    """The rejection reason carries node and rule ids. Those must stay
    out of the label set."""
    import urllib.request

    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Dupe", body="body")
    node_id = c.list_pending()["pending"][0]["id"]
    c.approve(node_id)
    c.log_session(title="Dupe", body="body")

    with urllib.request.urlopen(live_server + "/metrics", timeout=10) as resp:
        assert node_id not in resp.read().decode()


def test_rejecting_an_approved_node_stops_it_being_served_immediately(live_server, client_lib):
    """Approving invalidated the retrieval cache; rejecting did not. So a
    node a human had just pulled out of shared memory carried on being
    served to every agent for up to cache_ttl_seconds - the gate deciding
    something is wrong and nobody downstream finding out.

    The asymmetry is the bug. A rejection is the more urgent of the two
    directions, not the less.
    """
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Wrong answer", body="this turns out to be wrong")
    node = c.list_pending()["pending"][0]
    c.approve(node["id"])

    # Populate the cache for this anchor.
    assert c.retrieve_context("Wrong answer")["neighborhood_count"] == 1

    c.reject(node["id"], reason="turned out to be wrong")

    assert c.retrieve_context("Wrong answer")["neighborhood_count"] == 0
