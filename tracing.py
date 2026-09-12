"""
tracing.py - OpenTelemetry distributed tracing for AgentHive.

The metrics in metrics.py answer "is this working, in aggregate, over
time." This module answers a different question: "for THIS ONE write, or
THIS ONE retrieve, where did the time actually go, and what did the
server actually do?" - the whole point being that a team should be able
to watch one agent session's journey end to end (rate limit check, auth
lookup, cache lookup, graph traversal, the approval gate) instead of
inferring it from a handful of counters.

Design:

- Zero-cost by default. Nothing is exported unless OTEL_EXPORTER_OTLP_ENDPOINT
  or AGENTHIVE_TRACING_CONSOLE is set (see config.py). A fresh install pays
  nothing for this - `TRACER.start_as_current_span(...)` calls sprinkled
  through server.py/client.py are no-ops until tracing is turned on.
- Optional dependency. `opentelemetry-api`/`-sdk`/`-exporter-otlp-proto-http`
  are lazily imported, same pattern as psycopg2/redis - a plain
  `python3 server.py` with none of them installed still runs; TRACER
  becomes a tiny local no-op shim instead of the real thing.
- Two exporters, combinable: AGENTHIVE_TRACING_CONSOLE=true prints each
  finished span as one line of JSON to stdout (no infrastructure - good
  for "does this actually work" and for tests, see tests/test_tracing.py).
  OTEL_EXPORTER_OTLP_ENDPOINT sends spans to any OTLP/HTTP-compatible
  backend - Jaeger's all-in-one image (docker-compose's `jaeger` profile)
  is the one this project documents and tests against, but Tempo,
  Honeycomb, etc. all speak the same protocol.
- Context propagation across the client/server boundary via the standard
  W3C traceparent header (inject_headers / extract_context below), so a
  session's retrieve_context + log_session calls - and the server-side
  work each one triggers - show up as one connected trace, not two
  disconnected ones. See client.py's TeamMemoryClient.traced_session().

No span here ever carries a raw memory node body or a token value as an
attribute name that could leak into a backend's UI unexpectedly - only
ids, counts, and booleans. Team/user ids DO appear as span attributes
(unlike metrics.py's Prometheus series) because traces are inherently
per-request and don't accumulate into an unbounded label set the way a
Prometheus time series would.
"""
import os

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry import propagate as _otel_propagate

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by CI's no-extras job
    _OTEL_AVAILABLE = False


# ---------------------------------------------------------------------------
# No-op fallback - used only when the `opentelemetry` package isn't
# installed at all. When it IS installed but simply not configured (no
# exporter enabled), we rely on OpenTelemetry's own built-in no-op
# tracer/propagator instead of reinventing it - see init_tracing().
# ---------------------------------------------------------------------------

class _NoopSpan:
    def set_attribute(self, *a, **k):
        pass

    def update_name(self, *a, **k):
        pass

    def record_exception(self, *a, **k):
        pass

    def set_status(self, *a, **k):
        pass


class _NoopSpanCtx:
    def __enter__(self):
        return _NoopSpan()

    def __exit__(self, *exc_info):
        return False


class _NoopTracer:
    def start_as_current_span(self, name, **kwargs):
        return _NoopSpanCtx()


if _OTEL_AVAILABLE:
    # trace.get_tracer() returns a proxy tied to whatever TracerProvider is
    # globally configured AT CALL TIME, not at get_tracer() time - so this
    # reference, taken once here, silently starts producing real spans the
    # moment init_tracing() later calls set_tracer_provider(). Until then
    # (or if tracing is never enabled) it behaves as OpenTelemetry's own
    # no-op tracer. This is standard OTel Python behavior, not something
    # this module implements itself.
    TRACER = _otel_trace.get_tracer("agenthive")
else:
    TRACER = _NoopTracer()

_MODE = "disabled"
_initialized = False


def inject_headers(headers: dict) -> dict:
    """Injects the current span's W3C traceparent (+ tracestate) into an
    outgoing request's headers, mutating and returning `headers`. A no-op
    if opentelemetry isn't installed or there's no active span."""
    if _OTEL_AVAILABLE:
        try:
            _otel_propagate.inject(headers)
        except Exception:
            pass  # tracing must never break a real request
    return headers


