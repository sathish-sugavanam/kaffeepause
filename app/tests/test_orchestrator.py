"""End to end: orchestrator + pipeline against a bare git remote.

The "run container" is a local subprocess (LocalRunContainer), the checks run
real pytest on a tiny fixture repo, and the agents are scripted fakes that
edit files and return verdicts, the way app.agents.session would.
"""

import base64
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from app.agents.step import StepInput, StepOutput
from app.config import WorkerSettings
from app.container import LocalRunContainer
from app.errors import ErrorCode, RunFailed
from app.helpers.github import git_auth_env, remote_url
from app.inbox import InboxEvent
from app.intake import IssueReview
from app.orchestrator import get_task_from_event, issue_reviewer, run_dev_task

PY = sys.executable
SECRET = "sk-ant-this-is-the-run-containers-key"
CONFIG = f"""\
version: 1
stack: python
test:
  unit: "{PY} -m pytest -q -p no:cacheprovider --junitxml=$KP_JUNIT/unit.xml tests"
junit: "$KP_JUNIT/*.xml"
lint: ["{PY} -c 'import calc'"]
tester_owned: ["tests/**"]
protected: ["compose.yaml"]
"""
DESCRIPTION = "Add multiply.\n\n## Acceptance criteria\n- mul(2, 3) returns 6\n"

MUL_TEST = """\
import calc


def test_mul():
    fn = getattr(calc, "mul", None)
    assert fn is not None and fn(2, 3) == 6, "mul(2, 3) should be 6"
"""
NEG_TEST = """\
import calc


def test_mul_negative():
    assert calc.mul(-2, 3) == -6, "mul(-2, 3) should be -6"
"""
CALC = "def add(a, b):\n    return a + b\n"
GOOD_MUL = CALC + "\n\ndef mul(a, b):\n    return a * b\n"
BUGGY_MUL = CALC + "\n\ndef mul(a, b):\n    return a * b if a >= 0 else 0\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def event(description: str = DESCRIPTION, labels=()) -> InboxEvent:
    return InboxEvent(
        id=42,
        delivery_id="d-1",
        action="update",
        issue_id="SIG-7",
        payload={
            "data": {
                "title": "Add multiply!",
                "description": description,
                "labels": [{"name": n} for n in labels],
            }
        },
        attempts=1,
    )


def make_remote(tmp_path: Path, files: dict[str, str]) -> Path:
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "--quiet", str(bare), str(seed))
    for name, content in files.items():
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text(content)
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "init")
    git(seed, "push", "--quiet", "origin", "HEAD:main")
    return bare


BASE_FILES = {
    ".kaffeepause.yml": CONFIG,
    ".gitignore": "__pycache__/\n",
    "calc.py": CALC,
    "tests/test_calc.py": "import calc\n\n\ndef test_add():\n    assert calc.add(1, 2) == 3\n",
}


@pytest.fixture
def remote(tmp_path) -> Path:
    return make_remote(tmp_path, BASE_FILES)


@pytest.fixture
def settings(tmp_path, monkeypatch) -> WorkerSettings:
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    return WorkerSettings(
        database_url="unused",
        broker_url="memory://",
        work_dir=str(tmp_path / "work"),
        agent_env={"ANTHROPIC_API_KEY": SECRET},
    )


# ---- scripted agents ------------------------------------------------------


def make_tester_verdict(*tests, notes="", cannot_reproduce=False):
    return {
        "tests": [
            {"id": tid, "criterion": "mul(2, 3) returns 6", "expected_failure": msg}
            for tid, msg in tests
        ],
        "contract": "calc.mul(a, b) -> a * b",
        "gaps": [],
        "cannot_reproduce": cannot_reproduce,
        "notes": notes,
    }


def coder_verdict(disputes=()):
    return {
        "summary": "Added calc.mul.",
        "files_changed": ["calc.py"],
        "disputes": list(disputes),
    }


def security_verdict(*findings):
    return {"findings": list(findings), "summary": "Reviewed."}


BLOCKER = {
    "severity": "high",
    "file": "calc.py",
    "line": 5,
    "title": "Unsafe thing",
    "blocking": True,
    "fix_hint": "Do the safe thing.",
}


def writes(files: dict[str, str], verdict, cost: float = 0.5):
    def act(repo: Path, step: StepInput):
        for name, content in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(content)
        return verdict, cost

    return act


