"""
mcp_server.py - a thin MCP (Model Context Protocol) stdio server wrapping
client.py, so Cursor and Claude Code can discover retrieve_context and
log_session as native tools instead of needing a CLAUDE.md / .cursor/rules
instruction block that calls the HTTP API by hand (see ONBOARDING.md's
"Point your coding agent at it" step - this is the same two calls, just
exposed as MCP tools rather than documented prose).

Deliberately thin: this process holds no state of its own and makes no
decisions - every call is a pass-through to TeamMemoryClient, so the
member/admin permission split, the approval gate, and auth all still live
entirely server-side (see ADR.md's "Authentication" section). A user who
can run this server can do exactly what their AGENTHIVE_TOKEN already
lets them do over the HTTP API directly - nothing more.

Runs over stdio, the standard transport for a per-user local MCP server
that Cursor / Claude Code spawns as a subprocess (see README.md's "Hooking
up Cursor / Claude Code via MCP" for the client config). Reads its target
team/token/url from the environment rather than a config file, so the
same three AGENTHIVE_URL / AGENTHIVE_TOKEN / AGENTHIVE_TEAM_ID values used
for the plain client.py path work here unchanged.

Requires the optional `mcp` package (pip install -r requirements-mcp.txt)
- not a dependency of the server itself, only of this local wrapper, so a
normal AgentHive deployment needs nothing extra.
"""
import os
import sys

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print(
        "mcp_server.py needs the `mcp` package: pip install -r requirements-mcp.txt",
        file=sys.stderr,
    )
    raise

from client import TeamMemoryClient

mcp = FastMCP("agenthive")


def _client() -> TeamMemoryClient:
    missing = [
        name for name in ("AGENTHIVE_URL", "AGENTHIVE_TOKEN", "AGENTHIVE_TEAM_ID")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(
            "mcp_server.py is missing " + ", ".join(missing) + " - set these in "
            "the MCP client config's env block (see README.md's MCP section)."
        )
    return TeamMemoryClient(
        os.environ["AGENTHIVE_URL"],
        os.environ["AGENTHIVE_TOKEN"],
        os.environ["AGENTHIVE_TEAM_ID"],
    )


@mcp.tool()
def retrieve_context(anchor: str, hops: int = 2, hub_cutoff: int = 15) -> dict:
    """Retrieve a bounded neighborhood of this team's reviewed, approved
    memory around a topic. Call this once at the start of a session,
    before starting work, so you inherit what teammates already learned
    instead of rediscovering it. `anchor` is the topic/title to search
    around (e.g. "Postgres connection pool exhaustion"); `hops` bounds how
    far the traversal spreads from it (default 2); `hub_cutoff` stops
    traversal through overly-connected "hub" nodes so one popular node
    doesn't pull in the whole graph. Returns a `neighborhood` list of
    memory nodes plus `approx_tokens`, what folding them into context
    would actually cost."""
    return _client().retrieve_context(anchor, hops=hops, hub_cutoff=hub_cutoff)


@mcp.tool()
def log_session(
    title: str,
    body: str,
    tags: list[str] | None = None,
    links: list[str] | None = None,
) -> dict:
    """Log what happened this session as a memory node for the team to
    reuse. Call this once at the end of a session that learned something
    worth keeping - a root cause, a fix, a decision, a gotcha. The node
    starts PENDING and is invisible to retrieve_context until an admin
    approves it (or it matches an auto-approve rule) - this is a
    deliberate review gate, not a bug. `title` is a short, searchable
    summary (this is what future retrieve_context anchors match against);
    `body` is the actual content; `tags` and `links` are optional and
    help retrieval and review."""
    return _client().log_session(title=title, body=body, tags=tags or [], links=links or [])


if __name__ == "__main__":
    mcp.run()
