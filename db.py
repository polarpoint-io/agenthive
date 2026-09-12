"""
db.py - storage layer for AgentHive.

Two backends share one schema (see SCHEMA below):

- SqliteBackend: zero-dependency, one file. Still the default - good for a
  single team / single node running this on a laptop or one small VM.
- PostgresBackend: used automatically when DATABASE_URL is set (see
  config.py). Needed once multiple teams are writing concurrently at real
  volume - see ADR.md "Open questions". psycopg2 is only imported when this
  backend is actually selected, so a SQLite-only install never needs it.

The schema is written to be valid SQL in both databases (TEXT-typed
columns, app-generated ids, "CREATE TABLE/INDEX IF NOT EXISTS") so no
per-backend DDL branching is needed - this is the "the schema translates
directly" migration path the original ADR called out.
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
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id),
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    created_by TEXT,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_team ON users(team_id);

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

CREATE TABLE IF NOT EXISTS auto_approve_rules (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id),
    match_tag TEXT,
    match_agent_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_rules_team ON auto_approve_rules(team_id, active);

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
    reject_reason TEXT,
    auto_approved INTEGER NOT NULL DEFAULT 0,
    rule_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_team_status
    ON memory_nodes(team_id, status);
CREATE INDEX IF NOT EXISTS idx_memory_team_title
    ON memory_nodes(team_id, title);

-- One row per (retrieval, node) pair: which node a retrieve_context call
-- actually returned, to whom, and when. This is what lets the metrics
-- summary answer "is the graph being reused across team members" with a
-- number (distinct users per node) instead of an assertion. retrieval_id
-- groups the rows from one call so "total retrievals" is a COUNT(DISTINCT),
-- not a row count.
CREATE TABLE IF NOT EXISTS memory_access_log (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id),
    retrieval_id TEXT NOT NULL,
    node_id TEXT NOT NULL REFERENCES memory_nodes(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    accessed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_access_team ON memory_access_log(team_id);
CREATE INDEX IF NOT EXISTS idx_access_node ON memory_access_log(node_id);
"""


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class SqliteBackend:
    paramstyle = "?"

    def __init__(self, path: str):
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple = ()):
        with self._conn() as conn:
            conn.execute(sql, params)

    def query_one(self, sql: str, params: tuple = ()):
        with self._conn() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def query_all(self, sql: str, params: tuple = ()):
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def healthcheck(self) -> bool:
        try:
            with self._conn() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False


class PostgresBackend:
    paramstyle = "%s"

    def __init__(self, database_url: str):
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "DATABASE_URL points at Postgres but psycopg2 is not "
                "installed. `pip install psycopg2-binary` (see requirements.txt)."
            ) from e
        self._psycopg2 = psycopg2
        self._extras = psycopg2.extras
        self.database_url = database_url
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = self._psycopg2.connect(self.database_url)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _to_pg(self, sql: str) -> str:
        # Our SQL never contains a literal "?" outside of placeholders.
        return sql.replace("?", "%s")

    def execute(self, sql: str, params: tuple = ()):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(self._to_pg(sql), params)

    def query_one(self, sql: str, params: tuple = ()):
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(self._to_pg(sql), params)
                row = cur.fetchone()
                return dict(row) if row else None

    def query_all(self, sql: str, params: tuple = ()):
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(self._to_pg(sql), params)
                return [dict(r) for r in cur.fetchall()]

    def healthcheck(self) -> bool:
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            return True
        except Exception:
            return False


def make_backend(database_url: str = "", db_path: str = "agenthive.db"):
    if database_url and database_url.startswith(("postgres://", "postgresql://")):
        return PostgresBackend(database_url)
    return SqliteBackend(db_path)


