import time

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


def test_token_never_expires_by_default(live_server, client_lib):
    """AGENTHIVE_TOKEN_TTL_SECONDS defaults to 0 (see config.py) - the
    original behavior, tokens are valid until explicitly rotated/revoked.
    The bootstrap admin token from create_team should carry no expiry."""
    team, admin = _team(live_server, client_lib)
    assert admin.get("token_expires_at") is None


def test_expired_token_is_rejected_like_a_revoked_one(live_server_token_ttl, client_lib):
    """With AGENTHIVE_TOKEN_TTL_SECONDS=2 (see conftest.py's
    live_server_token_ttl), a token minted now is valid immediately but
    rejected once the TTL elapses - same 401 as an invalid/revoked token,
    see db.py's user_by_token."""
    data = client_lib.create_team(live_server_token_ttl, "TTL Co")
    team, admin = data["team"], data["user"]
    assert admin["token_expires_at"] is not None

    c = client_lib.TeamMemoryClient(live_server_token_ttl, admin["token"], team["id"])
    c.list_users()  # valid right away

    time.sleep(2.5)
    with pytest.raises(RuntimeError, match="401"):
        c.list_users()


def test_rotating_a_token_grants_a_fresh_expiry(live_server_token_ttl, client_lib):
    """A rotated token gets its own new TTL from the moment it's rotated,
    not the original creation time - so rotating is a genuine reset, not
    just a new secret with the old clock still running."""
    data = client_lib.create_team(live_server_token_ttl, "TTL Rotate Co")
    team, admin = data["team"], data["user"]
    admin_c = client_lib.TeamMemoryClient(live_server_token_ttl, admin["token"], team["id"])
    member = admin_c.create_user("engineer", role="member")

    time.sleep(1.0)  # half the 2s TTL elapsed, but not expired yet
    rotated = admin_c.rotate_token(member["id"])
    assert rotated["token_expires_at"] is not None

    rotated_c = client_lib.TeamMemoryClient(live_server_token_ttl, rotated["token"], team["id"])
    rotated_c.retrieve_context("nothing yet")  # still valid past the original token's window

    time.sleep(2.5)
    with pytest.raises(RuntimeError, match="401"):
        rotated_c.retrieve_context("nothing yet")


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
