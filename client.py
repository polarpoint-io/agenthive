"""
client.py - small client library an agent session (or a human admin
script) calls explicitly.

Deliberately not a proxy. There is no interception of LLM requests here.
An agent session (or a CLAUDE.md / .cursor/rules instruction telling it to)
makes at most two calls per session:

  1. at the start: retrieve_context(...)  - a bounded, opt-in read
  2. at the end:   log_session(...)       - writes a memory node. It is
                                             PENDING unless an admin has
                                             configured an auto-approve
                                             rule that matches it.

Auth: every call is made as a specific user's personal token (role
'admin' or 'member'), not a shared team secret - see auth.py / ADR.md.

No hard dependencies beyond the standard library. tracing.py is imported
for distributed tracing (see its docstring) but is itself a no-op unless
OpenTelemetry is installed AND tracing is enabled server-side - so this
client still needs nothing extra to just work.

Every call here starts its own client-side span and propagates it to the
server (see tracing.py's inject_headers), so even a single retrieve_context
or log_session call shows up as one connected trace spanning the network
hop. Wrap several calls in `with client.traced_session("..."):` to link a
whole agent session - e.g. retrieve at the start and log at the end -
into one trace instead of two separate ones.
"""
import json
import urllib.request
import urllib.parse
import urllib.error
from contextlib import contextmanager

import tracing

# A plain script that only ever does `import client` (never imports
# server.py) still gets its own client-side spans exported if it sets
# OTEL_EXPORTER_OTLP_ENDPOINT / AGENTHIVE_TRACING_CONSOLE - same env vars,
# no separate setup call needed. Idempotent - a process that also runs
# server.py's main() (which calls this too) configures tracing exactly
# once either way.
tracing.init_tracing()


class TeamMemoryClient:
    def __init__(self, base_url: str, api_key: str, team_id: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.team_id = team_id

    def _request(self, method: str, path: str, body: dict = None, query: dict = None,
                 span_name: str = None):
        with tracing.start_client_span(span_name or f"{method} {path}") as span:
            url = self.base_url + path
            if query:
                url += "?" + urllib.parse.urlencode(query)
            data = json.dumps(body).encode("utf-8") if body is not None else None
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("X-API-Key", self.api_key)
            req.add_header("Content-Type", "application/json")
            # Propagates this span's W3C traceparent to the server, so the
            # server-side spans this call triggers (see server.py) show up
            # as children of THIS span rather than starting a fresh trace.
            trace_headers = tracing.inject_headers({})
            for k, v in trace_headers.items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    span.set_attribute("http.status_code", resp.status)
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                span.set_attribute("http.status_code", e.code)
                detail = json.loads(e.read())
                raise RuntimeError(f"{e.code}: {detail.get('error', 'unknown error')}") from None

    @contextmanager
    def traced_session(self, name: str = "agent_session"):
        """Wraps a block of calls (typically retrieve_context(...) at the
        start of an agent session and log_session(...) at the end) in one
        parent span, so they show up in Jaeger/Tempo/etc. as one connected
        trace - "this session's whole journey" - instead of two unrelated
        ones. Purely a tracing nicety; the calls work identically without
        it, each just gets its own top-level trace instead."""
        with tracing.TRACER.start_as_current_span(name):
            yield

    # ---- the two calls an agent session actually makes ----

    def retrieve_context(self, anchor: str, hops: int = 2, hub_cutoff: int = 15) -> dict:
        """Call at the start of a session. Returns a bounded neighborhood of
        approved team memory around `anchor`, not a full dump."""
        return self._request(
            "GET", "/teams/" + self._team_id() + "/memory/retrieve",
            query={"anchor": anchor, "hops": hops, "hub_cutoff": hub_cutoff},
            span_name="retrieve_context",
        )

    def log_session(self, title: str, body: str, agent_id: str = None,
                     task_id: str = None, tags=None, links=None, tier: str = "L1") -> dict:
        """Call at the end of a session. Writes a memory node - PENDING
        unless an auto-approve rule matches it (see create_auto_approve_rule)."""
        return self._request(
            "POST", "/teams/" + self._team_id() + "/memory",
            body={
                "title": title, "body": body, "agent_id": agent_id,
                "task_id": task_id, "tags": tags or [], "links": links or [],
                "tier": tier,
            },
            span_name="log_session",
        )

    # ---- admin / setup helpers ----

    def create_agent(self, name: str, description: str = "", system_prompt: str = "") -> dict:
        return self._request(
            "POST", "/teams/" + self._team_id() + "/agents",
            body={"name": name, "description": description, "system_prompt": system_prompt},
        )

    def create_task(self, name: str, description: str = "") -> dict:
        return self._request(
            "POST", "/teams/" + self._team_id() + "/tasks",
            body={"name": name, "description": description},
        )

    def list_pending(self) -> dict:
        return self._request("GET", "/teams/" + self._team_id() + "/memory/pending")

    def approve(self, node_id: str, reviewed_by: str = None) -> dict:
        return self._request(
            "POST", "/teams/" + self._team_id() + f"/memory/{node_id}/approve",
            body={"reviewed_by": reviewed_by} if reviewed_by else {},
        )

    def reject(self, node_id: str, reviewed_by: str = None, reason: str = "") -> dict:
        return self._request(
            "POST", "/teams/" + self._team_id() + f"/memory/{node_id}/reject",
            body={"reviewed_by": reviewed_by, "reason": reason},
        )

    # ---- users (admin only on the server side) ----

    def create_user(self, name: str, role: str = "member") -> dict:
        """role: 'admin' or 'member'. Returns the new user's token - shown
        once, store it somewhere real."""
        return self._request(
            "POST", "/teams/" + self._team_id() + "/users",
            body={"name": name, "role": role},
        )

    def list_users(self) -> dict:
        return self._request("GET", "/teams/" + self._team_id() + "/users")

    def rotate_token(self, user_id: str) -> dict:
        return self._request(
            "POST", "/teams/" + self._team_id() + f"/users/{user_id}/rotate",
        )

    def revoke_user(self, user_id: str) -> dict:
        return self._request(
            "POST", "/teams/" + self._team_id() + f"/users/{user_id}/revoke",
        )

    # ---- auto-approve rules ----

    def create_auto_approve_rule(self, match_tag: str = None, match_agent_id: str = None) -> dict:
        return self._request(
            "POST", "/teams/" + self._team_id() + "/auto-approve-rules",
            body={"match_tag": match_tag, "match_agent_id": match_agent_id},
        )

    def list_auto_approve_rules(self) -> dict:
        return self._request("GET", "/teams/" + self._team_id() + "/auto-approve-rules")

    def delete_auto_approve_rule(self, rule_id: str) -> dict:
        return self._request(
            "DELETE", "/teams/" + self._team_id() + f"/auto-approve-rules/{rule_id}",
        )

    def _team_id(self) -> str:
        return self.team_id


def create_team(base_url: str, name: str, owner_name: str = "owner") -> dict:
    """One-time setup call: POST /teams needs no API key yet. Returns
    {"team": {...}, "user": {...with a 'token' field, shown once...}}."""
    with tracing.start_client_span("create_team"):
        headers = tracing.inject_headers({"Content-Type": "application/json"})
        req = urllib.request.Request(
            base_url.rstrip("/") + "/teams",
            data=json.dumps({"name": name, "owner_name": owner_name}).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
