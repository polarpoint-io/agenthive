#!/usr/bin/env python3
"""
server.py - AgentHive.

Endpoints
---------
Unauthenticated:
  POST /teams                                          {name, owner_name?}
                                                         -> {team, user (token shown once)}
  GET  /healthz                                         liveness probe
  GET  /readyz                                          readiness probe (checks DB)
  GET  /metrics                                         Prometheus exposition format
  GET  /ui                                              the review UI (static page)

Authenticated with  X-API-Key: <personal user token>  (see auth.py):
  POST /teams/{team_id}/users                [admin]    {name, role} -> token shown once
  GET  /teams/{team_id}/users                [admin]
  POST /teams/{team_id}/users/{id}/rotate    [admin or self]
  POST /teams/{team_id}/users/{id}/revoke    [admin]

  POST /teams/{team_id}/agents               [member]
  GET  /teams/{team_id}/agents               [member]
  POST /teams/{team_id}/tasks                [member]
  GET  /teams/{team_id}/tasks                [member]

  POST /teams/{team_id}/memory               [member]   log_session write path
  GET  /teams/{team_id}/memory/pending       [member]
  POST /teams/{team_id}/memory/{id}/approve  [admin]
  POST /teams/{team_id}/memory/{id}/reject   [admin]
  GET  /teams/{team_id}/memory/retrieve      [member]   retrieve_context read path

  POST   /teams/{team_id}/auto-approve-rules      [admin]
  GET    /teams/{team_id}/auto-approve-rules      [admin]
  DELETE /teams/{team_id}/auto-approve-rules/{id} [admin]

  GET  /teams/{team_id}/metrics/summary      [admin]   node counts, retrieval
                                                        count, top reused nodes -
                                                        see metrics.py / db.py

Every user belongs to exactly one team and has a role of 'admin' or
'member'. 'member' can read/write memory and manage agents/tasks; only
'admin' can review pending memory, manage users, configure auto-approve
rules, or read the metrics summary. See ADR.md for why the approval gate
exists and README.md for the full request/response shapes.

Set AGENTHIVE_TLS_CERT_FILE + AGENTHIVE_TLS_KEY_FILE to serve HTTPS
directly (see config.py); otherwise terminate TLS in front of this
process (a reverse proxy, or the Helm chart's Ingress).

Set OTEL_EXPORTER_OTLP_ENDPOINT and/or AGENTHIVE_TRACING_CONSOLE to map
the write -> review -> retrieve journey as OpenTelemetry traces (see
tracing.py and ONBOARDING.md); off by default, zero cost when unset.
"""
import argparse
import json
import logging
import os
import re
import signal
import ssl
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import metrics
import tracing
from auth import RATE_LIMITER, AuthError, require_role
from cache import CACHE
from config import CONFIG
from db import Store
from retrieval import retrieve

STORE: Store = None  # set in main() / create_app()
UI_HTML: str = None  # loaded lazily from ui/index.html

log = logging.getLogger("agenthive")


class JsonLogFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            payload.update(record.extra_fields)
        return json.dumps(payload)


def configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    if CONFIG.log_json:
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers = [handler]
    log.setLevel(getattr(logging, CONFIG.log_level.upper(), logging.INFO))
    log.propagate = False


def log_request(**fields):
    record = log.makeRecord(log.name, logging.INFO, __file__, 0,
                             "request", (), None)
    record.extra_fields = fields
    log.handle(record)


