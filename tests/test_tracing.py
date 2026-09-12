"""
test_tracing.py - proves the write -> review -> retrieve journey is
actually instrumented, against a real running server.py (same philosophy
as every other test here - see conftest.py). Uses the console exporter
(AGENTHIVE_TRACING_CONSOLE=true, see live_server_tracing in conftest.py)
rather than a real OTLP backend, so this suite needs no extra
infrastructure - each finished span prints as one line of JSON to
stdout, which we capture and parse back out.

This doesn't (and can't, without a real collector) prove OTLP export
works end to end - that was verified manually against a real Jaeger
container during development (see ADR.md's "v3" section). What this DOES
prove: tracing.py's plumbing is correct (spans get created, named,
attributed, and closed at the right points) and context propagation
actually links a client call to the server spans it triggers - the part
most likely to silently break.
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_spans(proc, timeout=5):
    """Terminates `proc`, drains its captured stdout, and returns the list
    of parsed span dicts (anything printed as a single-line JSON object
    with a "name" key - structured request-log lines are JSON too but
    don't have span-shaped fields, so they're naturally filtered out)."""
    proc.terminate()
    out, _ = proc.communicate(timeout=timeout)
    text = out.decode("utf-8", errors="replace")
    spans = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "name" in obj and "context" in obj and "trace_id" in obj.get("context", {}):
            spans.append(obj)
    return spans


def test_console_exporter_prints_spans_for_the_write_review_retrieve_journey(
    live_server_tracing, client_lib
):
    base_url, proc = live_server_tracing
    data = client_lib.create_team(base_url, "Tracing Co")
    team, owner = data["team"], data["user"]
    admin = client_lib.TeamMemoryClient(base_url, owner["token"], team["id"])

    node = admin.log_session(title="Trace Anchor", body="testing tracing end to end", tags=[])
    admin.approve(node["id"])
    admin.retrieve_context("Trace Anchor")

    time.sleep(0.2)  # let the last response finish leaving the socket before we terminate
    spans = _read_spans(proc)
    names = {s["name"] for s in spans}

    # the root span per HTTP call, renamed from "METHOD /path" to
    # "METHOD route_name" once the route is matched (see server.py's
    # _dispatch) - proves request-level tracing works.
    assert "POST create_memory" in names
    assert "POST approve" in names
    assert "GET retrieve_endpoint" in names

    # the child spans that make this useful for "where did the time go" -
    # one per meaningful step in the journey, not just the HTTP wrapper.
    assert "auth.authenticate" in names
    assert "ratelimit.check" in names
    assert "memory.write" in names
    assert "memory.review" in names
    assert "cache.lookup" in names
    assert "retrieval.traverse" in names
    assert "access_log.record" in names


def test_retrieval_span_carries_useful_attributes(live_server_tracing, client_lib):
    base_url, proc = live_server_tracing
    data = client_lib.create_team(base_url, "Tracing Co 2")
    team, owner = data["team"], data["user"]
    admin = client_lib.TeamMemoryClient(base_url, owner["token"], team["id"])

    node = admin.log_session(title="Attr Anchor", body="x" * 40, tags=[])
    admin.approve(node["id"])
    admin.retrieve_context("Attr Anchor", hops=3, hub_cutoff=7)

    time.sleep(0.2)
    spans = _read_spans(proc)
    traversal = next(s for s in spans if s["name"] == "retrieval.traverse")
    attrs = traversal["attributes"]

    assert attrs["agenthive.anchor"] == "Attr Anchor"
    assert attrs["agenthive.hops"] == 3
    assert attrs["agenthive.hub_cutoff"] == 7
    assert attrs["agenthive.neighborhood_count"] >= 1


def test_sample_ratio_zero_suppresses_every_span(live_server_tracing_sampled_out, client_lib):
    """AGENTHIVE_TRACE_SAMPLE_RATIO=0 (see config.py/tracing.py) means the
    sampler drops every trace at the root span - the same write -> review
    -> retrieve journey that reliably prints ~10 spans at the default
    ratio (see the first test above) should print none."""
    base_url, proc = live_server_tracing_sampled_out
    data = client_lib.create_team(base_url, "Sampled Out Co")
    team, owner = data["team"], data["user"]
    admin = client_lib.TeamMemoryClient(base_url, owner["token"], team["id"])

    node = admin.log_session(title="Never Sampled", body="testing ratio=0", tags=[])
    admin.approve(node["id"])
    admin.retrieve_context("Never Sampled")

    time.sleep(0.2)
    assert _read_spans(proc) == []


def test_client_and_server_spans_share_a_trace_id_via_propagation(live_server_tracing):
    """The whole point of propagating traceparent (see tracing.py's
    inject_headers/extract_context) is that a client call and the server
    spans it triggers end up in the SAME trace, not two disconnected ones.

    Runs the client side as its own fresh subprocess (rather than
    importing client.py into the already-running pytest process, where
    config.py/tracing.py may have already been imported with tracing off)
    so its environment is unambiguous from the start - the same pattern
    tests/test_redis_backends.py uses for two real server processes."""
    base_url, proc = live_server_tracing

    script = (
        "import client\n"
        "data = client.create_team(%r, 'Tracing Co 3')\n"
        "team, owner = data['team'], data['user']\n"
        "c = client.TeamMemoryClient(%r, owner['token'], team['id'])\n"
        "with c.traced_session('test_session'):\n"
        "    c.log_session(title='Propagation Anchor', body='x' * 20, tags=[])\n"
        "    c.retrieve_context('Propagation Anchor')\n"
    ) % (base_url, base_url)

    env = dict(os.environ)
    env["AGENTHIVE_TRACING_CONSOLE"] = "true"
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, cwd=ROOT, timeout=15,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    time.sleep(0.2)
    server_spans = _read_spans(proc)
    server_trace_ids = {
        s["context"]["trace_id"] for s in server_spans
        if s["name"] in ("POST create_memory", "GET retrieve_endpoint")
    }

    # Both server-side calls landed in the SAME trace (the client wrapped
    # them in one traced_session), and that trace has exactly one id -
    # proof the traceparent header actually carried the context across
    # the network hop rather than each call starting its own trace.
    assert len(server_trace_ids) == 1
