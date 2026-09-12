"""
Proves server.py actually serves HTTPS when AGENTHIVE_TLS_CERT_FILE /
AGENTHIVE_TLS_KEY_FILE are set - the native-TLS half of closing the
"no built-in TLS" gap (the other half is the Helm chart's Ingress).
"""
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_self_signed_cert(tmp_path):
    """A tiny self-signed cert/key pair, generated with the stdlib-adjacent
    `openssl` CLI so this test needs no extra Python crypto dependency."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "1",
         "-subj", "/CN=localhost"],
        check=True, capture_output=True,
    )
    return str(cert), str(key)


def test_server_serves_https_when_tls_configured(tmp_path):
    if subprocess.run(["which", "openssl"], capture_output=True).returncode != 0:
        pytest.skip("openssl CLI not available to generate a test certificate")

    cert, key = _make_self_signed_cert(tmp_path)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    port = _free_port()
    env = dict(os.environ)
    env["AGENTHIVE_TLS_CERT_FILE"] = cert
    env["AGENTHIVE_TLS_KEY_FILE"] = key

    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port), "--db",
         str(tmp_path / "tls.db"), "--host", "127.0.0.1"],
        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # self-signed test cert

        url = f"https://127.0.0.1:{port}/healthz"
        last_err = None
        for _ in range(50):
            try:
                with urllib.request.urlopen(url, timeout=1, context=ctx) as resp:
                    assert resp.status == 200
                    return
            except Exception as e:
                last_err = e
                time.sleep(0.1)
        raise AssertionError(f"server never came up over HTTPS: {last_err}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