ROUTES = [
    (re.compile(r"^/teams$"), "POST", "create_team", None),
    (re.compile(r"^/healthz$"), "GET", "healthz", None),
    (re.compile(r"^/readyz$"), "GET", "readyz", None),
    (re.compile(r"^/metrics$"), "GET", "metrics", None),
    (re.compile(r"^/ui/?$"), "GET", "ui", None),

    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/users$"), "POST", "create_user", "admin"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/users$"), "GET", "list_users", "admin"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/users/(?P<user_id>[\w-]+)/rotate$"), "POST", "rotate_user", "self_or_admin"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/users/(?P<user_id>[\w-]+)/revoke$"), "POST", "revoke_user", "admin"),

    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/agents$"), "POST", "create_agent", "member"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/agents$"), "GET", "list_agents", "member"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/tasks$"), "POST", "create_task", "member"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/tasks$"), "GET", "list_tasks", "member"),

    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/memory$"), "POST", "create_memory", "member"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/memory/pending$"), "GET", "list_pending", "member"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/memory/(?P<node_id>[\w-]+)/approve$"), "POST", "approve", "admin"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/memory/(?P<node_id>[\w-]+)/reject$"), "POST", "reject", "admin"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/memory/retrieve$"), "GET", "retrieve_endpoint", "member"),

    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/auto-approve-rules$"), "POST", "create_rule", "admin"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/auto-approve-rules$"), "GET", "list_rules", "admin"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/auto-approve-rules/(?P<rule_id>[\w-]+)$"), "DELETE", "delete_rule", "admin"),

    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/metrics/summary$"), "GET", "team_metrics_summary", "admin"),
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # replaced by structured request logging in _dispatch

    # ---- plumbing ----

    def _send(self, status: int, payload: dict, extra_headers: dict = None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", getattr(self, "_request_id", ""))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length) or b"{}"
        return json.loads(raw)

    def _rate_limit_key(self) -> str:
        api_key = self.headers.get("X-API-Key", "")
        if api_key:
            return "key:" + api_key
        return "ip:" + self.client_address[0]

    def _authenticate(self):
        """Returns the active user dict for X-API-Key, or raises AuthError."""
        api_key = self.headers.get("X-API-Key", "")
        if not api_key:
            raise AuthError(401, "missing X-API-Key header")
        user = STORE.user_by_token(api_key)
        if not user:
            raise AuthError(401, "invalid or revoked API key")
        return user

    def _dispatch(self, method: str):
        start = time.monotonic()
        self._request_id = uuid.uuid4().hex[:12]
        parsed = urlparse(self.path)
        status = 500
        team_id_for_log = None
        route_name = "unmatched"
        exempt_paths = ("/healthz", "/readyz", "/metrics")
        incoming_headers = dict(self.headers.items())

        with tracing.start_server_span(f"{method} {parsed.path}", incoming_headers) as span:
            span.set_attribute("http.method", method)
            span.set_attribute("agenthive.request_id", self._request_id)
            try:
                if CONFIG.rate_limit_enabled and parsed.path not in exempt_paths:
                    with tracing.TRACER.start_as_current_span("ratelimit.check") as rl_span:
                        rl_span.set_attribute("agenthive.rate_limiter_kind", RATE_LIMITER.kind)
                        allowed, retry_after = RATE_LIMITER.allow(self._rate_limit_key())
                        rl_span.set_attribute("agenthive.rate_limit_allowed", allowed)
                    if not allowed:
                        status = 429
                        self._send(429, {"error": "rate limit exceeded"},
                                   {"Retry-After": str(retry_after)})
                        return

                for pattern, route_method, name, auth_level in ROUTES:
                    if route_method != method:
                        continue
                    m = pattern.match(parsed.path)
                    if not m:
                        continue
                    route_name = name
                    span.update_name(f"{method} {route_name}")
                    span.set_attribute("http.route", route_name)
                    params = m.groupdict()
                    query = parse_qs(parsed.query)
                    team_id_for_log = params.get("team_id")
                    if team_id_for_log:
                        span.set_attribute("agenthive.team_id", team_id_for_log)

                    user = None
                    if auth_level is not None:
                        try:
                            with tracing.TRACER.start_as_current_span("auth.authenticate"):
                                user = self._authenticate()
                        except AuthError as e:
                            status = e.status
                            self._send(e.status, {"error": e.message})
                            return
                        span.set_attribute("agenthive.user_id", user["id"])
                        if user["team_id"] != params.get("team_id"):
                            status = 403
                            self._send(403, {"error": "this API key does not belong to this team"})
                            return
                        try:
                            if auth_level == "admin":
                                require_role(user, "admin")
                            elif auth_level == "self_or_admin":
                                if user["role"] != "admin" and user["id"] != params.get("user_id"):
                                    raise AuthError(403, "can only rotate your own token (or be admin)")
                        except AuthError as e:
                            status = e.status
                            self._send(e.status, {"error": e.message})
                            return

                    handler = getattr(self, f"h_{name}")
                    try:
                        if user is not None:
                            status = handler(params, query, user) or 200
                        else:
                            status = handler(params, query) or 200
                    except ValueError as e:
                        status = 400
                        self._send(400, {"error": str(e)})
                    except Exception as e:  # pragma: no cover - safety net
                        status = 500
                        span.record_exception(e)
                        log.exception("unhandled error")
                        self._send(500, {"error": "internal error"})
                    return

                status = 404
                self._send(404, {"error": "no matching route"})
            finally:
                span.set_attribute("http.status_code", status)
                duration_s = time.monotonic() - start
                log_request(
                    request_id=getattr(self, "_request_id", ""),
                    method=method,
                    path=parsed.path,
                    status=status,
                    duration_ms=round(duration_s * 1000, 2),
                    team_id=team_id_for_log,
                    remote_addr=self.client_address[0],
                )
                if CONFIG.metrics_enabled and parsed.path != "/metrics":
                    metrics.HTTP_REQUESTS.labels(method=method, route=route_name, status=str(status)).inc()
                    metrics.HTTP_DURATION.labels(method=method, route=route_name).observe(duration_s)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # ---- unauthenticated routes ----

    def h_create_team(self, params, query):
        body = self._body()
        name = body.get("name")
        if not name:
            raise ValueError("name is required")
        team, user = STORE.create_team(name, body.get("owner_name", "owner"))
        self._send(201, {"team": team, "user": user})
        return 201

    def h_healthz(self, params, query):
        self._send(200, {"status": "ok"})
        return 200

    def h_readyz(self, params, query):
        if STORE.healthcheck():
            self._send(200, {"status": "ready"})
            return 200
        self._send(503, {"status": "not ready", "error": "database healthcheck failed"})
        return 503

    def h_metrics(self, params, query):
        self._send_raw(200, metrics.render(), metrics.CONTENT_TYPE)
        return 200

    def h_ui(self, params, query):
        global UI_HTML
        if UI_HTML is None:
            ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "index.html")
            with open(ui_path, "r", encoding="utf-8") as f:
                UI_HTML = f.read()
        self._send_html(200, UI_HTML)
        return 200

    # ---- users ----

    def h_create_user(self, params, query, user):
        body = self._body()
        if not body.get("name"):
            raise ValueError("name is required")
        role = body.get("role", "member")
        new_user = STORE.create_user(params["team_id"], body["name"], role, created_by=user["id"])
        self._send(201, new_user)
        return 201

    def h_list_users(self, params, query, user):
        self._send(200, {"users": STORE.list_users(params["team_id"])})
        return 200

    def h_rotate_user(self, params, query, user):
        target = STORE.get_user(params["user_id"])
        if not target or target["team_id"] != params["team_id"]:
            raise ValueError("user not found for this team")
        token, expires_at = STORE.rotate_user_token(params["user_id"])
        self._send(200, {"id": params["user_id"], "token": token, "token_expires_at": expires_at})
        return 200

    def h_revoke_user(self, params, query, user):
        target = STORE.get_user(params["user_id"])
        if not target or target["team_id"] != params["team_id"]:
            raise ValueError("user not found for this team")
        STORE.revoke_user(params["user_id"])
        self._send(200, {"id": params["user_id"], "revoked": True})
        return 200

    # ---- agents / tasks ----

    def h_create_agent(self, params, query, user):
        body = self._body()
        if not body.get("name"):
            raise ValueError("name is required")
        agent = STORE.create_agent(
            params["team_id"], body["name"],
            body.get("description", ""), body.get("system_prompt", ""),
        )
        self._send(201, agent)
        return 201

    def h_list_agents(self, params, query, user):
        self._send(200, {"agents": STORE.list_agents(params["team_id"])})
        return 200

    def h_create_task(self, params, query, user):
        body = self._body()
        if not body.get("name"):
            raise ValueError("name is required")
        task = STORE.create_task(params["team_id"], body["name"], body.get("description", ""))
        self._send(201, task)
        return 201

    def h_list_tasks(self, params, query, user):
        self._send(200, {"tasks": STORE.list_tasks(params["team_id"])})
        return 200

    # ---- memory ----

    def h_create_memory(self, params, query, user):
        body = self._body()
        if not body.get("title") or not body.get("body"):
            raise ValueError("title and body are required")
        with tracing.TRACER.start_as_current_span("memory.write") as span:
            node = STORE.create_memory(
                team_id=params["team_id"],
                title=body["title"],
                body=body["body"],
                agent_id=body.get("agent_id"),
                task_id=body.get("task_id"),
                tier=body.get("tier", "L1"),
                tags=body.get("tags", []),
                links=body.get("links", []),
            )
            span.set_attribute("agenthive.memory_node_id", node["id"])
            span.set_attribute("agenthive.status", node["status"])
            span.set_attribute("agenthive.auto_approved", bool(node.get("auto_approved")))
        metrics.record_write(auto_approved=bool(node.get("auto_approved")))
        if node["status"] == "approved" and CACHE is not None:
            with tracing.TRACER.start_as_current_span("cache.invalidate_team"):
                CACHE.invalidate_team(params["team_id"])  # a new approved node changes the graph
        self._send(201, node)
        return 201

    def h_list_pending(self, params, query, user):
        self._send(200, {"pending": STORE.list_pending(params["team_id"])})
        return 200

    def h_approve(self, params, query, user):
        body = self._body()
        node = STORE.get_memory(params["node_id"])
        if not node or node["team_id"] != params["team_id"]:
            raise ValueError("memory node not found for this team")
        with tracing.TRACER.start_as_current_span("memory.review") as span:
            span.set_attribute("agenthive.memory_node_id", params["node_id"])
            span.set_attribute("agenthive.decision", "approved")
            updated = STORE.review(params["node_id"], True, body.get("reviewed_by") or user["name"])
        metrics.record_review(approved=True)
        if CACHE is not None:
            with tracing.TRACER.start_as_current_span("cache.invalidate_team"):
                CACHE.invalidate_team(params["team_id"])  # newly-approved node joins the graph
        self._send(200, updated)
        return 200

    def h_reject(self, params, query, user):
        body = self._body()
        node = STORE.get_memory(params["node_id"])
        if not node or node["team_id"] != params["team_id"]:
            raise ValueError("memory node not found for this team")
        with tracing.TRACER.start_as_current_span("memory.review") as span:
            span.set_attribute("agenthive.memory_node_id", params["node_id"])
            span.set_attribute("agenthive.decision", "rejected")
            updated = STORE.review(
                params["node_id"], False, body.get("reviewed_by") or user["name"], body.get("reason", "")
            )
        metrics.record_review(approved=False)
        self._send(200, updated)
        return 200

    def h_retrieve_endpoint(self, params, query, user):
        anchor = (query.get("anchor") or [None])[0]
        if not anchor:
            raise ValueError("anchor query param is required")
        hops = int((query.get("hops") or [2])[0])
        hub_cutoff = int((query.get("hub_cutoff") or [15])[0])
        team_id = params["team_id"]

        cache_status = "disabled"
        cross_user = False
        result = None
        with tracing.TRACER.start_as_current_span("cache.lookup") as span:
            span.set_attribute("agenthive.anchor", anchor)
            if CACHE is not None:
                cached_value, populated_by = CACHE.get(team_id, anchor, hops, hub_cutoff)
                if cached_value is not None:
                    result = cached_value
                    cache_status = "hit"
                    cross_user = populated_by is not None and populated_by != user["id"]
            span.set_attribute("agenthive.cache_status", cache_status if result is not None else "miss")
            span.set_attribute("agenthive.cache_cross_user", cross_user)

        if result is None:
            with tracing.TRACER.start_as_current_span("retrieval.traverse") as span:
                span.set_attribute("agenthive.anchor", anchor)
                span.set_attribute("agenthive.hops", hops)
                span.set_attribute("agenthive.hub_cutoff", hub_cutoff)
                approved = STORE.approved_nodes(team_id)
                result = retrieve(approved, anchor, hops=hops, hub_cutoff=hub_cutoff)
                span.set_attribute("agenthive.neighborhood_count", result["neighborhood_count"])
                span.set_attribute("agenthive.approx_tokens", result["approx_tokens"])
                span.set_attribute("agenthive.reduction_pct", result["reduction_pct"])
            if CACHE is not None:
                with tracing.TRACER.start_as_current_span("cache.set"):
                    CACHE.set(team_id, anchor, hops, hub_cutoff, result, user["id"])
                cache_status = "miss"

        node_ids = [n["id"] for n in result["neighborhood"]]
        if node_ids:
            with tracing.TRACER.start_as_current_span("access_log.record") as span:
                span.set_attribute("agenthive.node_count", len(node_ids))
                STORE.record_access(team_id, node_ids, user["id"])
        metrics.record_retrieval(result, cache_status, cross_user)

        self._send(200, result)
        return 200

    # ---- auto-approve rules ----

    def h_create_rule(self, params, query, user):
        body = self._body()
        rule = STORE.create_auto_approve_rule(
            params["team_id"], created_by=user["name"],
            match_tag=body.get("match_tag"), match_agent_id=body.get("match_agent_id"),
        )
        self._send(201, rule)
        return 201

    def h_list_rules(self, params, query, user):
        self._send(200, {"rules": STORE.list_auto_approve_rules(params["team_id"], active_only=False)})
        return 200

    def h_delete_rule(self, params, query, user):
        STORE.deactivate_auto_approve_rule(params["team_id"], params["rule_id"])
        self._send(200, {"id": params["rule_id"], "active": False})
        return 200

    # ---- per-team metrics summary ----

    def h_team_metrics_summary(self, params, query, user):
        self._send(200, STORE.team_metrics_summary(params["team_id"]))
        return 200


