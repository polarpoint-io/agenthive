"""
Genuine end-to-end test of mcp_server.py: a real live server.py subprocess
(the live_server fixture), a real mcp_server.py subprocess talking to it
over actual MCP stdio, and a real mcp.ClientSession driving that subprocess
- nothing mocked at the protocol, process, or HTTP layer, same philosophy
as the rest of this suite (see conftest.py's docstring).

Skipped automatically if the optional `mcp` package isn't installed (see
requirements-mcp.txt) - it is not a dependency of the server itself.
"""
import json
import os
import sys

import anyio
import pytest

mcp_client = pytest.importorskip("mcp")
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _result_json(result):
    """FastMCP's dict-returning tools don't populate structuredContent
    without an explicit output schema - the reliable cross-version way to
    get the actual payload back is the text content block it always sends."""
    text = "".join(getattr(block, "text", "") for block in result.content)
    return json.loads(text)


def _server_params(base_url: str, token: str, team_id: str) -> StdioServerParameters:
    env = dict(os.environ)
    env["AGENTHIVE_URL"] = base_url
    env["AGENTHIVE_TOKEN"] = token
    env["AGENTHIVE_TEAM_ID"] = team_id
    return StdioServerParameters(
        command=sys.executable, args=["mcp_server.py"], cwd=ROOT, env=env,
    )


def test_tools_are_discoverable_and_log_then_retrieve_round_trips(live_server, client_lib):
    """The actual point of this server: an agent that only speaks MCP can
    log a memory node and, once it's approved, retrieve it back - without
    ever touching the HTTP API or client.py directly."""
    data = client_lib.create_team(live_server, "MCP Team")
    team, admin = data["team"], data["user"]
    admin_c = client_lib.TeamMemoryClient(live_server, admin["token"], team["id"])

    async def run():
        params = _server_params(live_server, admin["token"], team["id"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert {"retrieve_context", "log_session"} <= names

                write_result = await session.call_tool(
                    "log_session",
                    {
                        "title": "MCP round trip",
                        "body": "Written via the MCP tool, not the HTTP API directly.",
                        "tags": ["mcp-test"],
                    },
                )
                assert not write_result.isError, write_result.content
                return _result_json(write_result)

    written = anyio.run(run)
    # Starts pending, same as any other write (see ADR.md's approval gate) -
    # approve it out-of-band via the plain client, same admin token.
    admin_c.approve(written["id"])

    async def retrieve():
        params = _server_params(live_server, admin["token"], team["id"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "retrieve_context", {"anchor": "MCP round trip", "hops": 2},
                )
                assert not result.isError, result.content
                return _result_json(result)

    retrieved = anyio.run(retrieve)
    titles = [n["title"] for n in retrieved["neighborhood"]]
    assert "MCP round trip" in titles


def test_missing_token_gives_a_clear_error_not_a_crash(live_server):
    """No AGENTHIVE_TOKEN set - the tool call should come back as an MCP
    tool error with a message pointing at what's missing, not a stack
    trace or a silent hang."""
    env = dict(os.environ)
    env["AGENTHIVE_URL"] = live_server
    env.pop("AGENTHIVE_TOKEN", None)
    env.pop("AGENTHIVE_TEAM_ID", None)
    params = StdioServerParameters(
        command=sys.executable, args=["mcp_server.py"], cwd=ROOT, env=env,
    )

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("retrieve_context", {"anchor": "anything"})

    result = anyio.run(run)
    assert result.isError
    text = " ".join(getattr(block, "text", "") for block in result.content)
    assert "AGENTHIVE_TOKEN" in text