class Agents:
    """Plays a fixed script of (role, action). Fails the test on a surprise step."""

    def __init__(self, *script):
        self.script = list(script)
        self.inputs: list[StepInput] = []

    def __call__(self, step: StepInput) -> StepOutput:
        self.inputs.append(step)
        assert self.script, f"unexpected extra {step.role} step"
        role, act = self.script.pop(0)
        assert step.role == role, f"expected {role}, got {step.role}"
        verdict, cost = act(Path(step.scope.repo_dir), step)
        return StepOutput(subtype="success", verdict=verdict, turns=3, cost_usd=cost)

    @property
    def roles(self) -> list[str]:
        return [s.role for s in self.inputs]


RED_TESTER = (
    "tester",
    writes(
        {"tests/test_mul.py": MUL_TEST},
        make_tester_verdict(("tests/test_mul.py::test_mul", "should be 6")),
    ),
)
GOOD_CODER = ("coder", writes({"calc.py": GOOD_MUL}, coder_verdict()))
NO_GAPS = ("tester", writes({}, make_tester_verdict()))
CLEAN = ("security", writes({}, security_verdict()))


def run(remote, settings, agents, ev=None, opened=None, review=None):
    def open_pr(**kwargs):
        if opened is None:
            pytest.fail("must not open a PR")
        opened.update(kwargs)
        return "https://github.com/o/r/pull/1"

    return run_dev_task(
        ev or event(),
        settings,
        start_container=lambda: nullcontext(
            LocalRunContainer(Path(settings.work_dir).parent / "container", [PY])
        ),
        open_pull_request=open_pr,
        run_agent=agents,
        review_issue=review,
        remote=str(remote),
    )


def fails_with(code, remote, settings, agents, ev=None, review=None) -> RunFailed:
    with pytest.raises(RunFailed) as info:
        run(remote, settings, agents, ev, review=review)
    assert info.value.code == code, info.value
    assert "agent/" not in git(remote, "branch", "--list")
    return info.value


BRANCH = "agent/SIG-7-add-multiply"


# ---- tests ----------------------------------------------------------------


def test_task_from_event():
    task = get_task_from_event(event(labels=["Bug"]))
    assert (task.issue_id, task.branch, task.kind) == ("SIG-7", BRANCH, "bug")
    assert task.criteria == ("mul(2, 3) returns 6",)


def test_happy_path_opens_a_ready_pr(remote, settings):
    agents = Agents(RED_TESTER, GOOD_CODER, NO_GAPS, CLEAN)
    opened: dict = {}
    assert run(remote, settings, agents, opened=opened).endswith("/pull/1")

    assert agents.roles == ["tester", "coder", "tester", "security"]
    assert opened["draft"] is False
    assert (opened["head"], opened["base"]) == (BRANCH, "main")
    assert "`tests.test_mul::test_mul`" in opened["body"]
    assert "Added calc.mul." in opened["body"]
    assert "Feedback returns used: 0" in opened["body"]
    assert git(remote, "show", f"{BRANCH}:calc.py") == GOOD_MUL
    assert git(remote, "show", f"{BRANCH}:tests/test_mul.py") == MUL_TEST
    log = git(remote, "log", "--format=%s", f"main..{BRANCH}").splitlines()
    assert log == ["SIG-7: implement Add multiply!", "SIG-7: failing tests"]
    assert not any(Path(settings.work_dir).iterdir())  # private clones cleaned up


def test_step_inputs_carry_scope_skills_and_budget(remote, settings):
    agents = Agents(RED_TESTER, GOOD_CODER, NO_GAPS, CLEAN)
    run(remote, settings, agents, opened={})
    tester, coder, gap, security = agents.inputs
    assert tester.skills == ("kaffeepause:write-failing-tests",)
    assert tester.scope.write_allow == ("tests/**",)
    assert tester.scope.bash_prefixes == (f"{PY} -m pytest",)
    assert tester.max_turns == 60 and tester.max_budget_usd == 10.0
    assert coder.model == "claude-opus-5-5" and coder.max_turns == 80
    assert coder.max_budget_usd == 9.5  # $10 minus the tester's $0.50
    assert "tests/**" in coder.scope.write_deny
    assert coder.scope.bash_prefixes is None
    assert gap.skills == ("kaffeepause:test-gap-review",)
    assert security.scope.write_deny == ("**",)
    assert "semgrep" in security.scope.bash_prefixes
    assert "calc.py" in security.prompt  # the diff is in the prompt
    assert "gitleaks: not installed" in security.prompt


