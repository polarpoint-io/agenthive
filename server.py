#!/usr/bin/env python3
"""
server.py - the team memory service, v0.

No dependencies beyond the Python standard library (http.server + sqlite3).
Run it with:  python3 server.py [--port 8790] [--db team_memory.db]

Endpoints:
  POST /teams                                  {name} -> {id, name, api_key}
  POST /teams/{team_id}/agents                 {name, description, system_prompt}
  GET  /teams/{team_id}/agents
  POST /teams/{team_id}/tasks                  {name, description}
  GET  /teams/{team_id}/tasks
  POST /teams/{team_id}/memory                 {title, body, agent_id?, task_id?, tier?, tags?, links?}
  GET  /teams/{team_id}/memory/pending
  POST /teams/{team_id}/memory/{id}/approve    {reviewed_by}
  POST /teams/{team_id}/memory/{id}/reject     {reviewed_by, reason?}
  GET  /teams/{team_id}/memory/retrieve?anchor=...&hops=2&hub_cutoff=15

All routes except POST /teams require header:  X-API-Key: <team's api_key>
This is a placeholder auth model - one shared secret per team, no per-user
identity, no rotation. See ADR.md "Open questions" before using this for
anything with real access-control requirements.
"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from db import Store
from retrieval import retrieve

STORE: Store = None  # set in main()

ROUTES = [
    (re.compile(r"^/teams$"), "POST", "create_team"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/agents$"), "POST", "create_agent"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/agents$"), "GET", "list_agents"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/tasks$"), "POST", "create_task"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/tasks$"), "GET", "list_tasks"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/memory$"), "POST", "create_memory"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/memory/pending$"), "GET", "list_pending"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/memory/(?P<node_id>[\w-]+)/approve$"), "POST", "approve"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/memory/(?P<node_id>[\w-]+)/reject$"), "POST", "reject"),
    (re.compile(r"^/teams/(?P<team_id>[\w-]+)/memory/retrieve$"), "GET", "retrieve_endpoint"),
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep test output clean; real deploys should log to a file

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _authorized_team(self, team_id: str):
        """Returns the team dict if X-API-Key matches this team_id, else None."""
        api_key = self.headers.get("X-API-Key", "")
        team = STORE.team_by_api_key(api_key)
        if not team or team["id"] != team_id:
            return None
        return team

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        for pattern, route_method, name in ROUTES:
            if route_method != method:
                continue
            m = pattern.match(parsed.path)
            if not m:
                continue
            params = m.groupdict()
            query = parse_qs(parsed.query)

            if name != "create_team":
                team_id = params.get("team_id")
                if not self._authorized_team(team_id):
                    return self._send(401, {"error": "invalid or missing X-API-Key for this team"})

            handler = getattr(self, f"h_{name}")
            try:
                handler(params, query)
            except ValueError as e:
                self._send(400, {"error": str(e)})
            except Exception as e:  # pragma: no cover - safety net for a v0 service
                self._send(500, {"error": f"internal error: {e}"})
            return

        self._send(404, {"error": "no matching route"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # ---- route handlers ----

    def h_create_team(self, params, query):
        body = self._body()
        name = body.get("name")
        if not name:
            raise ValueError("name is required")
        self._send(201, STORE.create_team(name))

    def h_create_agent(self, params, query):
        body = self._body()
        if not body.get("name"):
            raise ValueError("name is required")
        agent = STORE.create_agent(
            params["team_id"], body["name"],
            body.get("description", ""), body.get("system_prompt", ""),
        )
        self._send(201, agent)

    def h_list_agents(self, params, query):
        self._send(200, {"agents": STORE.list_agents(params["team_id"])})

    def h_create_task(self, params, query):
        body = self._body()
        if not body.get("name"):
            raise ValueError("name is required")
        task = STORE.create_task(params["team_id"], body["name"], body.get("description", ""))
        self._send(201, task)

    def h_list_tasks(self, params, query):
        self._send(200, {"tasks": STORE.list_tasks(params["team_id"])})

    def h_create_memory(self, params, query):
        body = self._body()
        if not body.get("title") or not body.get("body"):
            raise ValueError("title and body are required")
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
        self._send(201, node)

    def h_list_pending(self, params, query):
        self._send(200, {"pending": STORE.list_pending(params["team_id"])})

    def h_approve(self, params, query):
        body = self._body()
        node = STORE.get_memory(params["node_id"])
        if not node or node["team_id"] != params["team_id"]:
            raise ValueError("memory node not found for this team")
        updated = STORE.review(params["node_id"], True, body.get("reviewed_by", "unknown"))
        self._send(200, updated)

    def h_reject(self, params, query):
        body = self._body()
        node = STORE.get_memory(params["node_id"])
        if not node or node["team_id"] != params["team_id"]:
            raise ValueError("memory node not found for this team")
        updated = STORE.review(
            params["node_id"], False, body.get("reviewed_by", "unknown"), body.get("reason", "")
        )
        self._send(200, updated)

    def h_retrieve_endpoint(self, params, query):
        anchor = (query.get("anchor") or [None])[0]
        if not anchor:
            raise ValueError("anchor query param is required")
        hops = int((query.get("hops") or [2])[0])
        hub_cutoff = int((query.get("hub_cutoff") or [15])[0])

        approved = STORE.approved_nodes(params["team_id"])
        result = retrieve(approved, anchor, hops=hops, hub_cutoff=hub_cutoff)
        self._send(200, result)


def main():
    global STORE
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--db", default="team_memory.db")
    args = parser.parse_args()

    STORE = Store(args.db)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"team-memory-service listening on http://127.0.0.1:{args.port}  (db: {args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
