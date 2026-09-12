"""
db.py - SQLite storage layer for the team memory service.

No dependencies beyond the Python standard library. Same philosophy as
scripts/retrieve.py and scripts/log_session.py in the Obsidian vault:
this should run anywhere `python3` runs, with zero pip installs.
"""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    api_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id),
    name TEXT NOT NULL,
    description TEXT,
    system_prompt TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id),
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_nodes (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id),
    agent_id TEXT REFERENCES agents(id),
    task_id TEXT REFERENCES tasks(id),
    tier TEXT NOT NULL DEFAULT 'L1',
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    links TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT,
    reject_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_team_status
    ON memory_nodes(team_id, status);
CREATE INDEX IF NOT EXISTS idx_memory_team_title
    ON memory_nodes(team_id, title);
"""


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Store:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- teams ----

    def create_team(self, name: str) -> dict:
        team_id = new_id()
        api_key = "tm-" + uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO teams (id, name, api_key, created_at) VALUES (?, ?, ?, ?)",
                (team_id, name, api_key, now()),
            )
        return {"id": team_id, "name": name, "api_key": api_key}

    def team_by_api_key(self, api_key: str):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM teams WHERE api_key = ?", (api_key,)
            ).fetchone()
            return dict(row) if row else None

    def team_by_id(self, team_id: str):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM teams WHERE id = ?", (team_id,)
            ).fetchone()
            return dict(row) if row else None

    # ---- agents ----

    def create_agent(self, team_id: str, name: str, description: str = "",
                      system_prompt: str = "") -> dict:
        agent_id = new_id()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO agents (id, team_id, name, description, system_prompt, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, team_id, name, description, system_prompt, now()),
            )
        return {"id": agent_id, "team_id": team_id, "name": name,
                "description": description, "system_prompt": system_prompt}

    def list_agents(self, team_id: str):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM agents WHERE team_id = ? ORDER BY created_at", (team_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- tasks ----

    def create_task(self, team_id: str, name: str, description: str = "") -> dict:
        task_id = new_id()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO tasks (id, team_id, name, description, status, created_at) "
                "VALUES (?, ?, ?, ?, 'open', ?)",
                (task_id, team_id, name, description, now()),
            )
        return {"id": task_id, "team_id": team_id, "name": name, "description": description}

    def list_tasks(self, team_id: str):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE team_id = ? ORDER BY created_at", (team_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- memory nodes ----

    def create_memory(self, team_id: str, title: str, body: str,
                       agent_id: str = None, task_id: str = None,
                       tier: str = "L1", tags=None, links=None) -> dict:
        node_id = new_id()
        tags = json.dumps(tags or [])
        links = json.dumps(links or [])
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO memory_nodes "
                "(id, team_id, agent_id, task_id, tier, title, body, tags, links, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (node_id, team_id, agent_id, task_id, tier, title, body, tags, links, now()),
            )
        return self.get_memory(node_id)

    def get_memory(self, node_id: str):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM memory_nodes WHERE id = ?", (node_id,)
            ).fetchone()
            return _row_to_node(row) if row else None

    def list_pending(self, team_id: str):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_nodes WHERE team_id = ? AND status = 'pending' "
                "ORDER BY created_at",
                (team_id,),
            ).fetchall()
            return [_row_to_node(r) for r in rows]

    def review(self, node_id: str, approve: bool, reviewed_by: str, reason: str = ""):
        status = "approved" if approve else "rejected"
        with self._conn() as conn:
            conn.execute(
                "UPDATE memory_nodes SET status = ?, reviewed_at = ?, reviewed_by = ?, "
                "reject_reason = ? WHERE id = ?",
                (status, now(), reviewed_by, reason, node_id),
            )
        return self.get_memory(node_id)

    def approved_nodes(self, team_id: str):
        """All approved nodes for a team, keyed by lowercase title (for traversal)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_nodes WHERE team_id = ? AND status = 'approved'",
                (team_id,),
            ).fetchall()
            return [_row_to_node(r) for r in rows]


def _row_to_node(row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d["tags"])
    d["links"] = json.loads(d["links"])
    return d