def extract_context(headers: dict):
    """Extracts a parent context from an incoming request's headers, or
    None if absent/unavailable. Header keys are lowercased first - the
    W3C propagator looks for the literal key "traceparent", but stdlib
    http.server (and Python's own urllib client) capitalize header names
    on the wire ("Traceparent"), so a case-sensitive dict lookup would
    silently fail to link client and server spans."""
    if not _OTEL_AVAILABLE:
        return None
    try:
        lowered = {k.lower(): v for k, v in headers.items()}
        return _otel_propagate.extract(lowered)
    except Exception:
        return None


def start_server_span(name: str, headers: dict):
    """Starts (and returns a context manager for) the root span for one
    incoming HTTP request, continuing the caller's trace if it sent a
    traceparent header. Usage: `with tracing.start_server_span(...) as span:`."""
    ctx = extract_context(headers)
    kwargs = {}
    if ctx is not None:
        kwargs["context"] = ctx
    if _OTEL_AVAILABLE:
        kwargs["kind"] = _otel_trace.SpanKind.SERVER
    return TRACER.start_as_current_span(name, **kwargs)


def start_client_span(name: str):
    """Starts the root span for one outgoing client call (client.py).
    Whatever headers dict the caller then passes through inject_headers()
    while this span is current will carry it to the server."""
    kwargs = {}
    if _OTEL_AVAILABLE:
        kwargs["kind"] = _otel_trace.SpanKind.CLIENT
    return TRACER.start_as_current_span(name, **kwargs)


def init_tracing() -> str:
    """Configures the global TracerProvider from CONFIG, if tracing is
    enabled at all. Called once from server.py's main() before the server
    starts serving, and once from client.py at import time (so a plain
    script that just does `import client` gets its own spans exported too,
    with the same env vars - see client.py's docstring). Safe to call more
    than once; only the first call actually configures anything. Returns a
    short string describing what was configured (for the startup log line)
    - "disabled", "console", "otlp:<endpoint>", or "console+otlp:<endpoint>"."""
    global _MODE, _initialized
    if _initialized:
        return _MODE
    _initialized = True

    from config import CONFIG  # imported here, not at module load, to avoid
    # a circular import (config.py has no reason to import tracing.py).

    if not _OTEL_AVAILABLE:
        _MODE = "disabled (opentelemetry not installed - pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http)"
        return _MODE

    otlp_endpoint = CONFIG.otel_exporter_otlp_endpoint
    console = CONFIG.tracing_console

    if not otlp_endpoint and not console:
        _MODE = "disabled"
        return _MODE

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )

    provider = TracerProvider(
        resource=Resource.create({"service.name": CONFIG.otel_service_name})
    )
    modes = []

    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        endpoint = otlp_endpoint.rstrip("/")
        if not endpoint.endswith("/v1/traces"):
            endpoint += "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        modes.append(f"otlp:{otlp_endpoint}")

    if console:
        # indent=None -> one compact JSON object per line, not pretty-
        # printed across several - makes each finished span greppable /
        # parseable, which tests/test_tracing.py relies on. Simple (not
        # batch) processor so a span is printed the instant it ends,
        # matching this exporter's "see it right now" purpose.
        exporter = ConsoleSpanExporter(
            formatter=lambda span: span.to_json(indent=None) + "\n"
        )
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        modes.append("console")

    _otel_trace.set_tracer_provider(provider)
    _MODE = "+".join(modes)
    return _MODE


def current_mode() -> str:
    return _MODE


def shutdown_tracing():
    """Flushes and shuts down the tracer provider, if tracing was ever
    enabled. The OTLP exporter batches spans (BatchSpanProcessor's default
    export interval is ~5s) - without this, spans from the last few
    seconds before a graceful shutdown (SIGTERM, e.g. a Kubernetes pod
    being rolled) would simply never be sent. Safe to call even if
    tracing was never enabled, or opentelemetry isn't installed."""
    if not _OTEL_AVAILABLE or not _initialized:
        return
    try:
        provider = _otel_trace.get_tracer_provider()
        shutdown = getattr(provider, "shutdown", None)
        if shutdown is not None:
            shutdown()
    except Exception:
        pass  # shutdown should never prevent the process from exiting
