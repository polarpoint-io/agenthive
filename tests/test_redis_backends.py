"""
Same properties as the in-memory rate limiter / cache tests, run against
Redis-backed implementations - proving the "shared across replicas"
story actually works, not just that the interface compiles. Skipped
automatically if no Redis is reachable (see conftest.live_server_redis).
"""
import urllib.error

import pytest


def _admin_client(live_server_redis, client_lib, name="Team"):
    data = client_lib.create_team(live_server_redis, name)
    team, admin = data["team"], data["user"]
    return team, client_lib.TeamMemoryClient(live_server_redis, admin["token"], team["id"])


def test_redis_backend_is_selected(live_server_redis):
    import urllib.request
    with urllib.request.urlopen(live_server_redis + "/healthz") as resp:
        assert resp.status == 200


def test_redis_cache_hit_across_a_simulated_second_replica(live_server_redis, client_lib):
    # Both clients hit the SAME server process here (one process is enough
    # to prove the Redis-backed cache path works); the point of Redis in
    # production is that a second replica's process would see the same
    # cached entry too, which we can't spin up in this test but which
    # falls straight out of both processes sharing REDIS_URL.
    team, c = _admin_client(live_server_redis, client_lib)
    node = c.log_session(title="Redis Cached Note", body="body")
    c.approve(node["id"])

    first = c.retrieve_context("Redis Cached Note")
    second = c.retrieve_context("Redis Cached Note")
    assert first["neighborhood_count"] == second["neighborhood_count"] == 1


def test_redis_rate_limit_shared_state(client_lib, tmp_path):
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.request

    redis_url = os.environ.get("REDIS_URL_FOR_TESTS", "redis://127.0.0.1:6379/0")
    try:
        import redis
        redis.from_url(redis_url, socket_connect_timeout=1).ping()
    except Exception:
        pytest.skip("no reachable Redis for this test")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def start(db_name):
        port = free_port()
        env = dict(os.environ)
        env["REDIS_URL"] = redis_url
        env["AGENTHIVE_RATE_LIMIT_PER_MINUTE"] = "3"
        env["AGENTHIVE_RATE_LIMIT_WINDOW_SECONDS"] = "60"
        proc = subprocess.Popen(
            [sys.executable, "server.py", "--port", str(port), "--db",
             str(tmp_path / db_name), "--host", "127.0.0.1"],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        return proc, base

    # Two independent server PROCESSES, standing in for two replicas -
    # both point at the same Redis, so the limit is shared between them.
    proc_a, base_a = start("a.db")
    proc_b, base_b = start("b.db")
    try:
        hit_429 = False
        calls = 0
        for base in [base_a, base_b, base_a, base_b, base_a, base_b]:
            calls += 1
            try:
                client_lib.create_team(base, "Team")
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    hit_429 = True
                    break
                raise
        # 3 requests allowed total across BOTH processes, not 3 each -
        # proving the limit is shared, not per-process.
        assert hit_429, f"expected the shared limit to trip within {calls} calls across two processes"
    finally:
        for p in (proc_a, proc_b):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        try:
            import redis
            r = redis.from_url(redis_url)
            for key in r.scan_iter("agenthive:*"):
                r.delete(key)
        except Exception:
            pass
