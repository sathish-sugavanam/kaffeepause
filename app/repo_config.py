"""The repository contract, `.kaffeepause.yml` (PRD section 6), read from the base branch.

This is the subset the agent pipeline needs. `environment` and `services` are
accepted but not used yet: they need the injected runtime (build step 3), so
today the run container is still the Kaffeepause image.
"""

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import yaml

from app.errors import ErrorCode, RunFailed

FILENAME = ".kaffeepause.yml"

DEFAULT_TESTER_OWNED = (
    "**/test/**",
    "**/tests/**",
    "**/__tests__/**",
    "**/*_test.*",
    "**/*.test.*",
    "**/*_spec.*",
    "**/test_*.*",
)
# Always protected, whatever the repo says.
ALWAYS_PROTECTED = (FILENAME, ".github/**", ".claude/**")
DEFAULT_TIMEOUT_SECONDS = 15 * 60

KNOWN_KEYS = {
    "version",
    "stack",
    "environment",
    "services",
    "setup",
    "test",
    "junit",
    "lint",
    "tester_owned",
    "protected",
    "base_branch",
    "timeout_seconds",
}


@dataclass(frozen=True)
class RepoConfig:
    test: dict[str, str]  # layer name -> command
    junit: str
    stack: str = ""
    setup: tuple[str, ...] = ()
    lint: tuple[str, ...] = ()
    tester_owned: tuple[str, ...] = DEFAULT_TESTER_OWNED
    protected: tuple[str, ...] = ALWAYS_PROTECTED
    base_branch: str = "main"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    unused: tuple[str, ...] = field(default=())  # keys accepted but not acted on yet


def invalid(name: str, problem: str) -> RunFailed:
    return RunFailed(ErrorCode.CONFIG_INVALID, f"{FILENAME}: `{name}` {problem}")


def _str_list(raw: dict[str, Any], name: str) -> tuple[str, ...]:
    value = raw.get(name, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v.strip() for v in value
    ):
        raise invalid(name, "must be a list of non-empty strings")
    return tuple(value)


def parse_repo_config(text: str | None) -> RepoConfig:
    if text is None:
        raise RunFailed(
            ErrorCode.CONFIG_INVALID, f"{FILENAME} is missing on the base branch"
        )
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RunFailed(
            ErrorCode.CONFIG_INVALID, f"{FILENAME} is not valid YAML: {exc}"
        ) from None
    if not isinstance(raw, dict):
        raise RunFailed(ErrorCode.CONFIG_INVALID, f"{FILENAME} must be a mapping")

    if unknown := sorted(set(raw) - KNOWN_KEYS):
        raise invalid(unknown[0], "is not a known setting")
    if raw.get("version") != 1:
        raise invalid("version", "must be 1")

    test = raw.get("test")
    if isinstance(test, str):
        test = {"tests": test}
    if (
        not isinstance(test, dict)
        or not test
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in test.items())
    ):
        raise invalid("test", "must be a command, or a map of layer name to command")

    junit = raw.get("junit")
    if not isinstance(junit, str) or not junit.strip():
        raise invalid(
            "junit",
            "is required: the red check needs JUnit XML to tell a failed "
            "assertion from a crash",
        )
    if "$KP_JUNIT" not in junit:
        raise invalid("junit", "must point inside $KP_JUNIT")

    for name in ("stack", "base_branch"):
        if name in raw and not isinstance(raw[name], str):
            raise invalid(name, "must be a string")
    timeout = raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if (
        not isinstance(timeout, int | float)
        or isinstance(timeout, bool)
        or timeout <= 0
    ):
        raise invalid("timeout_seconds", "must be a positive number")

    tester_owned = _str_list(raw, "tester_owned") or DEFAULT_TESTER_OWNED
    protected = _str_list(raw, "protected")
    return RepoConfig(
        test=dict(test),
        junit=junit,
        stack=raw.get("stack", ""),
        setup=_str_list(raw, "setup"),
        lint=_str_list(raw, "lint"),
        tester_owned=tester_owned,
        protected=tuple(dict.fromkeys(ALWAYS_PROTECTED + protected)),
        base_branch=raw.get("base_branch", "main"),
        timeout_seconds=float(timeout),
        unused=tuple(k for k in ("environment", "services") if k in raw),
    )


def matches_any(path: str, globs: tuple[str, ...] | list[str]) -> bool:
    """Glob match on a repo-relative POSIX path, where `**` spans directories."""
    p = PurePosixPath(path)
    return any(p.full_match(g) for g in globs)
