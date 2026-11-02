"""
Driving a real coding agent, headlessly.

Each adapter does three things: build the command, run it, and read the
token usage back out. The usage shapes differ per agent and are the
fiddly part - an adapter that silently reports zero usage would make its
arm look free, so a missing usage block is an ERROR here, not a default.

The stub adapter is not a mock for tests to lean on. It exists so the
whole harness can be exercised end to end without spending money, which
is the only way to find the orchestration bugs before a paid run hits
them at task forty.
"""

import json
import os
import shutil
import subprocess


class AgentError(RuntimeError):
    pass


class Agent:
    name = "abstract"

    def available(self) -> bool:
        raise NotImplementedError

    def run(self, prompt: str, cwd: str, env: dict, timeout: int) -> dict:
        """Returns {"text": str, "usage": dict, "turns": int|None}."""
        raise NotImplementedError


class ClaudeCode(Agent):
    name = "claude"

    def available(self) -> bool:
        return shutil.which("claude") is not None

    def run(self, prompt, cwd, env, timeout):
        done = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            cwd=cwd, env={**os.environ, **env}, capture_output=True,
            text=True, timeout=timeout, check=False,
        )
        if done.returncode != 0 and not done.stdout.strip():
            raise AgentError(f"claude exited {done.returncode}: {done.stderr[-800:]}")
        try:
            payload = json.loads(done.stdout)
        except json.JSONDecodeError:
            raise AgentError(
                "claude did not return JSON. The --output-format contract has "
                f"changed or the run was interrupted: {done.stdout[-400:]}") from None

        usage = payload.get("usage") or {}
        if not usage:
            # Refusing beats defaulting: an arm whose usage silently
            # reads zero looks free, and that is the one error that
            # would invert a result rather than spoil it.
            raise AgentError("claude returned no usage block; cost cannot be measured")
        return {"text": payload.get("result", ""), "usage": usage,
                "turns": payload.get("num_turns")}


class Codex(Agent):
    name = "codex"

    def available(self) -> bool:
        return shutil.which("codex") is not None

    def run(self, prompt, cwd, env, timeout):
        done = subprocess.run(
            ["codex", "exec", "--json", prompt],
            cwd=cwd, env={**os.environ, **env}, capture_output=True,
            text=True, timeout=timeout, check=False,
        )
        usage, text, turns = {}, "", 0
        for line in done.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("usage"):
                usage = event["usage"]
            if event.get("type") in ("message", "assistant"):
                turns += 1
                text = event.get("text") or text
        if not usage:
            raise AgentError("codex reported no usage; cost cannot be measured")
        return {"text": text, "usage": usage, "turns": turns or None}


class Stub(Agent):
    """Runs nothing. Lets `--dry-run` exercise the orchestration - task
    ordering, per-repo teams, arm isolation, result shape - with no
    model calls and no money."""

    name = "stub"

    def available(self) -> bool:
        return True

    def run(self, prompt, cwd, env, timeout):
        size = len(prompt)
        return {
            "text": f"[stub] would have worked on {size} characters of prompt",
            "usage": {
                "input_tokens": size // 4,
                "cache_read_input_tokens": size // 2,
                "cache_creation_input_tokens": size // 8,
                "output_tokens": 128,
            },
            "turns": 3,
        }


REGISTRY = {a.name: a for a in (ClaudeCode, Codex, Stub)}


def get(name: str) -> Agent:
    if name not in REGISTRY:
        raise AgentError(f"unknown agent {name!r}; have {', '.join(sorted(REGISTRY))}")
    agent = REGISTRY[name]()
    if not agent.available():
        raise AgentError(
            f"{name} is not on PATH. Install it, or use --agent stub to "
            "exercise the harness without it.")
    return agent
