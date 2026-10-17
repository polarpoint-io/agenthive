"""
The operational metrics: review-queue health, retrieval quality, auth
failures, storage liveness.

These cover the things that fail SILENTLY. A backed-up review queue
throws no error - pending memory is simply invisible to retrieval, so
every agent gets quietly worse context. Anchors that stop matching
titles don't error either; they just return a weaker neighborhood. The
point of each metric here is to make a silent degradation visible, so
each test checks the series actually moves.
"""
import urllib.error
import urllib.request

import pytest


def _metrics(base_url):
    with urllib.request.urlopen(base_url + "/metrics", timeout=10) as resp:
        return resp.read().decode()


def _value(text, name, labels=""):
    target = name + labels
    for line in text.splitlines():
        if line.startswith(target + " "):
            return float(line.split()[1])
    return 0.0


def _team(client_lib, live_server, name="Platform Team"):
    team, admin = client_lib.create_team(live_server, name).values()
    return client_lib.TeamMemoryClient(live_server, admin["token"], team["id"]), team, admin


# --- review queue health ----------------------------------------------

def test_review_latency_is_observed_on_both_decisions(live_server, client_lib):
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Approve me", body="b")
    c.log_session(title="Reject me", body="b")
    pending = {n["title"]: n["id"] for n in c.list_pending()["pending"]}

    before = _value(_metrics(live_server), "agenthive_review_latency_seconds_count")
    c.approve(pending["Approve me"])
    c.reject(pending["Reject me"], reason="no")
    after = _value(_metrics(live_server), "agenthive_review_latency_seconds_count")

    # A rejection is a review decision too - leaving it out would make the
    # queue look faster than it is.
    assert after == before + 2


def test_oldest_pending_age_tracks_the_queue(live_server, client_lib):
    """The number to page on: pending memory is invisible to retrieval,
    so this climbing is context degrading with no error anywhere."""
    c, _, _ = _team(client_lib, live_server)
    assert _value(_metrics(live_server), "agenthive_oldest_pending_age_seconds") == 0.0

    c.log_session(title="Waiting", body="b")
    assert _value(_metrics(live_server), "agenthive_oldest_pending_age_seconds") >= 0.0
    assert "agenthive_oldest_pending_age_seconds" in _metrics(live_server)

    c.approve(c.list_pending()["pending"][0]["id"])
    assert _value(_metrics(live_server), "agenthive_oldest_pending_age_seconds") == 0.0


def test_the_queue_composition_is_visible_by_reserved_tag(live_server, client_lib):
    """A queue that is mostly one tag has one upstream cause."""
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="A bug", body="b", tags=["bugfix"])
    c.log_session(title="Plain", body="b")

    text = _metrics(live_server)
    assert _value(text, "agenthive_writes_by_reserved_tag_total", '{tag="bugfix"}') >= 1
    assert _value(text, "agenthive_writes_by_reserved_tag_total", '{tag="none"}') >= 1


def test_free_form_tags_do_not_become_metric_labels(live_server, client_lib):
    """Caller-controlled labels are how a metrics endpoint turns into a
    memory leak. Only the reserved vocabulary is allowed through."""
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Odd", body="b", tags=["some-ad-hoc-label-12345"])
    assert "some-ad-hoc-label-12345" not in _metrics(live_server)


# --- retrieval quality ------------------------------------------------

def test_anchor_resolution_distinguishes_exact_from_fallback(live_server, client_lib):
    """A graph where most retrievals fall back has titles that don't
    match how people phrase things, which traversal tuning won't fix."""
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Postgres connection pool exhaustion", body="b" * 50)
    c.approve(c.list_pending()["pending"][0]["id"])

    before_exact = _value(_metrics(live_server),
                          "agenthive_anchor_resolution_total", '{resolution="exact"}')
    c.retrieve_context("Postgres connection pool exhaustion")
    assert _value(_metrics(live_server), "agenthive_anchor_resolution_total",
                  '{resolution="exact"}') == before_exact + 1

    before_fb = _value(_metrics(live_server),
                       "agenthive_anchor_resolution_total", '{resolution="fallback"}')
    c.retrieve_context("connection pool")          # partial, not a title
    assert _value(_metrics(live_server), "agenthive_anchor_resolution_total",
                  '{resolution="fallback"}') == before_fb + 1

    before_none = _value(_metrics(live_server),
                         "agenthive_anchor_resolution_total", '{resolution="none"}')
    c.retrieve_context("entirely unrelated phrase")
    assert _value(_metrics(live_server), "agenthive_anchor_resolution_total",
                  '{resolution="none"}') == before_none + 1


def test_neighborhood_size_is_observed_including_empty(live_server, client_lib):
    """An empty retrieval is a real observation - dropping it would make
    the distribution look healthier than it is."""
    c, _, _ = _team(client_lib, live_server)
    before = _value(_metrics(live_server), "agenthive_retrieval_neighborhood_size_count")
    c.retrieve_context("nothing here")
    assert _value(_metrics(live_server),
                  "agenthive_retrieval_neighborhood_size_count") == before + 1


def test_index_mode_reports_the_same_resolution_as_full(live_server, client_lib):
    """The index is a projection of the same traversal, so it must not
    look like a different kind of match."""
    c, _, _ = _team(client_lib, live_server)
    c.log_session(title="Exact title here", body="b" * 50)
    c.approve(c.list_pending()["pending"][0]["id"])
    assert c.retrieve_index("Exact title here")["resolution"] == "exact"


# --- auth and storage -------------------------------------------------

def test_auth_failures_are_classified_without_the_response_saying_so(live_server, client_lib):
    """The response stays one opaque message so a stolen-token probe
    learns nothing. The metrics port is a different audience."""
    c, team, _ = _team(client_lib, live_server)
    victim = c.create_user("bob", role="member")
    c.revoke_user(victim["id"])

    revoked_client = client_lib.TeamMemoryClient(live_server, victim["token"], team["id"])
    with pytest.raises(RuntimeError) as err:
        revoked_client.report_gaps()

    # The wire still refuses to say which case applies.
    assert "revoked" in str(err.value) and "expired" in str(err.value)

    text = _metrics(live_server)
    assert _value(text, "agenthive_auth_failures_total", '{reason="revoked"}') >= 1

    bogus = client_lib.TeamMemoryClient(live_server, "not-a-real-token", team["id"])
    with pytest.raises(RuntimeError):
        bogus.report_gaps()
    assert _value(_metrics(live_server), "agenthive_auth_failures_total",
                  '{reason="unknown"}') >= 1


def test_a_missing_header_is_its_own_reason(live_server):
    req = urllib.request.Request(live_server + "/teams/whatever/gaps")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(req, timeout=10)
    assert _value(_metrics(live_server), "agenthive_auth_failures_total",
                  '{reason="missing_header"}') >= 1


def test_db_up_is_reported(live_server):
    assert _value(_metrics(live_server), "agenthive_db_up") == 1.0
