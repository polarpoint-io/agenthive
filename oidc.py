"""
oidc.py - Azure AD (Microsoft Entra ID) login for human users of the review
UI, via the standard OAuth2 Authorization Code flow with PKCE.

This is deliberately not a general-purpose OIDC library: it knows about
exactly one identity provider (Azure AD v2.0 endpoints) and exists to
support exactly one flow (server-side code exchange, PKCE verifier kept
server-side - see config.py's docstring for why this is a confidential
client rather than a browser-side SPA flow).

PyJWT (with its [crypto] extra, for RS256 signature verification) is the
only new dependency this adds, and it is imported lazily - same pattern as
psycopg2 in db.py and redis in auth.py - so a deployment that never sets
AGENTHIVE_AZURE_* env vars never needs it installed at all. See
requirements.txt / Dockerfile.

Nothing in here talks to the Store - server.py wires the claims this module
extracts (oid, email, name) to an AgentHive user record.
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from config import CONFIG

# Fixed per Microsoft's documented v2.0 endpoints - not configurable,
# because "tenant_id" is the only per-deployment variable in any of these.
_AUTHORITY = "https://login.microsoftonline.com/{tenant}"
_AUTHORIZE_PATH = "/oauth2/v2.0/authorize"
_TOKEN_PATH = "/oauth2/v2.0/token"
_JWKS_PATH = "/discovery/v2.0/keys"

# Overridable only by tests, which stand in a local HTTP server for Azure's
# real endpoints instead of reaching the network - see tests/test_oidc.py.
# Read from an env var (not just a module attribute) because server.py
# normally runs as a subprocess in tests (see conftest.py's live_server
# fixture) - an in-process attribute set by the test wouldn't be visible
# there, but an inherited environment variable is. Not a real deployment
# option - deliberately undocumented outside this module and the tests.
_authority_override: str | None = None


def _authority() -> str:
    env_override = os.environ.get("AGENTHIVE_AZURE_AUTHORITY_OVERRIDE")
    if env_override:
        return env_override
    if _authority_override:
        return _authority_override
    return _AUTHORITY.format(tenant=CONFIG.azure_ad_tenant_id)


class OidcError(Exception):
    """Anything that should surface to the human as 'sign-in failed' rather
    than a 500 - a bad/expired state, a token exchange rejected by Azure, a
    signature or claims check that didn't pass."""


def _pyjwt():
    try:
        import jwt  # PyJWT
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "Azure AD login is configured (AGENTHIVE_AZURE_* env vars are "
            "set) but PyJWT is not installed. `pip install PyJWT[crypto]` "
            "(see requirements.txt)."
        ) from e
    return jwt


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def new_pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge) per RFC 7636 (S256 method).
    The verifier is kept server-side (see db.py's oidc_states table) and
    only ever sent to Azure once, in the token-exchange POST - never
    exposed to the browser."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def new_state() -> str:
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# Authorization URL
# ---------------------------------------------------------------------------

def build_authorize_url(state: str, code_challenge: str) -> str:
    params = {
        "client_id": CONFIG.azure_ad_client_id,
        "response_type": "code",
        "redirect_uri": CONFIG.azure_ad_redirect_uri,
        "response_mode": "query",
        "scope": "openid profile email",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return _authority() + _AUTHORIZE_PATH + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# JWKS (fetched once, cached, refreshed only on a kid miss or after the TTL)
# ---------------------------------------------------------------------------

class _JwksCache:
    # Azure rotates signing keys infrequently and publishes the new one
    # ahead of using it, so a long TTL is fine and keeps this to at most
    # one network call per key rotation instead of one per login.
    TTL_SECONDS = 86400

    def __init__(self):
        self._lock = threading.Lock()
        self._keys: dict[str, dict] = {}
        self._fetched_at: float = 0.0

    def _fetch(self):
        url = _authority() + _JWKS_PATH
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise OidcError(f"could not fetch Azure AD signing keys: {e}") from e
        self._keys = {k["kid"]: k for k in data.get("keys", [])}
        self._fetched_at = time.time()

    def get(self, kid: str) -> dict:
        with self._lock:
            stale = (time.time() - self._fetched_at) > self.TTL_SECONDS
            if stale or kid not in self._keys:
                self._fetch()
            if kid not in self._keys:
                raise OidcError(f"unknown signing key id {kid!r} - Azure AD may have rotated keys")
            return self._keys[kid]

    def reset(self):
        """Test-only: forces the next get() to re-fetch."""
        with self._lock:
            self._keys = {}
            self._fetched_at = 0.0


JWKS = _JwksCache()


def _public_key_for(kid: str):
    jwt = _pyjwt()
    from jwt.algorithms import RSAAlgorithm

    jwk = JWKS.get(kid)
    return RSAAlgorithm.from_jwk(json.dumps(jwk))


# ---------------------------------------------------------------------------
# Code exchange + id_token validation
# ---------------------------------------------------------------------------

def exchange_code_for_claims(code: str, code_verifier: str) -> dict:
    """POSTs the authorization code to Azure's token endpoint, verifies the
    returned id_token's signature and standard claims, and returns the
    claims dict (at minimum: oid, iss, aud, exp - plus whatever of
    email/preferred_username/name Azure AD included). Raises OidcError for
    anything that means "sign-in failed" - never returns a partially
    validated result."""
    jwt = _pyjwt()

    token_url = _authority() + _TOKEN_PATH
    form = urllib.parse.urlencode({
        "client_id": CONFIG.azure_ad_client_id,
        "client_secret": CONFIG.azure_ad_client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CONFIG.azure_ad_redirect_uri,
        "code_verifier": code_verifier,
    }).encode("ascii")
    req = urllib.request.Request(
        token_url, data=form, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise OidcError(f"Azure AD rejected the code exchange: {e.code} {detail}") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise OidcError(f"could not reach Azure AD token endpoint: {e}") from e

    id_token = payload.get("id_token")
    if not id_token:
        raise OidcError("Azure AD's token response had no id_token")

    try:
        header = jwt.get_unverified_header(id_token)
    except Exception as e:
        raise OidcError(f"malformed id_token: {e}") from e
    kid = header.get("kid")
    if not kid:
        raise OidcError("id_token header has no kid")

    try:
        key = _public_key_for(kid)
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=CONFIG.azure_ad_client_id,
            issuer=_expected_issuers(),
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except Exception as e:
        raise OidcError(f"id_token failed validation: {e}") from e

    if not claims.get("oid"):
        raise OidcError("id_token has no 'oid' claim (stable per-user Azure AD object id)")

    return claims


def _expected_issuers():
    """Azure AD's v2.0 issuer is tenant-specific
    (https://login.microsoftonline.com/{tenant}/v2.0); PyJWT's `issuer`
    check accepts either a single string or a list, so this stays a single
    value for the real code path but a test can point _authority_override
    at a plain http:// stand-in issuer instead."""
    return _authority() + "/v2.0"
