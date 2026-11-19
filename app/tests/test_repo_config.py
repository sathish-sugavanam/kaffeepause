import pytest

from app.errors import ErrorCode, RunFailed
from app.repo_config import DEFAULT_TESTER_OWNED, matches_any, parse_repo_config

VALID = """
version: 1
stack: django
environment:
  image: python:3.14-slim
setup: ["uv sync"]
test:
  unit: "uv run pytest -m unit --junitxml=$KP_JUNIT/unit.xml"
  integration: "uv run pytest -m integration --junitxml=$KP_JUNIT/integration.xml"
junit: "$KP_JUNIT/*.xml"
lint: ["uv run ruff check", "uv run ruff format --check"]
tester_owned: ["**/tests/**", "**/test_*.py", "**/conftest.py"]
protected: ["compose.yaml"]
"""


def invalid_field(text: str | None) -> str:
    with pytest.raises(RunFailed) as info:
        parse_repo_config(text)
    assert info.value.code == ErrorCode.CONFIG_INVALID
    return info.value.message


def test_valid_config():
    config = parse_repo_config(VALID)
    assert list(config.test) == ["unit", "integration"]
    assert config.stack == "django"
    assert config.setup == ("uv sync",)
    assert config.base_branch == "main"
    assert config.timeout_seconds == 900
    assert config.unused == ("environment",)
    assert config.junit_glob == "*.xml"
    assert config.is_junit_report("unit.xml") and not config.is_junit_report("x.txt")
    # The harness's own paths are always protected, plus the repo's.
    assert config.protected == (
        ".kaffeepause.yml",
        ".github/**",
        ".claude/**",
        "compose.yaml",
    )


def test_single_test_command_and_default_tester_globs():
    config = parse_repo_config(
        'version: 1\ntest: "make test"\njunit: "$KP_JUNIT/*.xml"\n'
    )
    assert config.test == {"tests": "make test"}
    assert config.tester_owned == DEFAULT_TESTER_OWNED


def test_junit_is_required():
    assert "`junit` is required" in invalid_field("version: 1\ntest: make test\n")


@pytest.mark.parametrize(
    ("text", "field"),
    [
        (None, "missing"),
        ("version: 2\ntest: t\njunit: $KP_JUNIT/x", "`version`"),
        ("version: 1\njunit: $KP_JUNIT/x", "`test`"),
        ("version: 1\ntest: t\njunit: out/*.xml", "`junit`"),
        ("version: 1\ntest: t\njunit: out/$KP_JUNIT/*.xml", "`junit`"),
        ("version: 1\ntest: t\njunit: $KP_JUNIT/../../etc/*", "`junit`"),
        ("version: 1\ntest: t\njunit: $KP_JUNIT/x\nlint: [1]", "`lint`"),
        ("version: 1\ntest: t\njunit: $KP_JUNIT/x\nsurprise: 1", "`surprise`"),
        ("version: 1\ntest: t\njunit: $KP_JUNIT/x\ntimeout_seconds: 0", "timeout"),
        ("[not, a, mapping]", "mapping"),
        ("version: [", "YAML"),
    ],
)
def test_invalid_configs_name_the_field(text, field):
    assert field in invalid_field(text)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_x.py", True),
        ("app/tests/test_x.py", True),
        ("test_top.py", True),
        ("src/widget.test.ts", True),
        ("pkg/handler_test.go", True),
        ("app/models.py", False),
        ("contest.py", False),
    ],
)
def test_default_tester_globs(path, expected):
    assert matches_any(path, DEFAULT_TESTER_OWNED) is expected
