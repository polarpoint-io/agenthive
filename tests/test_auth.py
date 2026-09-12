import pytest


def _team(live_server, client_lib, name="Team"):
    data = client_lib.create_team(live_server, name)
    return data["team"], data["user"]


def test_missing_api_key_is_rejected(live_server, client_lib):
    team, admin = _team(live_server, client_lib)
    c = client_lib.TeamMemoryClient(live_server, "", team["id"])
    with pytest.raises(RuntimeError, match="401"):
        c.list_pending()


def test_invalid_api_key_is_rejected(live_server, client_lib):
    team, admin = _team(live_server, client_lib)
    c = client_lib.TeamMemoryClient(live_server, "ah-not-a-real-token", team["id"])
    with pytest.raises(RuntimeError, match="401"):
        c.list_pending()


def test_member_cannot_approve_but_can_log_and_retrieve(live_server, client_lib):
    team, admin = _team(live_server, client_lib)
    admin_c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])
    member = admin_c.create_user("engineer", role="member")
    member_c = client_lib.TeamMemoryClient(live_server, member["token"], team["id"])

    node = member_c.log_session(title="Note", body="body")
    with pytest.raises(RuntimeError, match="403"):
        member_c.approve(node["id"])

    # admin can approve it
    admin_c.approve(node["id"])
    result = member_c.retrieve_context("Note")
    assert result["neighborhood_count"] == 1


def test_member_cannot_manage_users(live_server, client_lib):
    team, admin = _team(live_server, client_lib)
    admin_c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])
    member = admin_c.create_user("engineer", role="member")
    member_c = client_lib.TeamMemoryClient(live_server, member["token"], team["id"])

    with pytest.raises(RuntimeError, match="403"):
        member_c.create_user("someone-else")


def test_revoked_user_is_locked_out(live_server, client_lib):
    team, admin = _team(live_server, client_lib)
    admin_c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])
    member = admin_c.create_user("temp-contractor", role="member")
    member_c = client_lib.TeamMemoryClient(live_server, member["token"], team["id"])

    member_c.list_pending()  # works before revocation
    admin_c.revoke_user(member["id"])

    with pytest.raises(RuntimeError, match="401"):
        member_c.list_pending()


def test_token_rotation_invalidates_old_token(live_server, client_lib):
    team, admin = _team(live_server, client_lib)
    admin_c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])
    member = admin_c.create_user("engineer", role="member")
    member_c = client_lib.TeamMemoryClient(live_server, member["token"], team["id"])

    rotated = admin_c.rotate_token(member["id"])
    with pytest.raises(RuntimeError, match="401"):
        member_c.list_pending()  # old token, now stale

    member_c.api_key = rotated["token"]
    member_c.list_pending()  # new token works


def test_self_rotate_allowed_for_member(live_server, client_lib):
    team, admin = _team(live_server, client_lib)
    admin_c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])
    member = admin_c.create_user("engineer", role="member")
    member_c = client_lib.TeamMemoryClient(live_server, member["token"], team["id"])

    rotated = member_c.rotate_token(member["id"])
    assert rotated["token"] != member["token"]
