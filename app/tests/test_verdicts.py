import pytest

from app.agents.roles import CODER, SECURITY, TESTER
from app.agents.verdicts import verdict_error

TESTER_OK = {
    "tests": [{"id": "t::a", "criterion": "c", "expected_failure": "boom"}],
    "contract": "def a() -> int",
    "gaps": [],
    "cannot_reproduce": False,
    "notes": "",
}
CODER_OK = {"summary": "Did it.", "files_changed": ["a.py"], "disputes": []}
SECURITY_OK = {
    "findings": [
        {
            "severity": "high",
            "file": "a.py",
            "line": 3,
            "title": "SQL injection",
            "blocking": True,
            "fix_hint": "Use params.",
        }
    ],
    "summary": "One issue.",
}


@pytest.mark.parametrize(
    ("role", "verdict"),
    [(TESTER, TESTER_OK), (CODER, CODER_OK), (SECURITY, SECURITY_OK)],
)
def test_valid_verdicts(role, verdict):
    assert verdict_error(role, verdict) is None


@pytest.mark.parametrize(
    ("role", "verdict", "where"),
    [
        (TESTER, {**TESTER_OK, "cannot_reproduce": "no"}, "cannot_reproduce"),
        (TESTER, {k: v for k, v in TESTER_OK.items() if k != "contract"}, "(root)"),
        (CODER, {**CODER_OK, "summary": ""}, "summary"),
        (CODER, {**CODER_OK, "extra": 1}, "(root)"),
        (
            SECURITY,
            {
                **SECURITY_OK,
                "findings": [{**SECURITY_OK["findings"][0], "severity": "meh"}],
            },
            "findings/0/severity",
        ),
        (CODER, None, "(root)"),
    ],
)
def test_invalid_verdicts(role, verdict, where):
    assert verdict_error(role, verdict).startswith(where)
