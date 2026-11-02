"""
Adaptive disclosure: the server decides, because the agent cannot.

Progressive disclosure was opt-in, and opt-in did not work. An agent
has no way to know a neighbourhood is wide BEFORE retrieving it, so it
took the documented default every time and the mechanism that saves
tokens never ran. The server already knows the size - it just computed
it.
"""
import subprocess
import sys

import pytest


def _team(client_lib, base, name="T"):
    team, admin = client_lib.create_team(base, name).values()
    return client_lib.TeamMemoryClient(base, admin["token"], team["id"]), team, admin


def _approved(c, title, body):
    c.log_session(title=title, body=body)
    node = next(n for n in c.list_pending()["pending"] if n["title"] == title)
    c.approve(node["id"])
    return node


def _metric(base_url, name):
    import urllib.request
    with urllib.request.urlopen(base_url + "/metrics", timeout=10) as r:
        for line in r.read().decode().splitlines():
            if line.startswith(name + " "):
                return float(line.split()[1])
    return 0.0


def test_a_big_neighbourhood_comes_back_as_an_index_unasked(live_server, client_lib):
    """The point of the whole change: nobody had to opt in."""
    c, _, _ = _team(client_lib, live_server)
    _approved(c, "Big note", "x" * 8000)        # ~2000 tokens, over the 800 default

    result = c.retrieve_context("Big note")
    assert result["mode"] == "index"
    assert result["auto_index"] is True
    assert "body" not in result["neighborhood"][0]
    assert result["neighborhood"][0]["approx_tokens"] > 0
    assert "over the" in result["note"] and "mode=full" in result["note"]


def test_a_small_neighbourhood_still_comes_back_whole(live_server, client_lib):
    """Two round trips to save a few hundred tokens is a worse deal than
    one, so under the threshold nothing changes."""
    c, _, _ = _team(client_lib, live_server)
    _approved(c, "Small note", "short body")

    result = c.retrieve_context("Small note")
    assert result.get("auto_index") is None
    assert result["neighborhood"][0]["body"] == "short body"


def test_an_explicit_mode_is_always_honoured(live_server, client_lib):
    """Adaptive is for callers with no preference. One that asked gets
    what it asked for, however large."""
    c, _, _ = _team(client_lib, live_server)
    _approved(c, "Big note", "x" * 8000)

    forced = c._request("GET", f"/teams/{c.team_id}/memory/retrieve",
                        query={"anchor": "Big note", "mode": "full"})
    assert forced.get("auto_index") is None
    assert forced["neighborhood"][0]["body"]


def test_auto_switches_are_counted_separately(live_server, client_lib):
    """Opt-in index mode was never chosen by agents, so the existing
    by_mode counter could not show disclosure running."""
    c, _, _ = _team(client_lib, live_server)
    _approved(c, "Big note", "x" * 8000)
    before = _metric(live_server, "agenthive_auto_index_total")
    c.retrieve_context("Big note")
    assert _metric(live_server, "agenthive_auto_index_total") == before + 1


def test_what_memory_cost_is_measured_not_just_what_it_saved(live_server, client_lib):
    """tokens_avoided compares against dumping the whole graph, which
    nobody would run. This is the side of the ledger that is real."""
    c, _, _ = _team(client_lib, live_server)
    _approved(c, "Some note", "a body worth a few tokens")
    before = _metric(live_server, "agenthive_tokens_injected_total")
    c.retrieve_context("Some note")
    assert _metric(live_server, "agenthive_tokens_injected_total") > before


def test_a_miss_tells_the_caller_it_became_a_gap(live_server, client_lib):
    """A miss costs a round trip either way. Spend it on something."""
    c, _, _ = _team(client_lib, live_server)
    result = c.retrieve_context("nothing matches this phrasing")
    assert result["neighborhood_count"] == 0
    assert result["gap_recorded"] is True
    assert "log_session closes it" in result["note"]


def test_the_threshold_can_be_turned_off(tmp_path, client_lib):
    """Set to 0 and the old always-full behaviour returns."""
    import os
    import socket
    import time
    import urllib.request

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ)
    env["AGENTHIVE_AUTO_INDEX_OVER"] = "0"
    env["AGENTHIVE_RATE_LIMIT_PER_MINUTE"] = "100000"
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port), "--db",
         str(tmp_path / "t.db"), "--host", "127.0.0.1"],
        cwd=".", env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/healthz", timeout=1).close()
                break
            except Exception:
                time.sleep(0.1)
        c, _, _ = _team(client_lib, base)
        _approved(c, "Big note", "x" * 8000)
        assert c.retrieve_context("Big note").get("auto_index") is None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
