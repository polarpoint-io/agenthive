"""
client.py - small client library an agent session calls explicitly.

Deliberately not a proxy. There is no interception of LLM requests here.
An agent session (or a CLAUDE.md / .cursor/rules instruction telling it to)
makes at most two calls per session:

  1. at the start: retrieve_context(...)  - a bounded, opt-in read
  2. at the end:   log_session(...)       - writes a PENDING memory node
                                             that a human still has to approve

No dependencies beyond the standard library.
"""
import json
import urllib.request
import urllib.parse
import urllib.error


class TeamMemoryClient:
    def __init__(self, base_url: str, api_key: str, team_id: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.team_id = team_id

    def _request(self, method: str, path: str, body: dict = None, query: dict = None):
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-API-Key", self.api_key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = json.loads(e.read())
            raise RuntimeError(f"{e.code}: {detail.get('error', 'unknown error')}") from None

    # ---- the two calls an agent session actually makes ----

    def retrieve_context(self, anchor: str, hops: int = 2, hub_cutoff: int = 15) -> dict:
        """Call at the start of a session. Returns a bounded neighborhood of
        approved team memory around `anchor`, not a full dump."""
        return self._request(
            "GET", "/teams/" + self._team_id() + "/memory/retrieve",
            query={"anchor": anchor, "hops": hops, "hub_cutoff": hub_cutoff},
        )

    def log_session(self, title: str, body: str, agent_id: str = None,
                     task_id: str = None, tags=None, links=None, tier: str = "L1") -> dict:
        """Call at the end of a session. Writes a PENDING node - invisible to
        retrieval until a human approves it."""
        return self._request(
            "POST", "/teams/" + self._team_id() + "/memory",
            body={
                "title": title, "body": body, "agent_id": agent_id,
                "task_id": task_id, "tags": tags or [], "links": links or [],
                "tier": tier,
            },
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

    def approve(self, node_id: str, reviewed_by: str) -> dict:
        return self._request(
            "POST", "/teams/" + self._team_id() + f"/memory/{node_id}/approve",
            body={"reviewed_by": reviewed_by},
        )

    def reject(self, node_id: str, reviewed_by: str, reason: str = "") -> dict:
        return self._request(
            "POST", "/teams/" + self._team_id() + f"/memory/{node_id}/reject",
            body={"reviewed_by": reviewed_by, "reason": reason},
        )

    def _team_id(self) -> str:
        return self.team_id


def create_team(base_url: str, name: str) -> dict:
    """One-time setup call: POST /teams needs no API key yet."""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/teams",
        data=json.dumps({"name": name}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())