def test_bug_uses_reproduce_skill(remote, settings):
    agents = Agents(RED_TESTER, GOOD_CODER, NO_GAPS, CLEAN)
    run(remote, settings, agents, ev=event(labels=["Bug"]), opened={})
    assert agents.inputs[0].skills == ("kaffeepause:reproduce-bug",)


def test_no_criteria_spends_nothing(remote, settings):
    agents = Agents()
    fails_with(
        ErrorCode.NO_CRITERIA, remote, settings, agents, ev=event("Make it better.")
    )
    assert agents.inputs == []


READY = IssueReview(testable=0.95, large=0.05, bug=0.0)
BUG_REPORT = """\
calc.mul is missing.

Steps to reproduce:
- Call calc.mul(2, 3)

Expected behaviour:
- mul(2, 3) returns 6
"""


def reviewer(review, seen=None):
    def review_issue(task):
        if seen is not None:
            seen.append(task)
        return review

    return review_issue


def test_ready_issue_runs_as_usual(remote, settings):
    agents = Agents(RED_TESTER, GOOD_CODER, NO_GAPS, CLEAN)
    seen = []
    run(remote, settings, agents, review=reviewer(READY, seen), opened={})
    assert agents.roles == ["tester", "coder", "tester", "security"]
    assert seen[0].criteria == ("mul(2, 3) returns 6",)


def test_untestable_issue_spends_nothing(remote, settings):
    agents = Agents()
    review = IssueReview(testable=0.1, large=0.0, bug=0.0)
    err = fails_with(
        ErrorCode.ISSUE_NOT_READY, remote, settings, agents, review=reviewer(review)
    )
    assert "test can check" in err.message
    assert agents.inputs == []


def test_oversized_issue_spends_nothing(remote, settings):
    agents = Agents()
    review = IssueReview(testable=0.9, large=0.8, bug=0.0)
    err = fails_with(
        ErrorCode.ISSUE_NOT_READY, remote, settings, agents, review=reviewer(review)
    )
    assert "too large" in err.message
    assert agents.inputs == []


def test_no_criteria_wins_over_the_review(remote, settings):
    review = IssueReview(testable=0.0, large=1.0, bug=0.0)
    fails_with(
        ErrorCode.NO_CRITERIA,
        remote,
        settings,
        Agents(),
        ev=event("Make it better."),
        review=reviewer(review),
    )


def test_unlabelled_bug_report_runs_as_a_bug(remote, settings):
    agents = Agents(RED_TESTER, GOOD_CODER, NO_GAPS, CLEAN)
    review = IssueReview(testable=0.9, large=0.0, bug=0.97)
    run(
        remote,
        settings,
        agents,
        ev=event(BUG_REPORT),
        review=reviewer(review),
        opened={},
    )
    assert agents.inputs[0].skills == ("kaffeepause:reproduce-bug",)


def test_unlabelled_bug_report_without_jev_has_no_criteria(remote, settings):
    fails_with(ErrorCode.NO_CRITERIA, remote, settings, Agents(), ev=event(BUG_REPORT))


def test_jev_outage_runs_without_the_gate(remote, settings):
    agents = Agents(RED_TESTER, GOOD_CODER, NO_GAPS, CLEAN)
    run(remote, settings, agents, review=reviewer(None), opened={})
    assert agents.roles == ["tester", "coder", "tester", "security"]


def test_missing_config(tmp_path, settings):
    remote = make_remote(tmp_path, {"calc.py": CALC})
    err = fails_with(ErrorCode.CONFIG_INVALID, remote, settings, Agents())
    assert "missing" in err.message


def test_baseline_red_spends_nothing(tmp_path, settings):
    files = {**BASE_FILES, "tests/test_calc.py": "def test_add():\n    assert False\n"}
    remote = make_remote(tmp_path, files)
    agents = Agents()
    fails_with(ErrorCode.BASELINE_RED, remote, settings, agents)
    assert agents.inputs == []


def test_red_check_rejects_a_crashing_test_after_one_retry(remote, settings):
    crash = "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"
    bad = (
        "tester",
        writes(
            {"tests/test_mul.py": crash},
            make_tester_verdict(("tests/test_mul.py::test_mul", "should be 6")),
        ),
    )
    agents = Agents(bad, bad)
    err = fails_with(ErrorCode.RED_CHECK_FAILED, remote, settings, agents)
    assert agents.roles == ["tester", "tester"]
    assert "crashed" in err.message or "not among" in err.message
    assert "red check" in agents.inputs[1].prompt  # the retry got the feedback


