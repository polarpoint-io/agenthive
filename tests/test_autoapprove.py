def _admin_client(live_server, client_lib, name="Team"):
    data = client_lib.create_team(live_server, name)
    team, admin = data["team"], data["user"]
    return client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])


def test_matching_tag_rule_auto_approves(live_server, client_lib):
    c = _admin_client(live_server, client_lib)
    c.create_auto_approve_rule(match_tag="ci-noise")

    node = c.log_session(title="Flaky test retry", body="body", tags=["ci-noise"])
    assert node["status"] == "approved"
    assert node["auto_approved"] == 1

    result = c.retrieve_context("Flaky test retry")
    assert result["neighborhood_count"] == 1


def test_non_matching_tag_stays_pending(live_server, client_lib):
    c = _admin_client(live_server, client_lib)
    c.create_auto_approve_rule(match_tag="ci-noise")

    node = c.log_session(title="Real incident", body="body", tags=["postgres"])
    assert node["status"] == "pending"


def test_matching_agent_rule_auto_approves(live_server, client_lib):
    c = _admin_client(live_server, client_lib)
    agent = c.create_agent("trusted-bot")
    c.create_auto_approve_rule(match_agent_id=agent["id"])

    node = c.log_session(title="Routine note", body="body", agent_id=agent["id"])
    assert node["status"] == "approved"


def test_deleted_rule_stops_applying(live_server, client_lib):
    c = _admin_client(live_server, client_lib)
    rule = c.create_auto_approve_rule(match_tag="ci-noise")
    c.delete_auto_approve_rule(rule["id"])

    node = c.log_session(title="Another flaky test", body="body", tags=["ci-noise"])
    assert node["status"] == "pending"
