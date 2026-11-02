"""
A fresh AgentHive team per (repository, arm).

Isolation is the whole experiment. If arms share a team, the held-out
arm's sessions still write memory that the served arm then reads, and
the comparison measures nothing. If repositories share a team, memory
from django leaks into astropy and the per-repository numbers are
meaningless.

So: one team per pair, created at the start of the run, never reused.
"""

import sys
import urllib.error
import urllib.request


def _client_module(repo_root: str):
    sys.path.insert(0, repo_root)
    import client  # noqa: PLC0415 - deliberately late, after sys.path
    return client


class Arm:
    """One arm's handle on AgentHive, or a no-op when memory is off."""

    def __init__(self, client_mod, base_url, team, user, enabled: bool):
        self._mod = client_mod
        self.base_url = base_url
        self.team_id = team["id"]
        self.token = user["token"]
        self.enabled = enabled

    def session(self, session_id: str):
        return self._mod.TeamMemoryClient(
            self.base_url, self.token, self.team_id, session_id=session_id)

    def env(self, session_id: str) -> dict:
        """What an agent needs to reach this team through agenthive-mcp.

        The held-out arm gets no credentials at all rather than a
        disabled flag: the cleanest way to run without memory is for the
        tools to be absent, not present and refusing."""
        # Credentials only if there are any. `enabled` says whether this
        # arm gets memory at all; the token says whether there is a
        # server to reach. A dry run is the first case without the
        # second, and it still has to exercise the prompt difference or
        # it is not testing the thing it exists to test.
        if not self.enabled or not self.token:
            return {}
        return {
            "AGENTHIVE_URL": self.base_url,
            "AGENTHIVE_TOKEN": self.token,
            "AGENTHIVE_TEAM_ID": self.team_id,
        }


def ping(base_url: str, timeout: int = 5) -> None:
    try:
        urllib.request.urlopen(base_url.rstrip("/") + "/healthz", timeout=timeout).close()
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(
            f"no AgentHive at {base_url} ({e}). Start one with "
            "`python3 server.py --port 8790`, or pass --no-hive to run the "
            "held-out arm only.") from None


def make_arms(base_url: str, repo_root: str, repo: str, arm_names) -> dict:
    """One team per (repo, arm). Named so a half-finished run leaves
    something a human can find and delete."""
    client_mod = _client_module(repo_root)
    out = {}
    for name in arm_names:
        if name == "held_out":
            out[name] = Arm(client_mod, base_url, {"id": ""}, {"token": ""}, enabled=False)
            continue
        data = client_mod.create_team(base_url, f"bench:{repo}:{name}")
        out[name] = Arm(client_mod, base_url, data["team"], data["user"], enabled=True)
    return out