def test_red_check_retry_can_succeed(remote, settings):
    wrong_message = (
        "tester",
        writes(
            {"tests/test_mul.py": MUL_TEST},
            make_tester_verdict(
                ("tests/test_mul.py::test_mul", "a message it never prints")
            ),
        ),
    )
    agents = Agents(wrong_message, RED_TESTER, GOOD_CODER, NO_GAPS, CLEAN)
    opened: dict = {}
    run(remote, settings, agents, opened=opened)
    assert opened["draft"] is False
    assert "does not contain the expected text" in agents.inputs[1].prompt


def test_tester_writing_source_files_fails(remote, settings):
    sneaky = (
        "tester",
        writes(
            {"tests/test_mul.py": MUL_TEST, "calc.py": GOOD_MUL},
            make_tester_verdict(("tests/test_mul.py::test_mul", "should be 6")),
        ),
    )
    err = fails_with(ErrorCode.PROTECTED_PATH, remote, settings, Agents(sneaky))
    assert "calc.py" in err.message


def test_coder_editing_tests_fails_the_run(remote, settings):
    cheat = (
        "coder",
        writes({"tests/test_mul.py": "def test_mul():\n    pass\n"}, coder_verdict()),
    )
    err = fails_with(
        ErrorCode.TESTER_FILES_CHANGED, remote, settings, Agents(RED_TESTER, cheat)
    )
    assert "tests/test_mul.py" in err.message


def test_coder_touching_protected_paths_fails_the_run(remote, settings):
    evil = (
        "coder",
        writes({"calc.py": GOOD_MUL, ".github/ci.yml": "x"}, coder_verdict()),
    )
    err = fails_with(
        ErrorCode.PROTECTED_PATH, remote, settings, Agents(RED_TESTER, evil)
    )
    assert ".github/ci.yml" in err.message


def test_failed_green_check_returns_to_coder(remote, settings):
    lazy = ("coder", writes({}, coder_verdict()))
    agents = Agents(RED_TESTER, lazy, GOOD_CODER, NO_GAPS, CLEAN)
    opened: dict = {}
    run(remote, settings, agents, opened=opened)
    assert agents.roles == ["tester", "coder", "coder", "tester", "security"]
    assert "Green check failed" in agents.inputs[2].prompt
    assert opened["draft"] is False and "Feedback returns used: 1" in opened["body"]


def test_dispute_discards_the_implementation_and_reruns_the_tester(remote, settings):
    dispute = {
        "test_id": "tests/test_mul.py::test_mul",
        "reason": "r",
        "criterion": "c",
    }
    disputing = ("coder", writes({"calc.py": BUGGY_MUL}, coder_verdict([dispute])))
    agents = Agents(RED_TESTER, disputing, RED_TESTER, GOOD_CODER, NO_GAPS, CLEAN)
    opened: dict = {}
    run(remote, settings, agents, opened=opened)
    assert agents.roles == ["tester", "coder", "tester", "coder", "tester", "security"]
    assert "disputed" in agents.inputs[2].prompt
    assert opened["draft"] is False and "Feedback returns used: 1" in opened["body"]


def test_gap_review_failures_go_back_to_the_coder(remote, settings):
    buggy = ("coder", writes({"calc.py": BUGGY_MUL}, coder_verdict()))
    gap = (
        "tester",
        writes(
            {"tests/test_neg.py": NEG_TEST},
            make_tester_verdict(("tests/test_neg.py::test_mul_negative", "")),
        ),
    )
    agents = Agents(RED_TESTER, buggy, gap, GOOD_CODER, CLEAN)
    opened: dict = {}
    run(remote, settings, agents, opened=opened)
    assert agents.roles == ["tester", "coder", "tester", "coder", "security"]
    assert "gap review added tests that fail" in agents.inputs[3].prompt
    assert "tests.test_neg::test_mul_negative" in opened["body"]
    assert git(remote, "show", f"{BRANCH}:tests/test_neg.py") == NEG_TEST


