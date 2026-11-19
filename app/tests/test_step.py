import pytest

from app.agents.hooks import Scope, bash_denial, scope_for
from app.agents.roles import CODER, SECURITY, TESTER
from app.agents.step import StepInput, StepOutput
from app.repo_config import parse_repo_config

CONFIG = parse_repo_config(
    'version: 1\ntest: "uv run pytest --junitxml=$KP_JUNIT/u.xml"\n'
    'junit: "$KP_JUNIT/*.xml"\ntester_owned: ["tests/**"]\n'
)


def test_step_input_round_trips():
    step = StepInput(
        role=TESTER,
        model="claude-sonnet-5-5",
        prompt="Write tests.",
        skills=("kaffeepause:write-failing-tests",),
        max_turns=60,
        max_budget_usd=9.5,
        scope=scope_for(TESTER, "/w/repo", CONFIG),
    )
    assert StepInput.from_json(step.to_json()) == step


def test_step_output_round_trips():
    out = StepOutput(subtype="success", verdict={"a": 1}, turns=3, cost_usd=0.5)
    assert StepOutput.parse(out.to_json()) == out


@pytest.mark.parametrize(
    ("text", "subtype"),
    [(None, "no_output"), ("{not json", "bad_output"), ("[1, 2]", "bad_output")],
)
def test_step_output_parse_never_raises(text, subtype):
    out = StepOutput.parse(text)
    assert (out.subtype, out.verdict) == (subtype, None)


def test_step_output_parse_coerces_hostile_fields():
    out = StepOutput.parse(
        '{"subtype": "success", "turns": "many", "cost_usd": -5, '
        '"usage": [1], "api_error_status": true, "summary": "' + "x" * 30_000 + '"}'
    )
    assert (out.turns, out.cost_usd, out.usage, out.api_error_status) == (
        0,
        0.0,
        {},
        None,
    )
    assert len(out.summary) == 20_000


def test_scope_for_each_role():
    tester = scope_for(TESTER, "/w/repo", CONFIG)
    assert tester.write_allow == ("tests/**",)
    assert tester.bash_prefixes == ("uv run pytest",)  # no git for the tester
    coder = scope_for(CODER, "/w/repo", CONFIG)
    assert coder.bash_prefixes is None and "tests/**" in coder.write_deny
    security = scope_for(SECURITY, "/w/repo", CONFIG)
    assert security.write_allow == () and "semgrep" in security.bash_prefixes


@pytest.mark.parametrize(
    "command",
    [
        "git diff --output=src/app.py",
        "git diff --output src/app.py",
        "git diff --ext-diff",
        "git log --textconv",
        "git -c core.pager=evil log",
        "git -ccore.pager=evil log",
    ],
)
def test_read_only_git_cannot_write_or_execute(command):
    scope = Scope(
        "/w/repo",
        write_allow=(),
        write_deny=("**",),
        bash_prefixes=("git diff", "git log", "git -c"),
    )
    assert "not allowed" in bash_denial(scope, command)


def test_read_only_git_still_works():
    scope = scope_for(SECURITY, "/w/repo", CONFIG)
    assert bash_denial(scope, "git diff --cached --stat HEAD~1") is None