# ---------------------------------------------------------------------------
# Store - the high-level API server.py talks to
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, database_url: str = "", db_path: str = "agenthive.db", backend=None):
        self.backend = backend or make_backend(database_url, db_path)

    def healthcheck(self) -> bool:
        return self.backend.healthcheck()

    # ---- teams + bootstrap admin user ----

    def create_team(self, name: str, owner_name: str = "owner"):
        """Creates a team and its first user (role=admin). Returns
        (team_dict, user_dict_with_token). The token is only ever returned
        here and at explicit create/rotate calls - it is never stored or
        returned in plaintext again."""
        from auth import generate_token, hash_token

        team_id = new_id()
        self.backend.execute(
            "INSERT INTO teams (id, name, created_at) VALUES (?, ?, ?)",
            (team_id, name, now()),
        )
        token = generate_token()
        user_id = new_id()
        self.backend.execute(
            "INSERT INTO users (id, team_id, name, role, token_hash, created_at, created_by) "
            "VALUES (?, ?, ?, 'admin', ?, ?, 'bootstrap')",
            (user_id, team_id, owner_name, hash_token(token), now()),
        )
        team = {"id": team_id, "name": name}
        user = {"id": user_id, "team_id": team_id, "name": owner_name,
                "role": "admin", "token": token}
        return team, user

    def team_by_id(self, team_id: str):
        return self.backend.query_one("SELECT * FROM teams WHERE id = ?", (team_id,))

    # ---- users / auth ----

    def create_user(self, team_id: str, name: str, role: str, created_by: str):
        from auth import generate_token, hash_token

        if role not in ("admin", "member"):
            raise ValueError("role must be 'admin' or 'member'")
        token = generate_token()
        user_id = new_id()
        self.backend.execute(
            "INSERT INTO users (id, team_id, name, role, token_hash, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, team_id, name, role, hash_token(token), now(), created_by),
        )
        return {"id": user_id, "team_id": team_id, "name": name, "role": role, "token": token}

    def list_users(self, team_id: str):
        rows = self.backend.query_all(
            "SELECT id, team_id, name, role, created_at, created_by, last_used_at, revoked_at "
            "FROM users WHERE team_id = ? ORDER BY created_at",
            (team_id,),
        )
        return rows

    def get_user(self, user_id: str):
        row = self.backend.query_one(
            "SELECT id, team_id, name, role, created_at, created_by, last_used_at, revoked_at "
            "FROM users WHERE id = ?",
            (user_id,),
        )
        return row

    def user_by_token(self, token: str):
        """Returns the active (non-revoked) user matching this token, else
        None. Updates last_used_at on success. Constant-time-ish because
        we hash first and compare by unique index, not by scanning."""
        from auth import hash_token

        token_hash = hash_token(token)
        row = self.backend.query_one(
            "SELECT * FROM users WHERE token_hash = ? AND revoked_at IS NULL",
            (token_hash,),
        )
        if row:
            self.backend.execute(
                "UPDATE users SET last_used_at = ? WHERE id = ?", (now(), row["id"])
            )
        return row

    def rotate_user_token(self, user_id: str):
        from auth import generate_token, hash_token

        token = generate_token()
        self.backend.execute(
            "UPDATE users SET token_hash = ?, revoked_at = NULL WHERE id = ?",
            (hash_token(token), user_id),
        )
        return token

    def revoke_user(self, user_id: str):
        self.backend.execute(
            "UPDATE users SET revoked_at = ? WHERE id = ?", (now(), user_id)
        )

    # ---- agents ----

    def create_agent(self, team_id: str, name: str, description: str = "",
                      system_prompt: str = "") -> dict:
        agent_id = new_id()
        self.backend.execute(
            "INSERT INTO agents (id, team_id, name, description, system_prompt, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, team_id, name, description, system_prompt, now()),
        )
        return {"id": agent_id, "team_id": team_id, "name": name,
                "description": description, "system_prompt": system_prompt}

    def list_agents(self, team_id: str):
        return self.backend.query_all(
            "SELECT * FROM agents WHERE team_id = ? ORDER BY created_at", (team_id,)
        )

    # ---- tasks ----

    def create_task(self, team_id: str, name: str, description: str = "") -> dict:
        task_id = new_id()
        self.backend.execute(
            "INSERT INTO tasks (id, team_id, name, description, status, created_at) "
            "VALUES (?, ?, ?, ?, 'open', ?)",
            (task_id, team_id, name, description, now()),
        )
        return {"id": task_id, "team_id": team_id, "name": name, "description": description}

    def list_tasks(self, team_id: str):
        return self.backend.query_all(
            "SELECT * FROM tasks WHERE team_id = ? ORDER BY created_at", (team_id,)
        )

    # ---- auto-approve rules ----

    def create_auto_approve_rule(self, team_id: str, created_by: str,
                                  match_tag: str = None, match_agent_id: str = None) -> dict:
        if not match_tag and not match_agent_id:
            raise ValueError("rule needs match_tag or match_agent_id")
        rule_id = new_id()
        self.backend.execute(
            "INSERT INTO auto_approve_rules "
            "(id, team_id, match_tag, match_agent_id, created_by, created_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (rule_id, team_id, match_tag, match_agent_id, created_by, now()),
        )
        return self.backend.query_one(
            "SELECT * FROM auto_approve_rules WHERE id = ?", (rule_id,)
        )

    def list_auto_approve_rules(self, team_id: str, active_only: bool = True):
        if active_only:
            return self.backend.query_all(
                "SELECT * FROM auto_approve_rules WHERE team_id = ? AND active = 1 "
                "ORDER BY created_at",
                (team_id,),
            )
        return self.backend.query_all(
            "SELECT * FROM auto_approve_rules WHERE team_id = ? ORDER BY created_at",
            (team_id,),
        )

    def deactivate_auto_approve_rule(self, team_id: str, rule_id: str):
        self.backend.execute(
            "UPDATE auto_approve_rules SET active = 0 WHERE id = ? AND team_id = ?",
            (rule_id, team_id),
        )

    def find_matching_rule(self, team_id: str, tags, agent_id):
        rules = self.list_auto_approve_rules(team_id, active_only=True)
        tags = set(tags or [])
        for rule in rules:
            if rule["match_tag"] and rule["match_tag"] in tags:
                return rule
            if rule["match_agent_id"] and rule["match_agent_id"] == agent_id:
                return rule
        return None

    # ---- memory nodes ----

    def create_memory(self, team_id: str, title: str, body: str,
                       agent_id: str = None, task_id: str = None,
                       tier: str = "L1", tags=None, links=None) -> dict:
        node_id = new_id()
        tags = tags or []
        links_list = links or []

        rule = self.find_matching_rule(team_id, tags, agent_id)
        status = "approved" if rule else "pending"
        reviewed_at = now() if rule else None
        reviewed_by = f"auto-approve:{rule['id']}" if rule else None

        self.backend.execute(
            "INSERT INTO memory_nodes "
            "(id, team_id, agent_id, task_id, tier, title, body, tags, links, status, "
            " created_at, reviewed_at, reviewed_by, auto_approved, rule_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (node_id, team_id, agent_id, task_id, tier, title, body,
             json.dumps(tags), json.dumps(links_list), status,
             now(), reviewed_at, reviewed_by,
             1 if rule else 0, rule["id"] if rule else None),
        )
        return self.get_memory(node_id)

    def get_memory(self, node_id: str):
        row = self.backend.query_one("SELECT * FROM memory_nodes WHERE id = ?", (node_id,))
        return _row_to_node(row) if row else None

    def list_pending(self, team_id: str):
        rows = self.backend.query_all(
            "SELECT * FROM memory_nodes WHERE team_id = ? AND status = 'pending' "
            "ORDER BY created_at",
            (team_id,),
        )
        return [_row_to_node(r) for r in rows]

    def review(self, node_id: str, approve: bool, reviewed_by: str, reason: str = ""):
        status = "approved" if approve else "rejected"
        self.backend.execute(
            "UPDATE memory_nodes SET status = ?, reviewed_at = ?, reviewed_by = ?, "
            "reject_reason = ? WHERE id = ?",
            (status, now(), reviewed_by, reason, node_id),
        )
        return self.get_memory(node_id)

    def approved_nodes(self, team_id: str):
        rows = self.backend.query_all(
            "SELECT * FROM memory_nodes WHERE team_id = ? AND status = 'approved'",
            (team_id,),
        )
        return [_row_to_node(r) for r in rows]

    # ---- access logging + metrics ----

    def record_access(self, team_id: str, node_ids, user_id: str) -> str:
        """Logs one retrieval touching `node_ids`, all under one
        retrieval_id so downstream queries can COUNT(DISTINCT retrieval_id)
        for 'how many retrievals' instead of 'how many node rows'."""
        retrieval_id = new_id()
        ts = now()
        for node_id in node_ids:
            self.backend.execute(
                "INSERT INTO memory_access_log (id, team_id, retrieval_id, node_id, user_id, accessed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (new_id(), team_id, retrieval_id, node_id, user_id, ts),
            )
        return retrieval_id

    def team_metrics_summary(self, team_id: str) -> dict:
        node_rows = self.backend.query_all(
            "SELECT status, COUNT(*) as n FROM memory_nodes WHERE team_id = ? GROUP BY status",
            (team_id,),
        )
        node_counts = {r["status"]: r["n"] for r in node_rows}

        retrievals_row = self.backend.query_one(
            "SELECT COUNT(DISTINCT retrieval_id) as n FROM memory_access_log WHERE team_id = ?",
            (team_id,),
        )
        total_retrievals = retrievals_row["n"] if retrievals_row else 0

        active_users_row = self.backend.query_one(
            "SELECT COUNT(*) as n FROM users WHERE team_id = ? AND revoked_at IS NULL",
            (team_id,),
        )
        active_users = active_users_row["n"] if active_users_row else 0

        top_nodes = self.backend.query_all(
            "SELECT n.id, n.title, "
            "       COUNT(DISTINCT a.user_id) as distinct_users, "
            "       COUNT(DISTINCT a.retrieval_id) as access_count "
            "FROM memory_access_log a "
            "JOIN memory_nodes n ON n.id = a.node_id "
            "WHERE a.team_id = ? "
            "GROUP BY n.id, n.title "
            "ORDER BY distinct_users DESC, access_count DESC "
            "LIMIT 10",
            (team_id,),
        )

        rule_count_row = self.backend.query_one(
            "SELECT COUNT(*) as n FROM auto_approve_rules WHERE team_id = ? AND active = 1",
            (team_id,),
        )

        return {
            "node_counts": node_counts,
            "total_retrievals": total_retrievals,
            "active_users": active_users,
            "active_auto_approve_rules": rule_count_row["n"] if rule_count_row else 0,
            "top_reused_nodes": top_nodes,
        }

    # ---- global (cross-team) stats for the /metrics gauges ----
    # No team_id in the Prometheus labels (see metrics.py) - these three
    # are the only cross-team numbers exposed there.

    def global_node_status_counts(self) -> dict:
        rows = self.backend.query_all(
            "SELECT status, COUNT(*) as n FROM memory_nodes GROUP BY status"
        )
        return {r["status"]: r["n"] for r in rows}

    def global_active_user_count(self) -> int:
        row = self.backend.query_one("SELECT COUNT(*) as n FROM users WHERE revoked_at IS NULL")
        return row["n"] if row else 0

    def global_team_count(self) -> int:
        row = self.backend.query_one("SELECT COUNT(*) as n FROM teams")
        return row["n"] if row else 0


def _row_to_node(row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d["tags"]) if isinstance(d["tags"], str) else d["tags"]
    d["links"] = json.loads(d["links"]) if isinstance(d["links"], str) else d["links"]
    return d