def test_unresolved_blocking_finding_opens_a_draft_pr(remote, settings):
    blocking = ("security", writes({}, security_verdict(BLOCKER)))
    agents = Agents(
        RED_TESTER, GOOD_CODER, NO_GAPS, blocking,  # return 1
        GOOD_CODER, blocking,  # return 2
        GOOD_CODER, blocking,  # none left: draft
    )  # fmt: skip
    opened: dict = {}
    run(remote, settings, agents, opened=opened)
    assert opened["draft"] is True
    assert "Draft" in opened["body"] and "Unsafe thing" in opened["body"]
    assert "Blocking security findings" in agents.inputs[4].prompt


def test_blocking_finding_fixed_on_return(remote, settings):
    blocking = ("security", writes({}, security_verdict(BLOCKER)))
    agents = Agents(RED_TESTER, GOOD_CODER, NO_GAPS, blocking, GOOD_CODER, CLEAN)
    opened: dict = {}
    run(remote, settings, agents, opened=opened)
    assert opened["draft"] is False


def test_budget_runs_out(remote, settings):
    pricey_tester = (RED_TESTER[0], writes({"tests/test_mul.py": MUL_TEST},
                     make_tester_verdict(("tests/test_mul.py::test_mul", "should be 6")), cost=6))  # fmt: skip
    pricey_coder = ("coder", writes({"calc.py": GOOD_MUL}, coder_verdict(), cost=6))
    agents = Agents(pricey_tester, pricey_coder)
    fails_with(ErrorCode.BUDGET_EXCEEDED, remote, settings, agents)
    assert agents.inputs[1].max_budget_usd == 4.0


def test_bad_verdict(remote, settings):
    nonsense = ("coder", writes({"calc.py": GOOD_MUL}, {"summary": "hi"}))
    err = fails_with(
        ErrorCode.BAD_VERDICT, remote, settings, Agents(RED_TESTER, nonsense)
    )
    assert err.message.startswith("coder:")


def test_cannot_reproduce(remote, settings):
    nope = (
        "tester",
        writes({}, make_tester_verdict(cannot_reproduce=True, notes="works")),
    )
    fails_with(ErrorCode.CANNOT_REPRODUCE, remote, settings, Agents(nope))


def test_missing_output_is_a_bad_verdict(remote, settings):
    def crashed(step: StepInput) -> StepOutput:
        return StepOutput.parse(None)  # the session died without output.json

    fails_with(ErrorCode.BAD_VERDICT, remote, settings, crashed)


# ---- credentials and untrusted text --------------------------------------


def test_secret_in_the_change_is_never_pushed(remote, settings):
    leaky = ("coder", writes({"calc.py": GOOD_MUL + f"# {SECRET}\n"}, coder_verdict()))
    agents = Agents(RED_TESTER, leaky, NO_GAPS, CLEAN)
    err = fails_with(ErrorCode.SECRET_IN_CHANGE, remote, settings, agents)
    assert SECRET not in str(err)


def test_agent_text_is_redacted_and_mentions_defused(remote, settings):
    chatty = (
        "coder",
        writes(
            {"calc.py": GOOD_MUL},
            {**coder_verdict(), "summary": f"Added mul. cc @octocat, key {SECRET}"},
        ),
    )
    opened: dict = {}
    run(remote, settings, Agents(RED_TESTER, chatty, NO_GAPS, CLEAN), opened=opened)
    assert SECRET not in opened["body"] and "[redacted]" in opened["body"]
    assert "@octocat" not in opened["body"] and "@\u200boctocat" in opened["body"]


def test_failure_messages_are_redacted(remote, settings):
    leaky_test = MUL_TEST.replace('"mul(2, 3) should be 6"', f'"{SECRET}"')
    bad = (
        "tester",
        writes(
            {"tests/test_mul.py": leaky_test},
            make_tester_verdict(("tests/test_mul.py::test_mul", "never printed")),
        ),
    )
    err = fails_with(ErrorCode.RED_CHECK_FAILED, remote, settings, Agents(bad, bad))
    assert SECRET not in str(err)


def test_git_auth_travels_in_the_environment_only():
    env = git_auth_env("ghp_example_token_value")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    header = env["GIT_CONFIG_VALUE_0"].removeprefix("AUTHORIZATION: basic ")
    assert base64.b64decode(header) == b"x-access-token:ghp_example_token_value"
    assert remote_url("o/r") == "https://github.com/o/r.git"  # no token in the URL


def test_gate_is_off_without_a_typesafe_key(settings):
    assert issue_reviewer(settings) is None
    on = WorkerSettings(
        database_url="unused", broker_url="memory://", typesafe_api_key="k"
    )
    assert callable(issue_reviewer(on))
