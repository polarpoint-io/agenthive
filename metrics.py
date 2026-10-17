"""
metrics.py - Prometheus metrics, exposed at GET /metrics.

This is the "prove it" module: AgentHive's whole pitch is that a bounded,
approved, shared memory graph (a) sends fewer tokens to the model than
dumping full team history would, and (b) gets reused across team members
instead of every agent re-deriving the same context alone. These metrics
make both claims checkable over time in Grafana instead of asserted in a
README:

- agenthive_tokens_avoided_total is the headline number: the running sum
  of (full-team tokens - what was actually returned) across retrievals
  that returned at least one note. An empty result is a miss: nothing
  was served, so nothing was avoided. Graph the hit counter as a rate()
  and it climbs with usage that actually returned memory.
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
WRITES_BY_RESERVED_TAG = Counter(
    "agenthive_writes_by_reserved_tag_total",
    "Writes carrying each reserved tag, plus 'none'. Shows what the review "
    "queue is actually made of - a queue that is mostly one tag is a queue "
    "with one upstream cause.",
    ["tag"], registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Review queue health
# ---------------------------------------------------------------------------
# The approval gate is the mechanism this whole system rests on, and
# until now nothing measured whether it was keeping up. Pending memory is
# INVISIBLE to retrieval, so a backed-up queue degrades every agent's
# context silently: no error, no failed request, no alert, just steadily
# worse answers. These two are the ones to page on.

REVIEW_LATENCY = Histogram(
    "agenthive_review_latency_seconds",
    "Time from a node being written to a human (or a rule) ruling on it",
    buckets=(60, 300, 900, 3600, 6 * 3600, 24 * 3600, 3 * 24 * 3600,
             7 * 24 * 3600, 30 * 24 * 3600),
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Retrieval quality
# ---------------------------------------------------------------------------

ANCHOR_RESOLUTION = Counter(
    "agenthive_anchor_resolution_total",
    "How each retrieval's anchor matched: exact title, token fallback, or "
    "nothing, or an alias a human recorded. A high fallback share means "
    "titles don't match how people phrase things - a naming problem no "
    "traversal tuning will fix, but one an alias fixes permanently.",
    ["resolution"], registry=REGISTRY,  # exact | alias | fallback | none
)
NEIGHBORHOOD_SIZE = Histogram(
    "agenthive_retrieval_neighborhood_size",
    "Nodes returned per retrieval. The distribution to look at before "
    "changing hops or hub_cutoff: a long tail means traversal is pulling "
    "in more than anyone reads.",
    buckets=(0, 1, 2, 3, 5, 8, 13, 21, 34, 55),
    registry=REGISTRY,
)

AUTH_FAILURES = Counter(
    "agenthive_auth_failures_total",
    "Rejected requests by reason. The HTTP response stays deliberately "
    "opaque so a stolen-token probe learns nothing; this is the metrics "
    "port, where telling a TTL rollout from a revocation actually matters.",
    ["reason"], registry=REGISTRY,  # missing_header | unknown | revoked | expired
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
    "Approx tokens NOT sent when a retrieval returned at least one note: "
    "full-team tokens minus the bounded neighborhood. Empty retrievals "
    "do not increment this.",
    registry=REGISTRY,
)
REDUCTION_PCT = Histogram(
    "agenthive_retrieval_reduction_pct",
    "Percentage reduction vs. a full-team dump, observed only when the "
    "retrieval returned at least one note",
    buckets=(0, 10, 25, 50, 75, 90, 95, 99, 100), registry=REGISTRY,
)
RETRIEVAL_HITS = Counter(
    "agenthive_retrieval_hits_total",
    "retrieve_context calls that returned at least one note",
    registry=REGISTRY,
)
RETRIEVAL_EMPTY = Counter(
    "agenthive_retrieval_empty_total",
    "retrieve_context calls that matched no approved note",
    registry=REGISTRY,
)

RETRIEVAL_MODE = Counter(
    "agenthive_retrievals_by_mode_total",
    "retrieve_context calls by disclosure mode: full returns bodies, index "
    "withholds them for a follow-up fetch",
    ["mode"], registry=REGISTRY,  # mode: full | index
)
INDEX_TOKENS_WITHHELD = Counter(
    "agenthive_index_tokens_withheld_total",
    "Approx body tokens NOT sent because the caller asked for the index "
    "instead of the full neighborhood. Gross, not net - subtract "
    "agenthive_fetch_tokens_served_total to get the real saving, because a "
    "caller that fetches every id back has saved nothing.",
    registry=REGISTRY,
)
FETCHES = Counter(
    "agenthive_fetches_total", "memory/fetch calls served", registry=REGISTRY,
)
FETCH_TOKENS_SERVED = Counter(
    "agenthive_fetch_tokens_served_total",
    "Approx tokens returned by memory/fetch - the half of the progressive-"
    "disclosure round trip that costs rather than saves",
    registry=REGISTRY,
)
FETCH_NODES = Counter(
    "agenthive_fetch_nodes_total",
    "Nodes returned by memory/fetch, by outcome. missing covers rejected, "
    "un-approved and other-team ids alike - the gate working, not an error.",
    ["outcome"], registry=REGISTRY,  # outcome: served | missing
)

PRIVATE_BLOCKS_STRIPPED = Counter(
    "agenthive_private_blocks_stripped_total",
    "<private> blocks removed from a memory node before it was stored",
    registry=REGISTRY,
)
AUTO_REJECTED = Counter(
    "agenthive_auto_rejected_total",
    "Writes rejected without a human, by cause: duplicate, trivial, or a "
    "rule. A spike is worth reading - auto-rejection is safe because a "
    "lost note can be rewritten, but a rule that is too broad discards "
    "knowledge quietly and nothing else would say so.",
    ["cause"], registry=REGISTRY,  # duplicate | trivial | rule
)
AUTO_APPROVE_BLOCKED = Counter(
    "agenthive_auto_approve_blocked_total",
    "Writes that matched an auto-approve rule but were held for human "
    "review anyway because of a reserved tag (security_alert/sensitive)",
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# The learning loop
# ---------------------------------------------------------------------------
# A self-improvement layer that cannot be shown to improve anything is
# the same mistake as a retrieval layer that asserts a token saving
# without measuring it. These are the counters that answer "is it
# working", and they are built so they can also answer "no".
#
# agenthive_gaps_closed_total is the headline. A gap closes when an
# anchor that previously returned nothing returns something - i.e. the
# graph learned the thing people kept asking for. Rising = the loop
# works. Flat while agenthive_open_gaps climbs = proposals are being
# raised and ignored, which is worth knowing and would otherwise be
# invisible.

GAPS_CLOSED = Counter(
    "agenthive_gaps_closed_total",
    "Anchors that previously returned nothing and now return something - "
    "the memory-processing loop actually closing a known gap",
    registry=REGISTRY,
)
GAP_MISSES_RECOVERED = Counter(
    "agenthive_gap_misses_recovered_total",
    "Accumulated miss count of gaps at the moment they closed. Divided by "
    "gaps_closed_total it gives the average number of failed lookups a gap "
    "cost before somebody filled it - the latency of the loop, in pain.",
    registry=REGISTRY,
)
TIER_CHANGES = Counter(
    "agenthive_tier_changes_applied_total",
    "Tier proposals an admin actually applied, by direction. Promotions "
    "without demotions means the thresholds only ratchet; a high rate of "
    "both on the same nodes means they are set too tight and the graph is "
    "oscillating.",
    ["direction"], registry=REGISTRY,  # direction: promote | demote
)
PROPOSALS = Counter(
    "agenthive_proposals_raised_total",
    "Proposals computed by the memory-processing layer, by kind. Counted "
    "when an endpoint computes them, so this measures what the layer "
    "FOUND - compare against tier_changes_applied to see what was acted on.",
    ["kind"], registry=REGISTRY,  # kind: link | tier | rule | noisy | stale | gap
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
        by_tier = GaugeMetricFamily(
            "agenthive_memory_nodes_by_tier",
            "Approved memory nodes by tier, across all teams",
            labels=["tier"],
        )
        open_gaps = GaugeMetricFamily(
            "agenthive_open_gaps",
            "Distinct anchors currently returning nothing, across all teams",
        )
        oldest_pending = GaugeMetricFamily(
            "agenthive_oldest_pending_age_seconds",
            "Age of the longest-waiting unreviewed node. Pending memory is "
            "invisible to retrieval, so this climbing is context quietly "
            "degrading for every agent on the team.",
        )
        aliases = GaugeMetricFamily(
            "agenthive_anchor_aliases",
            "Phrasings a human has mapped to a node, across all teams. Each "
            "one converts a repeat miss into a permanent exact hit.",
        )
        db_up = GaugeMetricFamily(
            "agenthive_db_up", "1 when the storage backend answers, 0 when it does not"
        )

        if _STORE is not None:
            try:
                counts = _STORE.global_node_status_counts()
                for status, count in counts.items():
                    nodes_by_status.add_metric([status], count)
                active_users.add_metric([], _STORE.global_active_user_count())
                teams.add_metric([], _STORE.global_team_count())
                for tier, count in _STORE.global_tier_counts().items():
                    by_tier.add_metric([tier], count)
                open_gaps.add_metric([], _STORE.global_open_gap_count())
                oldest_pending.add_metric([], _STORE.global_oldest_pending_age_seconds())
                aliases.add_metric([], _STORE.global_alias_count())
                db_up.add_metric([], 1 if _STORE.healthcheck() else 0)
            except Exception:
                # A metrics scrape must never 500 the process. db_up is
                # emitted here as well: if the queries above threw, the
                # backend is exactly what is wrong, and reporting nothing
                # would turn an outage into silence.
                db_up.add_metric([], 0)

        yield nodes_by_status
        yield active_users
        yield teams
        yield by_tier
        yield open_gaps
        yield oldest_pending
        yield aliases
        yield db_up


REGISTRY.register(DbStatsCollector())


def record_write(auto_approved: bool, tags=None):
    MEMORY_WRITES.labels(status="auto_approved" if auto_approved else "pending").inc()
    # Reserved vocabulary only - free-form tags are caller-controlled and
    # would make this label unbounded, which is how a metrics endpoint
    # becomes a memory leak.
    from governance import RESERVED_TAGS

    present = RESERVED_TAGS & {str(t).strip().lower() for t in (tags or [])}
    for tag in present or {"none"}:
        WRITES_BY_RESERVED_TAG.labels(tag=tag).inc()


def record_review(approved: bool, latency_seconds: float = None):
    MEMORY_REVIEWS.labels(decision="approved" if approved else "rejected").inc()
    if latency_seconds is not None and latency_seconds >= 0:
        REVIEW_LATENCY.observe(latency_seconds)


def record_auth_failure(reason: str):
    if reason in ("missing_header", "unknown", "revoked", "expired"):
        AUTH_FAILURES.labels(reason=reason).inc()


def record_retrieval(result: dict, cache_status: str, cross_user: bool, mode: str = "full"):
    """cache_status: 'hit' | 'miss' | 'disabled'. mode: 'full' | 'index'.

    Token avoidance is counted only when a note was returned. An empty
    neighborhood did not spare the caller a full-graph dump; it spared
    them nothing, because there was nothing to fold into context.

    `mode` deliberately does NOT change tokens_avoided. In index mode the
    response really is tiny, so computing avoided from what was sent would
    make the headline number jump the moment a caller switches mode - and
    then jump again, in the same direction, when it fetches the bodies
    back. Avoidance stays defined against the full neighborhood either
    way, and the index/fetch pair is tracked on its own counters so the
    net can be worked out rather than assumed."""
    RETRIEVALS.inc()
    RETRIEVAL_MODE.labels(mode=mode).inc()
    resolution = result.get("resolution")
    if resolution in ("exact", "alias", "fallback", "none"):
        ANCHOR_RESOLUTION.labels(resolution=resolution).inc()
    NEIGHBORHOOD_SIZE.observe(result.get("neighborhood_count") or 0)
    served = result.get("approx_tokens", 0) or 0
    TOKENS_SERVED.inc(served)

    # In index mode `served` is titles only, so the full-neighborhood cost
    # has to come from the field project_index() preserved for exactly this.
    full_neighborhood = (
        result.get("approx_tokens_full_neighborhood") if mode == "index" else served
    ) or 0

    if (result.get("neighborhood_count") or 0) > 0:
        RETRIEVAL_HITS.inc()
        avoided = max((result.get("approx_tokens_full_team", 0) or 0) - full_neighborhood, 0)
        TOKENS_AVOIDED.inc(avoided)
        REDUCTION_PCT.observe(result.get("reduction_pct", 0) or 0)
        if mode == "index":
            INDEX_TOKENS_WITHHELD.inc(max(full_neighborhood - served, 0))
    else:
        RETRIEVAL_EMPTY.inc()
    if cache_status == "hit":
        CACHE_HITS.inc()
        if cross_user:
            CROSS_USER_CACHE_HITS.inc()
    elif cache_status == "miss":
        CACHE_MISSES.inc()


def render() -> bytes:
    return generate_latest(REGISTRY)


CONTENT_TYPE = CONTENT_TYPE_LATEST


def record_fetch(result: dict):
    """The paying half of progressive disclosure. Counted separately from
    retrievals so the two are never conflated in a dashboard."""
    FETCHES.inc()
    served = result.get("approx_tokens", 0) or 0
    FETCH_TOKENS_SERVED.inc(served)
    TOKENS_SERVED.inc(served)
    FETCH_NODES.labels(outcome="served").inc(result.get("node_count", 0) or 0)
    FETCH_NODES.labels(outcome="missing").inc(len(result.get("missing") or []))


def record_private_stripped(blocks: int):
    if blocks > 0:
        PRIVATE_BLOCKS_STRIPPED.inc(blocks)


def record_auto_approve_blocked():
    AUTO_APPROVE_BLOCKED.inc()


def record_gap_closed(misses_recovered: int):
    """One anchor that used to return nothing now returns something."""
    GAPS_CLOSED.inc()
    GAP_MISSES_RECOVERED.inc(max(misses_recovered, 0))


def record_tier_changes(applied):
    """applied: an iterable of {"direction": "promote"|"demote"}."""
    for change in applied:
        direction = change.get("direction")
        if direction in ("promote", "demote"):
            TIER_CHANGES.labels(direction=direction).inc()


def record_proposals(kind: str, count: int):
    if count > 0:
        PROPOSALS.labels(kind=kind).inc(count)


def record_auto_reject(reason: str):
    """Bucket the free-text reason into a bounded label. The reason
    string contains node and rule ids, which must never become one."""
    text = (reason or "").lower()
    if "identical to approved" in text:
        cause = "duplicate"
    elif "characters" in text:
        cause = "trivial"
    else:
        cause = "rule"
    AUTO_REJECTED.labels(cause=cause).inc()
