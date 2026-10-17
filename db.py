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
import governance
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
    revoked_at TEXT,
    token_expires_at TEXT,
    azure_oid TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_team ON users(team_id);

-- The unique index on users.azure_oid is NOT created here, deliberately -
-- see the "Only one AgentHive user per Azure AD object id" comment in
-- SqliteBackend/PostgresBackend.__init__ below for why it has to run
-- after the additive azure_oid migration instead of inline with the rest
-- of this schema.

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
-- Mirror of auto_approve_rules, for the other direction. A separate
-- table rather than a `decision` column on that one: this schema has no
-- migration runner beyond a couple of hand-written ALTERs in
-- SqliteBackend, and CREATE TABLE IF NOT EXISTS is the only change that
-- applies cleanly to an existing database on BOTH backends.
CREATE TABLE IF NOT EXISTS auto_reject_rules (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id),
    match_tag TEXT,
    match_agent_id TEXT,
    reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

-- A phrasing a human has mapped to a node, almost always while clearing
-- it off the gap list. This is the cheap approximation of semantic
-- search: no embeddings, no model, because a person did the matching
-- once and everybody gets the answer afterwards.
--
-- PK on (team_id, anchor_key) so one phrasing resolves to one node and
-- re-aliasing replaces rather than accumulating ambiguity.
CREATE TABLE IF NOT EXISTS anchor_aliases (
    team_id TEXT NOT NULL REFERENCES teams(id),
    anchor_key TEXT NOT NULL,
    anchor TEXT NOT NULL,
    node_id TEXT NOT NULL REFERENCES memory_nodes(id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (team_id, anchor_key)
);

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

-- One row per (team, normalised anchor) that returned no approved note.
-- Aggregated on purpose: the review UI needs "what do people keep
-- missing" without a row per call, and the anchor text must not become
-- a Prometheus label. anchor_key is the trimmed lower-case anchor.
CREATE TABLE IF NOT EXISTS retrieval_misses (
    team_id TEXT NOT NULL REFERENCES teams(id),
    anchor_key TEXT NOT NULL,
    anchor TEXT NOT NULL,
    miss_count INTEGER NOT NULL,
    last_user_id TEXT NOT NULL REFERENCES users(id),
    last_missed_at TEXT NOT NULL,
    PRIMARY KEY (team_id, anchor_key)
);

CREATE INDEX IF NOT EXISTS idx_miss_team ON retrieval_misses(team_id, miss_count);

-- Short-lived, single-use: one row per in-flight Azure AD login attempt,
-- created at GET /auth/azure/login and deleted the moment GET
-- /auth/azure/callback consumes it (success or failure - never replayed).
-- The PKCE code_verifier lives here, server-side, rather than in the
-- browser - see oidc.py / config.py.
CREATE TABLE IF NOT EXISTS oidc_states (
    state TEXT PRIMARY KEY,
    code_verifier TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- A session minted by a successful Azure AD sign-in, bound to an existing
-- AgentHive user (see users.azure_oid). Functions exactly like the opaque
-- per-user token from an auth standpoint (see auth.py's user_by_token /
-- this file's session_by_token) but is short-lived on purpose - see
-- config.py's AGENTHIVE_AZURE_SESSION_TTL_SECONDS.
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _age_seconds(stamp: str) -> float:
    """Seconds between an ISO stamp written by now() and the present.

    now() emits a trailing "Z", which fromisoformat only parses from
    3.11 - the replace keeps this working on 3.10 too. Returns 0.0
    rather than raising on anything unparseable: a metric must never be
    the reason a request or a scrape fails."""
    import datetime

    try:
        parsed = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - parsed).total_seconds()


def _token_expiry() -> str | None:
    """None if AGENTHIVE_TOKEN_TTL_SECONDS is unset/0 (tokens never expire
    on their own - see config.py), else an ISO-8601 UTC timestamp
    `token_ttl_seconds` from now. Same sortable string format as now(), so
    expiry is just a string comparison (see user_by_token) - no datetime
    parsing, no timezone-across-backends concerns."""
    from config import CONFIG

    if CONFIG.token_ttl_seconds and CONFIG.token_ttl_seconds > 0:
        return time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + CONFIG.token_ttl_seconds)
        )
    return None


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class SqliteBackend:
    paramstyle = "?"

    def __init__(self, path: str):
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Additive migration for databases created before token_expires_at
            # existed - CREATE TABLE IF NOT EXISTS above is a no-op against
            # them. Safe to run on every startup: SQLite has no "ADD COLUMN
            # IF NOT EXISTS", so the duplicate-column case is just ignored.
            try:
                conn.execute("ALTER TABLE users ADD COLUMN token_expires_at TEXT")
            except sqlite3.OperationalError:
                pass  # already has the column
            try:
                conn.execute("ALTER TABLE users ADD COLUMN azure_oid TEXT")
            except sqlite3.OperationalError:
                pass  # already has the column
            # Only one AgentHive user per Azure AD object id - a partial
            # index (NULLs excluded) rather than a plain UNIQUE column
            # constraint, since most rows never get linked. Created here,
            # after the ALTER TABLE above, rather than inline in SCHEMA:
            # on a pre-existing database SCHEMA's own "CREATE TABLE IF NOT
            # EXISTS users" is a no-op (the table already exists without
            # this column), so an index on azure_oid would fail with "no
            # such column" if it ran before this migration added it.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_azure_oid "
                "ON users(azure_oid) WHERE azure_oid IS NOT NULL"
            )

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
                # Same additive migration as SqliteBackend - Postgres does
                # support IF NOT EXISTS here, so no try/except needed.
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS token_expires_at TEXT")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS azure_oid TEXT")
                # See the matching comment in SqliteBackend.__init__ for
                # why this index is created here rather than inline in
                # SCHEMA.
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_azure_oid "
                    "ON users(azure_oid) WHERE azure_oid IS NOT NULL"
                )

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
        expires_at = _token_expiry()
        self.backend.execute(
            "INSERT INTO users (id, team_id, name, role, token_hash, created_at, created_by, token_expires_at) "
            "VALUES (?, ?, ?, 'admin', ?, ?, 'bootstrap', ?)",
            (user_id, team_id, owner_name, hash_token(token), now(), expires_at),
        )
        team = {"id": team_id, "name": name}
        user = {"id": user_id, "team_id": team_id, "name": owner_name,
                "role": "admin", "token": token, "token_expires_at": expires_at}
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
        expires_at = _token_expiry()
        self.backend.execute(
            "INSERT INTO users (id, team_id, name, role, token_hash, created_at, created_by, token_expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, team_id, name, role, hash_token(token), now(), created_by, expires_at),
        )
        return {"id": user_id, "team_id": team_id, "name": name, "role": role,
                "token": token, "token_expires_at": expires_at}

    def list_users(self, team_id: str):
        rows = self.backend.query_all(
            "SELECT id, team_id, name, role, created_at, created_by, last_used_at, "
            "revoked_at, token_expires_at, azure_oid "
            "FROM users WHERE team_id = ? ORDER BY created_at",
            (team_id,),
        )
        return rows

    def get_user(self, user_id: str):
        row = self.backend.query_one(
            "SELECT id, team_id, name, role, created_at, created_by, last_used_at, "
            "revoked_at, token_expires_at, azure_oid "
            "FROM users WHERE id = ?",
            (user_id,),
        )
        return row

    def user_by_token(self, token: str):
        """Returns the active user matching this token, else None: not
        revoked, AND (no expiry set OR expiry is still in the future - see
        config.py's AGENTHIVE_TOKEN_TTL_SECONDS). An expired token fails
        auth exactly like a revoked one, on purpose - server.py doesn't
        need to know the difference, and not distinguishing the two in the
        response avoids leaking which case applies to an attacker probing
        a stolen token. Updates last_used_at on success. Constant-time-ish
        because we hash first and compare by unique index, not by
        scanning."""
        from auth import hash_token

        token_hash = hash_token(token)
        row = self.backend.query_one(
            "SELECT * FROM users WHERE token_hash = ? AND revoked_at IS NULL",
            (token_hash,),
        )
        if row and row["token_expires_at"] and row["token_expires_at"] <= now():
            return None
        if row:
            self.backend.execute(
                "UPDATE users SET last_used_at = ? WHERE id = ?", (now(), row["id"])
            )
        return row

    def rotate_user_token(self, user_id: str):
        from auth import generate_token, hash_token

        token = generate_token()
        expires_at = _token_expiry()
        self.backend.execute(
            "UPDATE users SET token_hash = ?, revoked_at = NULL, token_expires_at = ? WHERE id = ?",
            (hash_token(token), expires_at, user_id),
        )
        return token, expires_at

    def revoke_user(self, user_id: str):
        self.backend.execute(
            "UPDATE users SET revoked_at = ? WHERE id = ?", (now(), user_id)
        )

    # ---- Azure AD linking + sessions (see oidc.py / ADR.md "Authentication") ----

    def user_by_azure_oid(self, azure_oid: str):
        return self.backend.query_one(
            "SELECT * FROM users WHERE azure_oid = ? AND revoked_at IS NULL",
            (azure_oid,),
        )

    def link_azure_user(self, team_id: str, user_id: str, azure_oid: str):
        """Admin-only, explicit action - see ADR.md's "No auto-provisioning"
        reasoning. Raises ValueError if this Azure AD object id is already
        linked to a different user (the partial unique index on
        users.azure_oid enforces this at the DB level; both backends raise
        their own integrity-error type here, so we catch broadly)."""
        target = self.get_user(user_id)
        if not target or target["team_id"] != team_id:
            raise ValueError("user not found for this team")
        try:
            self.backend.execute(
                "UPDATE users SET azure_oid = ? WHERE id = ?", (azure_oid, user_id)
            )
        except Exception as e:
            raise ValueError(
                "this Azure AD account is already linked to a different user"
            ) from e

    def unlink_azure_user(self, team_id: str, user_id: str):
        target = self.get_user(user_id)
        if not target or target["team_id"] != team_id:
            raise ValueError("user not found for this team")
        self.backend.execute(
            "UPDATE users SET azure_oid = NULL WHERE id = ?", (user_id,)
        )

    def create_oidc_state(self, state: str, code_verifier: str):
        """One row per in-flight Azure AD login attempt - see
        oidc_states in db.py's SCHEMA."""
        self.backend.execute(
            "INSERT INTO oidc_states (state, code_verifier, created_at) VALUES (?, ?, ?)",
            (state, code_verifier, now()),
        )

    def consume_oidc_state(self, state: str, max_age_seconds: int = 600):
        """Single-use: the row is deleted whether or not it was found or
        still fresh, so a state value is never usable twice (replay
        protection) - only the first callback with a given state can
        possibly succeed. Returns the code_verifier, or None if the state
        is unknown or older than max_age_seconds (an abandoned/expired
        login attempt)."""
        row = self.backend.query_one(
            "SELECT * FROM oidc_states WHERE state = ?", (state,)
        )
        if not row:
            return None
        self.backend.execute("DELETE FROM oidc_states WHERE state = ?", (state,))
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - max_age_seconds)
        )
        if row["created_at"] < cutoff:
            return None
        return row["code_verifier"]

    def create_session(self, user_id: str):
        """Mints a session token for a human who just signed in via Azure
        AD - functions exactly like a personal API token from server.py's
        point of view (see session_by_token) but expires automatically
        after CONFIG.oidc_session_ttl_seconds, unlike an agent's token.
        Returns (token, expires_at)."""
        from auth import generate_token, hash_token
        from config import CONFIG

        token = generate_token()
        session_id = new_id()
        expires_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + CONFIG.oidc_session_ttl_seconds),
        )
        self.backend.execute(
            "INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, user_id, hash_token(token), now(), expires_at),
        )
        return token, expires_at

    def session_by_token(self, token: str):
        """Returns the active user dict for a session token, or None - same
        shape/contract as user_by_token, so server.py's _authenticate can
        try both without caring which one matched. An expired session
        fails exactly like a missing one (see user_by_token's docstring
        for the same not-leaking-which-case reasoning)."""
        from auth import hash_token

        token_hash = hash_token(token)
        row = self.backend.query_one(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ? AND u.revoked_at IS NULL",
            (token_hash, now()),
        )
        if row:
            self.backend.execute(
                "UPDATE users SET last_used_at = ? WHERE id = ?", (now(), row["id"])
            )
        return row

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

    def create_auto_reject_rule(self, team_id: str, created_by: str,
                                 match_tag: str = None, match_agent_id: str = None,
                                 reason: str = "") -> dict:
        if not match_tag and not match_agent_id:
            raise ValueError("an auto-reject rule needs match_tag or match_agent_id")
        rule_id = new_id()
        self.backend.execute(
            "INSERT INTO auto_reject_rules "
            "(id, team_id, match_tag, match_agent_id, reason, created_by, created_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (rule_id, team_id, match_tag, match_agent_id, reason or "", created_by, now()),
        )
        return self.get_auto_reject_rule(rule_id)

    def get_auto_reject_rule(self, rule_id: str):
        row = self.backend.query_one("SELECT * FROM auto_reject_rules WHERE id = ?", (rule_id,))
        return dict(row) if row else None

    def list_auto_reject_rules(self, team_id: str, active_only: bool = True):
        sql = "SELECT * FROM auto_reject_rules WHERE team_id = ?"
        if active_only:
            sql += " AND active = 1"
        return [dict(r) for r in self.backend.query_all(sql + " ORDER BY created_at", (team_id,))]

    def deactivate_auto_reject_rule(self, team_id: str, rule_id: str) -> None:
        self.backend.execute(
            "UPDATE auto_reject_rules SET active = 0 WHERE id = ? AND team_id = ?",
            (rule_id, team_id),
        )

    def find_matching_reject_rule(self, team_id: str, tags, agent_id):
        tags = {str(t).strip().lower() for t in (tags or [])}
        for rule in self.list_auto_reject_rules(team_id):
            if rule["match_tag"] and str(rule["match_tag"]).strip().lower() in tags:
                return rule
            if rule["match_agent_id"] and rule["match_agent_id"] == agent_id:
                return rule
        return None

    def find_approved_duplicate(self, team_id: str, title: str, body: str):
        """An approved node with identical title AND body.

        Narrowed in SQL, decided in Python. The first version loaded and
        normalised EVERY approved node on EVERY write, which is an O(n)
        scan with full body hydration on the hot write path - it roughly
        doubled the test suite's runtime before anybody noticed, and on a
        real graph it would have been far worse.

        SQL only filters by normalised title, which it can do exactly:
        governance normalises a title with .strip().lower(), and
        LOWER(TRIM(title)) is the same operation on both backends. The
        authoritative comparison, including the body's whitespace
        collapsing, still happens in Python against the handful of rows
        that survive - so the two cannot drift apart on the part that is
        actually subtle.

        idx_memory_team_title already covers (team_id, title), so this is
        an index lookup rather than the scan it replaced."""
        title_key = (title or "").strip().lower()
        if not title_key:
            return None

        rows = self.backend.query_all(
            "SELECT * FROM memory_nodes "
            "WHERE team_id = ? AND status = 'approved' AND LOWER(TRIM(title)) = ?",
            (team_id, title_key),
        )
        key = governance.normalise_for_duplicate(title, body)
        for row in rows:
            node = _row_to_node(row)
            if governance.normalise_for_duplicate(node["title"], node["body"]) == key:
                return node
        return None

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

        # ---- the automatic decision, in strict precedence order ----
        #
        # 1. A reserved tag outranks everything. security_alert and
        #    sensitive reach a human whichever way automation would have
        #    sent them: auto-approving one hides it, auto-rejecting one
        #    discards it, and both beat a slow queue only in the sense
        #    that nobody notices.
        # 2. Rejection beats approval when both match. That is a config
        #    error either way, and resolving it toward "does not enter
        #    the graph" is the conservative reading.
        # 3. Deterministic checks (duplicate, trivial) before rules, so a
        #    redundant node is rejected for the reason that is actually
        #    true rather than whichever rule happened to catch it.
        #
        # Checked here rather than inside the matchers so a rule that
        # matched on agent_id is caught too - a tag-only check inside
        # find_matching_rule would have waved that through.
        from config import CONFIG

        self.last_auto_approve_blocked = False
        self.last_auto_reject_reason = None
        human_only = governance.needs_human(tags)

        reject_reason = None
        if not human_only:
            if CONFIG.auto_reject_duplicates:
                duplicate = self.find_approved_duplicate(team_id, title, body)
                if duplicate is not None:
                    reject_reason = f"auto: identical to approved node {duplicate['id']}"
            if reject_reason is None and governance.trivial_body(
                    body, CONFIG.auto_reject_body_under):
                reject_reason = (
                    f"auto: body under {CONFIG.auto_reject_body_under} characters")
            if reject_reason is None:
                reject_rule = self.find_matching_reject_rule(team_id, tags, agent_id)
                if reject_rule is not None:
                    detail = reject_rule["reason"] or "matched an auto-reject rule"
                    reject_reason = f"auto-reject:{reject_rule['id']} {detail}"

        rule = None
        if reject_reason is None:
            rule = self.find_matching_rule(team_id, tags, agent_id)
            if rule is not None and human_only:
                rule = None
                self.last_auto_approve_blocked = True

        if reject_reason is not None:
            self.last_auto_reject_reason = reject_reason
            status = "rejected"
            reviewed_at = now()
            reviewed_by = "auto"
        else:
            status = "approved" if rule else "pending"
            reviewed_at = now() if rule else None
            reviewed_by = f"auto-approve:{rule['id']}" if rule else None

        self.backend.execute(
            "INSERT INTO memory_nodes "
            "(id, team_id, agent_id, task_id, tier, title, body, tags, links, status, "
            " created_at, reviewed_at, reviewed_by, auto_approved, rule_id, reject_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (node_id, team_id, agent_id, task_id, tier, title, body,
             json.dumps(tags), json.dumps(links_list), status,
             now(), reviewed_at, reviewed_by,
             1 if rule else 0, rule["id"] if rule else None,
             reject_reason or ""),
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
        node = self.get_memory(node_id)
        if node:
            node["review_latency_seconds"] = _age_seconds(node.get("created_at"))
        return node

    def list_rejected(self, team_id: str, limit: int = 200):
        """Rejected nodes, newest first, with the reason.

        Without this, "an auto-rejected node is recoverable, an admin can
        still approve it" is a claim with no way to act on it - the node
        is invisible to retrieval and absent from the pending queue, so
        nothing in the product can show it to anyone. Recoverability you
        cannot reach is deletion with extra steps."""
        rows = self.backend.query_all(
            "SELECT * FROM memory_nodes WHERE team_id = ? AND status = 'rejected' "
            "ORDER BY reviewed_at DESC LIMIT ?",
            (team_id, limit),
        )
        return [_row_to_node(r) for r in rows]

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

    def record_retrieval_miss(self, team_id: str, anchor: str, user_id: str) -> None:
        """Count one empty retrieve_context for this anchor. Repeated
        misses of the same phrase (ignoring case and surrounding space)
        increment one row so the review UI can show what to title next."""
        from retrieval import slug

        key = slug(anchor)[:200]
        display = anchor.strip()[:200]
        if not key:
            return
        self.backend.execute(
            "INSERT INTO retrieval_misses "
            "(team_id, anchor_key, anchor, miss_count, last_user_id, last_missed_at) "
            "VALUES (?, ?, ?, 1, ?, ?) "
            "ON CONFLICT (team_id, anchor_key) DO UPDATE SET "
            "miss_count = retrieval_misses.miss_count + 1, "
            "anchor = excluded.anchor, "
            "last_user_id = excluded.last_user_id, "
            "last_missed_at = excluded.last_missed_at",
            (team_id, key, display, user_id, now()),
        )

    # ---- anchor aliases ----

    def create_alias(self, team_id: str, anchor: str, node_id: str,
                      created_by: str) -> dict:
        """Map a phrasing to a node. Upsert, because re-aliasing is a
        correction rather than a second opinion."""
        from retrieval import slug

        key = slug(anchor)[:200]
        if not key:
            raise ValueError("anchor must contain something to match on")

        node = self.get_memory(node_id)
        if not node or node["team_id"] != team_id:
            # Same shape as every other cross-team read here: an id from
            # elsewhere is simply not found, with no way to tell that
            # apart from a typo.
            raise ValueError("node not found in this team")
        if node["status"] != "approved":
            raise ValueError(
                "can only alias to an approved node - a pending one is "
                "invisible to retrieval, so the alias would resolve to nothing")

        self.backend.execute(
            "INSERT INTO anchor_aliases "
            "(team_id, anchor_key, anchor, node_id, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (team_id, anchor_key) DO UPDATE SET "
            "node_id = excluded.node_id, anchor = excluded.anchor, "
            "created_by = excluded.created_by, created_at = excluded.created_at",
            (team_id, key, anchor.strip()[:200], node_id, created_by, now()),
        )
        return self.get_alias(team_id, key)

    def get_alias(self, team_id: str, anchor_key: str):
        row = self.backend.query_one(
            "SELECT * FROM anchor_aliases WHERE team_id = ? AND anchor_key = ?",
            (team_id, anchor_key),
        )
        return dict(row) if row else None

    def list_aliases(self, team_id: str):
        """Aliases with the title each one points at, so the review UI can
        show what it means rather than an opaque id."""
        rows = self.backend.query_all(
            "SELECT a.*, n.title as node_title, n.status as node_status "
            "FROM anchor_aliases a JOIN memory_nodes n ON n.id = a.node_id "
            "WHERE a.team_id = ? ORDER BY a.created_at DESC",
            (team_id,),
        )
        return [dict(r) for r in rows]

    def alias_map(self, team_id: str) -> dict:
        """{anchor_key: node_id} for the retrieval path. One small query
        per uncached retrieval; the table has one row per phrasing a human
        has bothered to record, so it stays small by construction."""
        rows = self.backend.query_all(
            "SELECT anchor_key, node_id FROM anchor_aliases WHERE team_id = ?",
            (team_id,),
        )
        return {r["anchor_key"]: r["node_id"] for r in rows}

    def delete_alias(self, team_id: str, anchor_key: str) -> None:
        self.backend.execute(
            "DELETE FROM anchor_aliases WHERE team_id = ? AND anchor_key = ?",
            (team_id, anchor_key),
        )

    def global_alias_count(self) -> int:
        row = self.backend.query_one("SELECT COUNT(*) as n FROM anchor_aliases")
        return row["n"] if row else 0

    def close_retrieval_miss(self, team_id: str, anchor: str) -> int:
        """A previously-missing anchor just returned something. Clear the
        gap and report how many misses it had accumulated.

        Without this, retrieval_misses only ever grows: an anchor stays
        on the gap list after somebody fills it, report_gaps keeps
        telling agents to write a note that now exists, and the headline
        "is this loop working" question has no answer because closure is
        never observed.

        A SELECT before the DELETE on purpose. This runs on every
        successful retrieval, and the overwhelmingly common case is "no
        miss row", which should cost one indexed PK read and no write at
        all rather than a lock on the hot read path."""
        from retrieval import slug

        key = slug(anchor)[:200]
        if not key:
            return 0
        row = self.backend.query_one(
            "SELECT miss_count FROM retrieval_misses WHERE team_id = ? AND anchor_key = ?",
            (team_id, key),
        )
        if not row:
            return 0
        self.backend.execute(
            "DELETE FROM retrieval_misses WHERE team_id = ? AND anchor_key = ?",
            (team_id, key),
        )
        return row["miss_count"] or 0

    # ---- signals for the memory-processing layer (see insights.py) ----
    #
    # All read-only, all plain SQL that runs identically on SQLite and
    # Postgres. The analysis itself lives in insights.py as pure
    # functions over these rows, so it can be tested without a database.

    def list_retrieval_misses(self, team_id: str, min_count: int = 1, limit: int = 50):
        """Anchors that returned nothing, most-missed first.

        Already aggregated per (team, anchor_key) by
        record_retrieval_miss, so this is the gap signal in its final
        form - no scan of a per-call log."""
        return self.backend.query_all(
            "SELECT anchor, anchor_key, miss_count, last_missed_at "
            "FROM retrieval_misses WHERE team_id = ? AND miss_count >= ? "
            "ORDER BY miss_count DESC, last_missed_at DESC LIMIT ?",
            (team_id, min_count, limit),
        )

    def co_access_pairs(self, team_id: str, min_shared: int = 2, limit: int = 100):
        """Pairs of nodes returned by the SAME retrieval, N times or more.

        record_access writes one row per node under a shared
        retrieval_id precisely so this self-join is possible. The
        `a.node_id < b.node_id` predicate both dedupes the pair and
        drops the self-match, so each unordered pair appears once."""
        return self.backend.query_all(
            "SELECT a.node_id as a_id, b.node_id as b_id, "
            "       COUNT(DISTINCT a.retrieval_id) as shared "
            "FROM memory_access_log a "
            "JOIN memory_access_log b "
            "  ON a.retrieval_id = b.retrieval_id AND a.node_id < b.node_id "
            "WHERE a.team_id = ? AND b.team_id = ? "
            "GROUP BY a.node_id, b.node_id "
            "HAVING COUNT(DISTINCT a.retrieval_id) >= ? "
            "ORDER BY shared DESC LIMIT ?",
            (team_id, team_id, min_shared, limit),
        )

    def node_access_stats(self, team_id: str):
        """Per approved node: how often, by how many people, how recently.

        LEFT JOIN on purpose - a node nobody has ever retrieved is the
        most interesting row here, and an INNER JOIN would hide exactly
        those. Every non-aggregated column is in GROUP BY so this is
        valid on Postgres as well as SQLite."""
        return self.backend.query_all(
            "SELECT n.id, n.title, n.tier, n.created_at, "
            "       COUNT(DISTINCT a.retrieval_id) as access_count, "
            "       COUNT(DISTINCT a.user_id) as distinct_users, "
            "       MAX(a.accessed_at) as last_accessed_at "
            "FROM memory_nodes n "
            "LEFT JOIN memory_access_log a ON a.node_id = n.id "
            "WHERE n.team_id = ? AND n.status = 'approved' "
            "GROUP BY n.id, n.title, n.tier, n.created_at",
            (team_id,),
        )

    def review_decisions(self, team_id: str):
        """Every node a human (or a rule) has already ruled on.

        This is the labelled data the approval gate produces for free and
        nothing currently reads. auto_approved is carried so rule mining
        can exclude decisions a rule already made - counting those as
        evidence for the rule that made them is circular."""
        rows = self.backend.query_all(
            "SELECT id, agent_id, tags, status, auto_approved, rule_id "
            "FROM memory_nodes WHERE team_id = ? AND status IN ('approved', 'rejected')",
            (team_id,),
        )
        out = []
        for row in rows:
            out.append({
                "id": row["id"],
                "agent_id": row["agent_id"],
                "tags": json.loads(row["tags"]) if row["tags"] else [],
                "status": row["status"],
                "auto_approved": bool(row["auto_approved"]),
                "rule_id": row["rule_id"],
            })
        return out

    def set_node_tier(self, team_id: str, node_id: str, tier: str) -> None:
        """Tier is metadata about how a node is USED, never its content.
        Scoped by team_id as well as id so a stray id cannot reach
        across a team boundary."""
        self.backend.execute(
            "UPDATE memory_nodes SET tier = ? WHERE id = ? AND team_id = ?",
            (tier, node_id, team_id),
        )

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

        miss_row = self.backend.query_one(
            "SELECT COALESCE(SUM(miss_count), 0) as n FROM retrieval_misses WHERE team_id = ?",
            (team_id,),
        )
        retrieval_misses = miss_row["n"] if miss_row else 0

        miss_rows = self.backend.query_all(
            "SELECT anchor, miss_count, last_missed_at FROM retrieval_misses "
            "WHERE team_id = ? ORDER BY miss_count DESC, last_missed_at DESC LIMIT 10",
            (team_id,),
        )
        from retrieval import pending_title_match

        pending = self.list_pending(team_id)
        top_missed = []
        for row in miss_rows:
            match = pending_title_match(pending, row["anchor"])
            top_missed.append({
                "anchor": row["anchor"],
                "miss_count": row["miss_count"],
                "last_missed_at": row["last_missed_at"],
                "pending_id": match["id"] if match else None,
                "pending_title": match["title"] if match else None,
            })

        return {
            "node_counts": node_counts,
            "total_retrievals": total_retrievals,
            "retrieval_hits": total_retrievals,
            "retrieval_misses": retrieval_misses,
            "active_users": active_users,
            "active_auto_approve_rules": rule_count_row["n"] if rule_count_row else 0,
            "top_reused_nodes": top_nodes,
            "top_missed_anchors": top_missed,
        }

    # ---- global (cross-team) stats for the /metrics gauges ----
    # No team_id in the Prometheus labels (see metrics.py) - these three
    # are the only cross-team numbers exposed there.

    def global_node_status_counts(self) -> dict:
        rows = self.backend.query_all(
            "SELECT status, COUNT(*) as n FROM memory_nodes GROUP BY status"
        )
        return {r["status"]: r["n"] for r in rows}

    def global_oldest_pending_age_seconds(self) -> float:
        """Age of the longest-waiting unreviewed node, across all teams.

        The single most important operational number in this system and
        it has never been exposed. Pending memory is INVISIBLE to
        retrieval, so a backed-up queue degrades every agent's context
        silently - no error, no failed request, just worse answers. This
        is what you alert on."""
        row = self.backend.query_one(
            "SELECT MIN(created_at) as oldest FROM memory_nodes WHERE status = 'pending'"
        )
        if not row or not row["oldest"]:
            return 0.0
        return max(_age_seconds(row["oldest"]), 0.0)

    def classify_token_failure(self, token: str) -> str:
        """Why a token was rejected: revoked | expired | unknown.

        Called ONLY on the failure path, so the extra query costs nothing
        on a healthy request. The distinction deliberately never reaches
        the HTTP response - user_by_token keeps returning a single opaque
        failure so an attacker probing a stolen token learns nothing (see
        its docstring). A metric is a different audience: it is on the
        metrics port, scraped by Prometheus, and a spike in `expired`
        versus `revoked` is the difference between a TTL rollout biting
        and someone using a token you killed."""
        from auth import hash_token

        row = self.backend.query_one(
            "SELECT revoked_at, token_expires_at FROM users WHERE token_hash = ?",
            (hash_token(token),),
        )
        if not row:
            return "unknown"
        if row["revoked_at"]:
            return "revoked"
        if row["token_expires_at"] and row["token_expires_at"] <= now():
            return "expired"
        return "unknown"

    def global_tier_counts(self) -> dict:
        """Approved nodes by tier. The slope of this is what "the graph
        is stratifying by use" looks like; a flat all-L1 line means the
        tier proposals are being generated and ignored."""
        rows = self.backend.query_all(
            "SELECT tier, COUNT(*) as n FROM memory_nodes "
            "WHERE status = 'approved' GROUP BY tier"
        )
        return {r["tier"] or "L1": r["n"] for r in rows}

    def global_open_gap_count(self) -> int:
        """Distinct anchors currently missing, across all teams."""
        row = self.backend.query_one("SELECT COUNT(*) as n FROM retrieval_misses")
        return row["n"] if row else 0

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
