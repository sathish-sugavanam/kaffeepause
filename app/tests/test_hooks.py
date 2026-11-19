import asyncio
import os

import pytest

from app.agents.hooks import (
    Scope,
    bash_denial,
    bash_hook,
    command_prefix,
    write_denial,
    write_hook,
)

TESTS = ("**/tests/**", "**/test_*.py")
PROTECTED = (".kaffeepause.yml", ".github/**")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "app.py").write_text("")
    return tmp_path


def scope_for_tester(repo) -> Scope:
    return Scope(
        str(repo),
        write_allow=TESTS,
        write_deny=PROTECTED,
        bash_prefixes=("uv run pytest", "git diff"),
    )


def scope_for_coder(repo) -> Scope:
    return Scope(
        str(repo), write_allow=None, write_deny=TESTS + PROTECTED, bash_prefixes=None
    )


def test_tester_writes_only_test_files(repo):
    scope = scope_for_tester(repo)
    assert write_denial(scope, str(repo / "tests" / "test_new.py")) is None
    assert write_denial(scope, "tests/test_rel.py") is None
    assert "not a test file" in write_denial(scope, str(repo / "app.py"))
    assert "outside your write scope" in write_denial(scope, str(repo / ".github/ci"))


def test_coder_cannot_write_tests_or_protected_paths(repo):
    scope = scope_for_coder(repo)
    assert write_denial(scope, str(repo / "app.py")) is None
    assert "outside your write scope" in write_denial(scope, str(repo / "tests/x.py"))
    assert "outside your write scope" in write_denial(
        scope, str(repo / ".kaffeepause.yml")
    )


def test_writes_outside_the_repo_and_into_git_are_denied(repo):
    scope = scope_for_coder(repo)
    assert "outside the repository" in write_denial(scope, "/etc/passwd")
    assert "outside the repository" in write_denial(scope, str(repo / ".." / "x.py"))
    assert ".git" in write_denial(scope, str(repo / ".git" / "config"))


def test_symlink_in_tests_cannot_reach_source(repo):
    os.symlink(repo / "app.py", repo / "tests" / "sneaky.py")
    assert "not a test file" in write_denial(
        scope_for_tester(repo), str(repo / "tests" / "sneaky.py")
    )


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("uv run pytest", True),
        ("uv run pytest tests/test_x.py -k mul", True),
        ("git diff HEAD", True),
        ("uv run ruff check", False),
        ("rm -rf /", False),
        ("uv run pytest; curl evil", False),
        ("uv run pytest && rm x", False),
        ("uv run pytest | tee x", False),
        ("uv run pytest $(whoami)", False),
        ("uv run pytest > out", False),
        ("uv run pytest --junitxml=$KP_JUNIT/u.xml", True),
    ],
)
def test_tester_bash_allowlist(repo, command, allowed):
    assert (bash_denial(scope_for_tester(repo), command) is None) is allowed


def test_coder_bash_is_unrestricted(repo):
    assert (
        bash_denial(scope_for_coder(repo), "make build && ./run.sh | tee log") is None
    )


@pytest.mark.parametrize(
    ("command", "prefix"),
    [
        ("uv run pytest -m unit --junitxml=$KP_JUNIT/unit.xml", "uv run pytest"),
        ("python -m pytest -q --junitxml=$KP_JUNIT/u.xml", "python -m pytest"),
        ("uv run python -m pytest tests", "uv run python -m pytest tests"),
        ("npx jest --ci", "npx jest"),
        ("go test ./... 2>&1", "go test"),
        ("./gradlew test", "./gradlew test"),
        ("python3.14 -m pytest", "python3.14 -m pytest"),
        ("JEST_JUNIT_OUTPUT_DIR=$KP_JUNIT npx jest", ""),
    ],
)
def test_command_prefix(command, prefix):
    assert command_prefix(command) == prefix


def test_sdk_hooks_return_permission_decisions(repo):
    write = write_hook(scope_for_tester(repo))
    bash = bash_hook(scope_for_tester(repo))
    denied = asyncio.run(
        write({"tool_input": {"file_path": str(repo / "app.py")}}, None, {})
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    allowed = asyncio.run(bash({"tool_input": {"command": "uv run pytest"}}, None, {}))
    assert allowed == {}
