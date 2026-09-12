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
    # DEPLOYMENT.md "Known limitations" and ADR.md "v2".
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

    @classmethod
    def from_env(cls) -> "Config":
        return cls()


CONFIG = Config.from_env()
