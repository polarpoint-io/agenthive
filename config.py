"""
config.py - environment-driven configuration for AgentHive.

Everything here has a sane development default so `python3 server.py` still
works with zero setup, but every value is overridable via an environment
variable for real deployments (Docker, systemd, etc.) - see DEPLOYMENT.md.
"""
import os


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    try:
        return float(val)
    except ValueError:
        return default


class Config:
    # --- storage ---
    # If DATABASE_URL is set and starts with postgres:// or postgresql://,
    # the Postgres backend is used. Otherwise AGENTHIVE_DB_PATH (a SQLite file)
    # is used. See ADR.md: the schema is identical across both backends.
    database_url: str = os.environ.get("DATABASE_URL", "")
    db_path: str = os.environ.get("AGENTHIVE_DB_PATH", "agenthive.db")

    # --- server ---
    host: str = os.environ.get("AGENTHIVE_HOST", "127.0.0.1")
    port: int = _int("AGENTHIVE_PORT", 8790)

    # --- auth ---
    # Minimum length enforced on generated tokens; not user-configurable,
    # listed here for visibility.
    token_prefix: str = "ah-"

    # 0 (default) means tokens never expire on their own - the original
    # behavior, and still correct for a small team that rotates tokens by
    # hand. Set to expire new/rotated tokens automatically after this many
    # seconds; an expired token fails auth exactly like a revoked one (see
    # db.py's user_by_token), so an admin has to rotate it to keep going.
    # Only applies to tokens created/rotated after this is set - existing
    # tokens are unaffected until they're next rotated. See DEPLOYMENT.md
    # "Known limitations".
    token_ttl_seconds: int = _int("AGENTHIVE_TOKEN_TTL_SECONDS", 0)

    # --- rate limiting ---
    # A sliding-window limiter, per API key (falls back to per-IP for the
    # one unauthenticated route, POST /teams). In-memory, per-process: fine
    # for a single-node deployment. A multi-node deployment behind a load
    # balancer needs a shared store (e.g. Redis) - not implemented here,
    # flagged in DEPLOYMENT.md.
    rate_limit_enabled: bool = _bool("AGENTHIVE_RATE_LIMIT_ENABLED", True)
    rate_limit_per_minute: int = _int("AGENTHIVE_RATE_LIMIT_PER_MINUTE", 120)
    rate_limit_window_seconds: int = _int("AGENTHIVE_RATE_LIMIT_WINDOW_SECONDS", 60)

    # --- logging ---
    log_level: str = os.environ.get("AGENTHIVE_LOG_LEVEL", "INFO")
    # Structured (JSON lines) vs plain text. JSON is the right default for
    # anything running behind a log aggregator; plain text is easier to
    # read while developing locally.
    log_json: bool = _bool("AGENTHIVE_LOG_JSON", True)

    # --- shared state across replicas ---
    # Set REDIS_URL (e.g. redis://redis:6379/0) to share the rate limiter
    # and the retrieval cache across multiple replicas. Unset means both
    # fall back to in-memory, per-process state - correct for a single
    # replica, a soft (not hard) guard once you run more than one. See
    # DEPLOYMENT.md "Known limitations" and ADR.md's "Reliability and
    # observability" section.
    redis_url: str = os.environ.get("REDIS_URL", "")

    # --- retrieval cache ---
    # Caches retrieve()'s result per (team, anchor, hops, hub_cutoff) so
    # repeated requests for the same context - common when several agents
    # pick up the same incident/task - are served instantly instead of
    # re-walking the graph, and so a cache hit is direct evidence of the
    # "shared memory across the team" property (see metrics.py). Any
    # write/review for a team invalidates that team's cached entries.
    cache_enabled: bool = _bool("AGENTHIVE_CACHE_ENABLED", True)
    cache_ttl_seconds: int = _int("AGENTHIVE_CACHE_TTL_SECONDS", 300)

    # --- TLS (optional, for deployments with no TLS-terminating proxy) ---
    # If both are set, server.py serves HTTPS directly. In Kubernetes the
    # Helm chart's Ingress normally terminates TLS instead - see
    # helm/agenthive/README.md - but native support closes the gap for
    # bare-metal / systemd deployments with no reverse proxy in front.
    tls_cert_file: str = os.environ.get("AGENTHIVE_TLS_CERT_FILE", "")
    tls_key_file: str = os.environ.get("AGENTHIVE_TLS_KEY_FILE", "")

    # --- metrics ---
    metrics_enabled: bool = _bool("AGENTHIVE_METRICS_ENABLED", True)

    # --- distributed tracing (OpenTelemetry) ---
    # Off unless one of these is set - a fresh install pays nothing for
    # this. OTEL_EXPORTER_OTLP_ENDPOINT (the standard OTel env var, no
    # AGENTHIVE_ prefix - same convention as DATABASE_URL/REDIS_URL) sends
    # spans to any OTLP/HTTP backend: Jaeger, Grafana Tempo, Honeycomb,
    # your APM of choice. AGENTHIVE_TRACING_CONSOLE prints each finished
    # span as one line of JSON to stdout - zero infrastructure, useful for
    # seeing the write -> review -> retrieve journey mapped out without
    # standing anything up first (see ONBOARDING.md). Both can be set at
    # once. See tracing.py for what's actually instrumented and why.
    otel_exporter_otlp_endpoint: str = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    otel_service_name: str = os.environ.get("OTEL_SERVICE_NAME", "agenthive")
    tracing_console: bool = _bool("AGENTHIVE_TRACING_CONSOLE", False)

    # Head-based sampling ratio in [0.0, 1.0]. 1.0 (default) traces every
    # request - fine at the traffic this service sees today (see ADR.md's
    # "Reliability and observability" section), but a high-throughput
    # deployment should turn this down rather than pay for and store a
    # trace per request. The decision
    # is made once per trace, at the root span, via OpenTelemetry's
    # standard ParentBased(TraceIdRatioBased(...)) sampler - see
    # tracing.py - so a sampled-in trace stays fully sampled end to end
    # instead of dropping spans partway through.
    trace_sample_ratio: float = _float("AGENTHIVE_TRACE_SAMPLE_RATIO", 1.0)

    # --- Azure AD (Microsoft Entra ID) login, humans only ---
    # Agents keep using their existing opaque per-user tokens unchanged -
    # this adds a second, optional login path for the humans who use the
    # review UI (see ui/index.html), via the standard OAuth2 Authorization
    # Code flow + PKCE. The token exchange and PKCE code_verifier are kept
    # server-side (see oidc.py) rather than in browser JavaScript, so this
    # is a confidential client, not a SPA/public client.
    #
    # A successful Azure sign-in only ever produces a session for an
    # AgentHive user that already exists and has been explicitly linked to
    # that Azure AD object id by an admin (see server.py's
    # link-azure/unlink-azure routes) - there is no auto-provisioning. See
    # ADR.md's "Authentication" section for the reasoning.
    #
    # All four of tenant_id/client_id/client_secret/redirect_uri must be
    # set for the feature to turn on at all (see oidc_enabled below); any
    # one left empty means /auth/azure/* routes report themselves as
    # disabled rather than half-configuring.
    azure_ad_tenant_id: str = os.environ.get("AGENTHIVE_AZURE_TENANT_ID", "")
    azure_ad_client_id: str = os.environ.get("AGENTHIVE_AZURE_CLIENT_ID", "")
    azure_ad_client_secret: str = os.environ.get("AGENTHIVE_AZURE_CLIENT_SECRET", "")
    # Must exactly match a Redirect URI registered on the Azure AD App
    # Registration, e.g. https://agenthive.yourcompany.com/auth/azure/callback
    azure_ad_redirect_uri: str = os.environ.get("AGENTHIVE_AZURE_REDIRECT_URI", "")

    # How long a session minted by a successful Azure sign-in lasts before
    # the human has to sign in again. Deliberately much shorter than the
    # (optional) agent token TTL above - a human re-authenticating against
    # Azure AD periodically is normal and expected; an agent doing so is not.
    oidc_session_ttl_seconds: int = _int("AGENTHIVE_AZURE_SESSION_TTL_SECONDS", 3600)

    # Where the review UI is hosted, if not served by this process itself
    # (see ui/Dockerfile for running it as its own container). Empty means
    # "this server's own /ui" - the default, all-in-one deployment. Only
    # affects where Azure AD sign-in redirects land (see server.py's
    # _ui_base()) - it doesn't disable this server's own /ui route, so both
    # can be reachable at once if you want that during a migration.
    ui_url: str = os.environ.get("AGENTHIVE_UI_URL", "")

    # Access-Control-Allow-Origin value for browser requests, needed once
    # the UI is served from a different origin than this API (see ui_url
    # above). "*" (default) matches AgentHive's existing bearer-token auth
    # model - X-API-Key isn't a cookie, so it's never sent automatically by
    # a browser to an origin the user didn't ask it to call, unlike cookie
    # auth where a wildcard origin would be a real CSRF risk. Set this to
    # an explicit origin (e.g. https://agenthive-ui.yourcompany.com) if you
    # want to lock it down anyway.
    cors_origin: str = os.environ.get("AGENTHIVE_CORS_ORIGIN", "*")

    @property
    def oidc_enabled(self) -> bool:
        return bool(
            self.azure_ad_tenant_id
            and self.azure_ad_client_id
            and self.azure_ad_client_secret
            and self.azure_ad_redirect_uri
        )

    @classmethod
    def from_env(cls) -> "Config":
        return cls()


CONFIG = Config.from_env()
