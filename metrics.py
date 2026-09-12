"""
metrics.py - Prometheus metrics, exposed at GET /metrics.

This is the "prove it" module: AgentHive's whole pitch is that a bounded,
approved, shared memory graph (a) sends fewer tokens to the model than
dumping full team history would, and (b) gets reused across team members
instead of every agent re-deriving the same context alone. These metrics
make both claims checkable over time in Grafana instead of asserted in a
README:

- agenthive_tokens_avoided_total is the headline number: the running sum
  of (full-team tokens - what was actually returned) across every
  retrieval. Graph it as a rate() and it climbs with usage.
- agenthive_cache_hits_total / agenthive_cross_user_cache_hits_total show
  the graph being used as SHARED memory: a hit is one fewer traversal;
  a cross-user hit is proof a different teammate benefited from context
  someone else's session already pulled.
- agenthive_memory_nodes / agenthive_active_users (gauges, computed at
  scrape time from the DB) show the graph growing and getting used -
  "how this improves over time" is this dashboard's slope, not a single
  number this service could compute itself.

No label carries a team_id or user_id - unbounded label cardinality is
how you turn a metrics endpoint into a memory leak. Anything broken down
by team lives in the per-team JSON summary endpoint instead
(GET /teams/{id}/metrics/summary) and the review UI's Metrics tab.
"""
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily

REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter(
    "agenthive_http_requests_total", "Total HTTP requests",
    ["method", "route", "status"], registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "agenthive_http_request_duration_seconds", "HTTP request duration",
    ["method", "route"], registry=REGISTRY,
)

MEMORY_WRITES = Counter(
    "agenthive_memory_writes_total", "Memory nodes written, by initial status",
    ["status"], registry=REGISTRY,  # status: pending | auto_approved
)
MEMORY_REVIEWS = Counter(
    "agenthive_memory_reviews_total", "Manual review decisions",
    ["decision"], registry=REGISTRY,  # decision: approved | rejected
)

RETRIEVALS = Counter(
    "agenthive_retrievals_total", "retrieve_context calls served", registry=REGISTRY,
)
TOKENS_SERVED = Counter(
    "agenthive_tokens_served_total",
    "Approx tokens returned to callers across all retrievals", registry=REGISTRY,
)
TOKENS_AVOIDED = Counter(
    "agenthive_tokens_avoided_total",
    "Approx tokens NOT sent - the gap between a full-team dump and the "
    "bounded neighborhood actually returned, summed across all retrievals",
    registry=REGISTRY,
)
REDUCTION_PCT = Histogram(
    "agenthive_retrieval_reduction_pct",
    "Per-retrieval percentage reduction vs. a full-team dump",
    buckets=(0, 10, 25, 50, 75, 90, 95, 99, 100), registry=REGISTRY,
)

CACHE_HITS = Counter("agenthive_cache_hits_total", "Retrieval cache hits", registry=REGISTRY)
CACHE_MISSES = Counter("agenthive_cache_misses_total", "Retrieval cache misses", registry=REGISTRY)
CROSS_USER_CACHE_HITS = Counter(
    "agenthive_cross_user_cache_hits_total",
    "Cache hits served to a different user than the one who populated the "
    "entry - a different teammate benefiting from the same retrieved "
    "context, i.e. the graph acting as shared (not per-user) memory",
    registry=REGISTRY,
)

_STORE = None


def bind_store(store):
    """Called once from server.py after the Store is constructed, so the
    DB-backed gauges below have something to query at scrape time."""
    global _STORE
    _STORE = store


class DbStatsCollector:
    """A custom collector queries the DB fresh on every /metrics scrape,
    rather than us trying to keep gauges in sync on every write - simpler
    and always correct, at the cost of one cheap query per scrape."""

    def collect(self):
        nodes_by_status = GaugeMetricFamily(
            "agenthive_memory_nodes",
            "Current memory node count by status, across all teams",
            labels=["status"],
        )
        active_users = GaugeMetricFamily(
            "agenthive_active_users",
            "Non-revoked users, across all teams",
        )
        teams = GaugeMetricFamily("agenthive_teams", "Total teams")

        if _STORE is not None:
            try:
                counts = _STORE.global_node_status_counts()
                for status, count in counts.items():
                    nodes_by_status.add_metric([status], count)
                active_users.add_metric([], _STORE.global_active_user_count())
                teams.add_metric([], _STORE.global_team_count())
            except Exception:
                pass  # a metrics scrape should never 500 the process

        yield nodes_by_status
        yield active_users
        yield teams


REGISTRY.register(DbStatsCollector())


def record_write(auto_approved: bool):
    MEMORY_WRITES.labels(status="auto_approved" if auto_approved else "pending").inc()


def record_review(approved: bool):
    MEMORY_REVIEWS.labels(decision="approved" if approved else "rejected").inc()


def record_retrieval(result: dict, cache_status: str, cross_user: bool):
    """cache_status: 'hit' | 'miss' | 'disabled'."""
    RETRIEVALS.inc()
    TOKENS_SERVED.inc(result.get("approx_tokens", 0))
    avoided = max(result.get("approx_tokens_full_team", 0) - result.get("approx_tokens", 0), 0)
    TOKENS_AVOIDED.inc(avoided)
    REDUCTION_PCT.observe(result.get("reduction_pct", 0) or 0)
    if cache_status == "hit":
        CACHE_HITS.inc()
        if cross_user:
            CROSS_USER_CACHE_HITS.inc()
    elif cache_status == "miss":
        CACHE_MISSES.inc()


def render() -> bytes:
    return generate_latest(REGISTRY)


CONTENT_TYPE = CONTENT_TYPE_LATEST
