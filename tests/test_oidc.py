"""
test_oidc.py - end-to-end test of the Azure AD login flow, against a real
running server.py (same "test against a real running server" philosophy as
the rest of this suite - see conftest.py) and a real, locally-generated RSA
keypair. Nothing here is mocked at the HTTP or crypto layer: a small local
HTTP server (FakeAzureAD below) stands in for the three Microsoft
endpoints this flow actually touches (JWKS, token exchange - authorize is
never hit here, since that's Microsoft's own hosted login page, out of
scope for this service to test), and id_tokens are real, signed JWTs
verified through the exact same code path (oidc.py) that talks to the real
Azure AD in production - only the network address changes
(AGENTHIVE_AZURE_AUTHORITY_OVERRIDE, read by oidc.py's _authority(), see
its docstring for why that's an env var rather than a plain Python
attribute: server.py runs as a subprocess here, same as every other test
in this suite).
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from conftest import _free_port, _wait_for  # noqa: E402


class FakeAzureAD:
    """A stand-in for the three Azure AD v2.0 endpoints oidc.py talks to.
    Runs in this test process (not the server.py subprocess) on its own
    real TCP port, so the subprocess reaches it exactly like it would
    reach the real login.microsoftonline.com - just at a different host.

    add_code(code, claims) registers what the fake token endpoint should
    hand back (as a freshly signed id_token) the next time that code is
    exchanged - the same "arrange, then drive the real flow" shape as
    every other fixture in this suite."""

    def __init__(self):
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self.private_key.public_key()
        self.kid = "test-key-1"
        jwk = RSAAlgorithm.to_jwk(self.public_key, as_dict=True)
        jwk["kid"] = self.kid
        jwk["use"] = "sig"
        jwk["alg"] = "RS256"
        self._jwks_body = json.dumps({"keys": [jwk]}).encode("utf-8")
        self._codes: dict[str, dict] = {}

        outer = self
        port = _free_port()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                if self.path == "/discovery/v2.0/keys":
                    self._json(200, None, raw=outer._jwks_body)
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                if self.path != "/oauth2/v2.0/token":
                    self._json(404, {"error": "not found"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
                code = (form.get("code") or [None])[0]
                claim_overrides = outer._codes.get(code)
                if claim_overrides is None:
                    self._json(400, {"error": "invalid_grant", "error_description": "unknown code"})
                    return
                id_token = outer._sign(claim_overrides)
                self._json(200, {"id_token": id_token, "access_token": "unused", "token_type": "Bearer"})

            def _json(self, status, payload, raw=None):
                body = raw if raw is not None else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def _sign(self, claim_overrides: dict) -> str:
        now = int(time.time())
        claims = {
            "iss": self.base_url + "/v2.0",
            "aud": claim_overrides.pop("_aud"),
            "iat": now,
            "exp": now + 300,
            "sub": "some-opaque-subject",
        }
        claims.update(claim_overrides)
        return jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": self.kid})

    def add_code(self, code: str, aud: str, **claims):
        """claims should include at least oid; may include name/email/etc."""
        claims["_aud"] = aud
        self._codes[code] = claims

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture()
def fake_azure():
    fake = FakeAzureAD()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture()
def live_server_oidc(tmp_path, fake_azure):
    """Same shape as conftest.py's live_server, plus the AGENTHIVE_AZURE_*
    env vars pointed at fake_azure instead of the real Azure AD."""
    port = _free_port()
    db_path = str(tmp_path / "test.db")
    env = dict(os.environ)
    env["AGENTHIVE_RATE_LIMIT_PER_MINUTE"] = "100000"
    env["AGENTHIVE_LOG_JSON"] = "true"
    env["AGENTHIVE_AZURE_TENANT_ID"] = "test-tenant"
    env["AGENTHIVE_AZURE_CLIENT_ID"] = "test-client-id"
    env["AGENTHIVE_AZURE_CLIENT_SECRET"] = "test-client-secret"
    env["AGENTHIVE_AZURE_REDIRECT_URI"] = f"http://127.0.0.1:{port}/auth/azure/callback"
    env["AGENTHIVE_AZURE_AUTHORITY_OVERRIDE"] = fake_azure.base_url
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port), "--db", db_path,
         "--host", "127.0.0.1"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for(base_url + "/healthz")
        yield base_url, fake_azure
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _get(url, allow_redirects_manually=True):
    """Like urllib.request.urlopen but never follows a 302 - we need the
    Location header, not the page it points to (that page is Microsoft's,
    or in the callback case, our own /ui)."""
    class NoRedirect(urllib.request.HTTPErrorProcessor):
        def http_response(self, request, response):
            return response
        https_response = http_response

    opener = urllib.request.build_opener(NoRedirect)
    return opener.open(url)


def _login_and_get_state(base_url: str) -> tuple[str, str]:
    """Drives GET /auth/azure/login and returns (state, authorize_url)."""
    resp = _get(base_url + "/auth/azure/login")
    assert resp.status == 302
    location = resp.headers["Location"]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    return query["state"][0], location


def test_azure_status_reports_enabled(live_server_oidc):
    base_url, _ = live_server_oidc
    with urllib.request.urlopen(base_url + "/auth/azure/status") as resp:
        data = json.loads(resp.read())
    assert data == {"enabled": True}


def test_azure_status_reports_disabled_when_unconfigured(live_server):
    with urllib.request.urlopen(live_server + "/auth/azure/status") as resp:
        data = json.loads(resp.read())
    assert data == {"enabled": False}
    # And the routes that depend on it being configured refuse to work:
    resp = _get(live_server + "/auth/azure/login")
    assert resp.status == 404


def test_full_login_links_to_existing_user_and_mints_a_working_session(live_server_oidc):
    base_url, fake_azure = live_server_oidc

    # Create a team (bootstraps an admin user with a personal token).
    req = urllib.request.Request(
        base_url + "/teams", method="POST",
        data=json.dumps({"name": "Azure Team", "owner_name": "Ada"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        created = json.loads(resp.read())
    team_id = created["team"]["id"]
    admin_token = created["user"]["token"]
    admin_id = created["user"]["id"]

    # Admin links their own user to a (fake) Azure AD object id - the
    # explicit, no-auto-provisioning step ADR.md describes.
    azure_oid = "11111111-2222-3333-4444-555555555555"
    req = urllib.request.Request(
        base_url + f"/teams/{team_id}/users/{admin_id}/link-azure", method="POST",
        data=json.dumps({"azure_oid": azure_oid}).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": admin_token},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200

    # Drive the real login -> (fake) Azure sign-in -> callback flow.
    state, _ = _login_and_get_state(base_url)
    fake_azure.add_code("fake-auth-code", aud="test-client-id", oid=azure_oid,
                         name="Ada Lovelace", email="ada@example.com")
    resp = _get(base_url + "/auth/azure/callback?code=fake-auth-code&state=" + state)
    assert resp.status == 302
    location = resp.headers["Location"]
    assert location.startswith("/ui#session=")
    fragment = urllib.parse.parse_qs(location.split("#", 1)[1])
    assert fragment["team"][0] == team_id
    assert fragment["name"][0] == "Ada"  # the AgentHive user's name, not the Azure claim
    session_token = fragment["session"][0]

    # The minted session token works exactly like a personal token.
    req = urllib.request.Request(
        base_url + f"/teams/{team_id}/agents", method="GET",
        headers={"X-API-Key": session_token},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200


def test_unlinked_azure_account_gets_a_clear_message_not_auto_provisioned(live_server_oidc):
    base_url, fake_azure = live_server_oidc

    state, _ = _login_and_get_state(base_url)
    unknown_oid = "not-linked-to-anyone"
    fake_azure.add_code("fake-code-2", aud="test-client-id", oid=unknown_oid,
                         name="Grace Hopper", email="grace@example.com")
    resp = _get(base_url + "/auth/azure/callback?code=fake-code-2&state=" + state)
    assert resp.status == 302
    location = resp.headers["Location"]
    assert location.startswith("/ui#azure_link_needed=1")
    fragment = urllib.parse.parse_qs(location.split("#", 1)[1])
    assert fragment["oid"][0] == unknown_oid
    assert fragment["name"][0] == "Grace Hopper"


def test_state_is_single_use_replay_is_rejected(live_server_oidc):
    base_url, fake_azure = live_server_oidc

    state, _ = _login_and_get_state(base_url)
    fake_azure.add_code("fake-code-3", aud="test-client-id", oid="whoever")
    resp = _get(base_url + "/auth/azure/callback?code=fake-code-3&state=" + state)
    assert resp.status == 302 and "azure_link_needed" in resp.headers["Location"]

    # Same state again - must not still work (single-use, see
    # db.py's consume_oidc_state).
    resp = _get(base_url + "/auth/azure/callback?code=fake-code-3&state=" + state)
    assert resp.status == 302
    assert resp.headers["Location"].startswith("/ui#azure_error=")


def test_wrong_audience_id_token_is_rejected(live_server_oidc):
    """Proves the id_token is actually validated (audience, in this case),
    not merely decoded - a forged/misdirected token for a different client
    must not mint a session."""
    base_url, fake_azure = live_server_oidc

    state, _ = _login_and_get_state(base_url)
    fake_azure.add_code("fake-code-4", aud="someone-elses-client-id", oid="whoever")
    resp = _get(base_url + "/auth/azure/callback?code=fake-code-4&state=" + state)
    assert resp.status == 302
    assert resp.headers["Location"].startswith("/ui#azure_error=")


def test_link_azure_rejects_duplicate_oid(live_server_oidc):
    base_url, _ = live_server_oidc

    req = urllib.request.Request(
        base_url + "/teams", method="POST",
        data=json.dumps({"name": "T1"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        t1 = json.loads(resp.read())

    req = urllib.request.Request(
        base_url + "/teams", method="POST",
        data=json.dumps({"name": "T2"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        t2 = json.loads(resp.read())

    shared_oid = "shared-oid"
    req = urllib.request.Request(
        base_url + f"/teams/{t1['team']['id']}/users/{t1['user']['id']}/link-azure",
        method="POST", data=json.dumps({"azure_oid": shared_oid}).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": t1["user"]["token"]},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200

    req = urllib.request.Request(
        base_url + f"/teams/{t2['team']['id']}/users/{t2['user']['id']}/link-azure",
        method="POST", data=json.dumps({"azure_oid": shared_oid}).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": t2["user"]["token"]},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400