def create_app(database_url: str = None, db_path: str = None) -> Store:
    """Builds a Store from explicit args, falling back to CONFIG (env vars).
    Used by main() and by tests that want an isolated DB per test."""
    global STORE
    STORE = Store(
        database_url=database_url if database_url is not None else CONFIG.database_url,
        db_path=db_path if db_path is not None else CONFIG.db_path,
    )
    metrics.bind_store(STORE)
    return STORE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=CONFIG.port)
    parser.add_argument("--host", default=CONFIG.host)
    parser.add_argument("--db", default=CONFIG.db_path,
                         help="SQLite path, ignored if DATABASE_URL is set")
    args = parser.parse_args()

    configure_logging()
    tracing_mode = tracing.init_tracing()
    create_app(db_path=args.db)

    server = ThreadingHTTPServer((args.host, args.port), Handler)

    scheme = "http"
    if CONFIG.tls_cert_file and CONFIG.tls_key_file:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=CONFIG.tls_cert_file, keyfile=CONFIG.tls_key_file)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"

    backend_kind = "postgres" if CONFIG.database_url.startswith(("postgres://", "postgresql://")) else "sqlite"
    cache_kind = CACHE.kind if CACHE is not None else "disabled"
    log.info(
        f"AgentHive listening on {scheme}://{args.host}:{args.port} "
        f"(backend: {backend_kind}, cache: {cache_kind}, rate-limiter: {RATE_LIMITER.kind}, "
        f"tracing: {tracing_mode})"
    )
    # SIGTERM (how Docker/Kubernetes ask a container to stop) has no
    # default Python handler, so without this the process would die
    # immediately and skip the tracing shutdown below - silently dropping
    # whatever spans the OTLP exporter's batch processor hadn't flushed
    # yet (its default export interval is ~5s). Converting it into
    # KeyboardInterrupt reuses the same graceful-shutdown path as Ctrl+C.
    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        tracing.shutdown_tracing()


if __name__ == "__main__":
    main()
