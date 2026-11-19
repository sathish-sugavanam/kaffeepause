"""PreToolUse rules: where an agent may write, and which commands it may run.

The decision functions are pure, so they are tested without a model. The SDK
hook wrappers only translate their answer into a permission decision.

These hooks run inside the run container, which the agent can tamper with
through Bash. They keep honest mistakes out; the worker's own diff checks on
the bundle are what actually enforce scope.
"""

import os
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.repo_config import matches_any

WRITE_TOOLS = "Write|Edit|MultiEdit|NotebookEdit"
# Chaining, substitution and redirection would let an allowlisted prefix run anything.
_SHELL_META = (";", "&", "|", "`", "$(", ">", "<", "\n", "\r")
READ_ONLY_GIT = ("git status", "git diff", "git log", "git show")
SCANNERS = ("semgrep", "gitleaks", "osv-scanner")


@dataclass(frozen=True)
class Scope:
    repo_dir: str
    write_allow: tuple[str, ...] | None  # None: anything not denied
    write_deny: tuple[str, ...]
    bash_prefixes: tuple[str, ...] | None  # None: any command

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Scope:
        allow = raw.get("write_allow")
        prefixes = raw.get("bash_prefixes")
        return cls(
            repo_dir=raw["repo_dir"],
            write_allow=None if allow is None else tuple(allow),
            write_deny=tuple(raw.get("write_deny", ())),
            bash_prefixes=None if prefixes is None else tuple(prefixes),
        )


def write_denial(scope: Scope, file_path: str) -> str | None:
    """None if the write is allowed, else the reason it is denied."""
    if not file_path:
        return "no file path given"
    repo = os.path.realpath(scope.repo_dir)
    full = os.path.join(repo, file_path) if not os.path.isabs(file_path) else file_path
    # realpath follows symlinks, so a link inside tests/ can't reach source files.
    target = os.path.realpath(full)
    if target != repo and not target.startswith(repo + os.sep):
        return f"{file_path} is outside the repository"
    rel = os.path.relpath(target, repo).replace(os.sep, "/")
    if rel == ".git" or rel.startswith(".git/"):
        return "the .git directory is off limits"
    if matches_any(rel, scope.write_deny):
        return f"{rel} is outside your write scope"
    if scope.write_allow is not None and not matches_any(rel, scope.write_allow):
        return f"{rel} is not a test file; you may only write test files"
    return None


def bash_denial(scope: Scope, command: str) -> str | None:
    if scope.bash_prefixes is None:
        return None
    if any(m in command for m in _SHELL_META):
        return "chaining, pipes, redirection and command substitution are not allowed"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "the command could not be parsed"
    for prefix in scope.bash_prefixes:
        want = shlex.split(prefix)
        if tokens[: len(want)] == want:
            return None
    allowed = ", ".join(f"`{p}`" for p in scope.bash_prefixes)
    return f"only these commands are allowed: {allowed}"


def command_prefix(command: str) -> str:
    """`uv run pytest -m unit --junitxml=$KP_JUNIT/u.xml` -> `uv run pytest`.

    The tester may then run that prefix with any arguments, e.g. a single test file.

    `python -m pytest ...` keeps `-m pytest`, so the prefix is never a bare interpreter.
    """
    tokens = shlex.split(command)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if (
            token == "-m"
            and i + 1 < len(tokens)
            and out
            and os.path.basename(out[-1]).startswith("python")
        ):
            out += tokens[i : i + 2]
            i += 2
            continue
        # Stop at flags, variables and shell operators, and (after the program
        # itself, which may be ./gradlew or python3.14) at path-like arguments.
        stop = "$=<>|&;" + ("/." if out else "")
        if token.startswith("-") or any(c in token for c in stop):
            break
        out.append(token)
        i += 1
    return shlex.join(out)


Hook = Callable[[Any, str | None, Any], Awaitable[dict[str, Any]]]


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def write_hook(scope: Scope) -> Hook:
    async def hook(hook_input: Any, _tool_use_id: str | None, _ctx: Any) -> dict:
        args = hook_input.get("tool_input") or {}
        path = args.get("file_path") or args.get("notebook_path") or ""
        reason = write_denial(scope, path)
        return _deny(reason) if reason else {}

    return hook


def bash_hook(scope: Scope) -> Hook:
    async def hook(hook_input: Any, _tool_use_id: str | None, _ctx: Any) -> dict:
        command = (hook_input.get("tool_input") or {}).get("command", "")
        reason = bash_denial(scope, command)
        return _deny(reason) if reason else {}

    return hook
