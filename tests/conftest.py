"""
conftest.py - spins up a real server.py subprocess per test module against
an isolated SQLite file, the same "test against a real running server"
philosophy as the original test_service.py. This proves wire-level
behavior (headers, status codes, JSON shapes), not just the Store class.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(url: str, retries: int = 50):
    for _ in range(retries):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.URLError:
            time.sleep(0.1)
    raise RuntimeError(f"server never came up at {url}")


@pytest.fixture()
def live_server(tmp_path):
    """Yields a base_url for a freshly started server.py.

    If DATABASE_URL is set in the environment (CI's Postgres job), the
    server runs against that Postgres instance - rows accumulate across
    tests but every test uses freshly generated team/user/agent ids, so
    there's no cross-test interference. Otherwise each test gets its own
    throwaway SQLite file for full isolation."""
    port = _free_port()
    db_path = str(tmp_path / "test.db")
    env = dict(os.environ)
    env["AGENTHIVE_RATE_LIMIT_PER_MINUTE"] = "100000"  # tests shouldn't trip the limiter
    env["AGENTHIVE_LOG_JSON"] = "true"
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port), "--db", db_path,
         "--host", "127.0.0.1"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for(base_url + "/healthz")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def live_server_rate_limited(tmp_path):
    """Same as live_server but with a deliberately tight rate limit, for
    tests that need to actually trip it."""
    port = _free_port()
    db_path = str(tmp_path / "test.db")
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env["AGENTHIVE_RATE_LIMIT_PER_MINUTE"] = "3"
    env["AGENTHIVE_RATE_LIMIT_WINDOW_SECONDS"] = "60"
    env["AGENTHIVE_LOG_JSON"] = "true"
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port), "--db", db_path,
         "--host", "127.0.0.1"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for(base_url + "/healthz")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def live_server_token_ttl(tmp_path):
    """Same as live_server, but AGENTHIVE_TOKEN_TTL_SECONDS=2 - every token
    minted while this is running expires almost immediately, so
    tests/test_auth.py can prove expiry is enforced without waiting
    around for a realistic TTL. 2s (not 1s) leaves enough margin for a
    couple of real HTTP round-trips to land before the window closes."""
    port = _free_port()
    db_path = str(tmp_path / "test.db")
    env = dict(os.environ)
    env["AGENTHIVE_RATE_LIMIT_PER_MINUTE"] = "100000"
    env["AGENTHIVE_LOG_JSON"] = "true"
    env["AGENTHIVE_TOKEN_TTL_SECONDS"] = "2"
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port), "--db", db_path,
         "--host", "127.0.0.1"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for(base_url + "/healthz")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def live_server_redis(tmp_path):
    """Same as live_server, but with REDIS_URL set so the rate limiter and
    retrieval cache both run against Redis instead of falling back to
    in-memory. Skips if REDIS_URL isn't provided (CI sets it; local runs
    can too - see tests/README or just `redis-server &`)."""
    redis_url = os.environ.get("REDIS_URL_FOR_TESTS", "redis://127.0.0.1:6379/0")
    try:
        import redis
        redis.from_url(redis_url, socket_connect_timeout=1).ping()
    except Exception:
        pytest.skip("no reachable Redis at " + redis_url + " - set REDIS_URL_FOR_TESTS or run redis-server")

    port = _free_port()
    db_path = str(tmp_path / "test.db")
    env = dict(os.environ)
    env["REDIS_URL"] = redis_url
    env["AGENTHIVE_RATE_LIMIT_PER_MINUTE"] = "100000"
    env["AGENTHIVE_LOG_JSON"] = "true"
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port), "--db", db_path,
         "--host", "127.0.0.1"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for(base_url + "/healthz")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # best-effort cleanup of the keys this run created
        try:
            import redis
            r = redis.from_url(redis_url)
            for key in r.scan_iter("agenthive:*"):
                r.delete(key)
        except Exception:
            pass


@pytest.fixture()
def live_server_tracing(tmp_path):
    """Same as live_server, but with AGENTHIVE_TRACING_CONSOLE=true so
    every finished span is printed as one line of JSON to the process's
    stdout - which we capture (stdout=PIPE) so tests/test_tracing.py can
    parse it back out. Yields (base_url, proc); the test is responsible
    for calling proc.terminate() + proc.communicate() itself once it's
    done making calls, so it can read the captured spans before the
    fixture's own teardown runs (a second terminate()/wait() on an
    already-exited process is harmless)."""
    port = _free_port()
    db_path = str(tmp_path / "test.db")
    env = dict(os.environ)
    env["AGENTHIVE_RATE_LIMIT_PER_MINUTE"] = "100000"
    env["AGENTHIVE_LOG_JSON"] = "true"
    env["AGENTHIVE_TRACING_CONSOLE"] = "true"
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port), "--db", db_path,
         "--host", "127.0.0.1"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for(base_url + "/healthz")
        yield base_url, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def live_server_tracing_sampled_out(tmp_path):
    """Same as live_server_tracing, but AGENTHIVE_TRACE_SAMPLE_RATIO=0 - no
    trace should ever be sampled in, so no span should ever print. Proves
    the sampling knob actually suppresses export, not just that it's read
    without error (see tests/test_tracing.py)."""
    port = _free_port()
    db_path = str(tmp_path / "test.db")
    env = dict(os.environ)
    env["AGENTHIVE_RATE_LIMIT_PER_MINUTE"] = "100000"
    env["AGENTHIVE_LOG_JSON"] = "true"
    env["AGENTHIVE_TRACING_CONSOLE"] = "true"
    env["AGENTHIVE_TRACE_SAMPLE_RATIO"] = "0"
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port), "--db", db_path,
         "--host", "127.0.0.1"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for(base_url + "/healthz")
        yield base_url, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def client_lib():
    sys.path.insert(0, ROOT)
    import client as client_module
    return client_module
