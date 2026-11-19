from pathlib import Path

import pytest

from app.checks import TestRun, baseline_check, gap_check, green_check, red_check
from app.junit import ERROR, FAILURE, PASSED, SKIPPED, match_test_id, parse_junit

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_calc" name="test_add"/>
  <testcase classname="tests.test_calc" name="test_skip"><skipped message="later"/></testcase>
  <testcase classname="tests.test_mul" name="test_mul">
    <failure message="AssertionError: mul(2, 3) should be 6">assert None</failure>
  </testcase>
  <testcase classname="tests.test_div" name="test_div">
    <error message="ImportError: cannot import name 'div'"/>
  </testcase>
</testsuite></testsuites>
"""


def run(**statuses: tuple[str, str]) -> TestRun:
    from app.junit import TestResult

    return TestRun({tid: TestResult(tid, s, m) for tid, (s, m) in statuses.items()})


def test_parse_junit_statuses(tmp_path: Path):
    path = tmp_path / "unit.xml"
    path.write_text(JUNIT)
    results = parse_junit([path])
    assert {tid: r.status for tid, r in results.items()} == {
        "tests.test_calc::test_add": PASSED,
        "tests.test_calc::test_skip": SKIPPED,
        "tests.test_mul::test_mul": FAILURE,
        "tests.test_div::test_div": ERROR,
    }
    assert "mul(2, 3) should be 6" in results["tests.test_mul::test_mul"].message


def test_parse_junit_rejects_garbage(tmp_path: Path):
    path = tmp_path / "bad.xml"
    path.write_text("<testsuite>")
    with pytest.raises(ValueError, match="not valid JUnit XML"):
        parse_junit([path])


@pytest.mark.parametrize(
    "claimed",
    [
        "tests.test_mul::test_mul",
        "tests/test_mul.py::test_mul",
        "test_mul.py::test_mul",
        "app/tests/test_mul.py::test_mul",
    ],
)
def test_match_test_id_accepts_common_spellings(claimed):
    ids = {"tests.test_mul::test_mul", "tests.test_calc::test_add"}
    assert match_test_id(claimed, ids) == "tests.test_mul::test_mul"


def test_match_test_id_refuses_ambiguous_suffix():
    ids = {"a.test_x::test_one", "b.test_x::test_one"}
    assert match_test_id("test_x::test_one", ids) is None


BASE = run(**{"t::add": (PASSED, "")})


def test_baseline_check():
    assert baseline_check(BASE).ok
    broken = baseline_check(run(**{"t::add": (FAILURE, "boom")}))
    assert not broken.ok and "t::add failed: boom" in broken.problems[0]


def test_red_check_passes_on_assertion_failure_with_expected_message():
    after = run(**{"t::add": (PASSED, ""), "t::mul": (FAILURE, "mul should be 6")})
    outcome, matched = red_check(BASE, after, {"t::mul": "should be 6"})
    assert outcome.ok, outcome.problems
    assert matched == {"t::mul": "t::mul"}


@pytest.mark.parametrize(
    ("after", "expected", "problem"),
    [
        ({"t::mul": (ERROR, "ImportError")}, {"t::mul": "x"}, "crashed (<error>)"),
        ({"t::mul": (PASSED, "")}, {"t::mul": "x"}, "already passes"),
        ({"t::mul": (SKIPPED, "")}, {"t::mul": "x"}, "was skipped"),
        ({"t::mul": (FAILURE, "other")}, {"t::mul": "x"}, "does not contain"),
        ({"t::mul": (FAILURE, "x")}, {"t::nope": "x"}, "not among the new test IDs"),
        ({"t::mul": (FAILURE, "x")}, {}, "not listed in the verdict"),
        ({}, {}, "no new tests"),
    ],
)
def test_red_check_failures(after, expected, problem):
    outcome, _ = red_check(BASE, run(**{"t::add": (PASSED, "")}, **after), expected)
    assert not outcome.ok
    assert any(problem in p for p in outcome.problems), outcome.problems


def test_red_check_fails_when_a_baseline_test_breaks():
    after = run(**{"t::add": (FAILURE, "broke"), "t::mul": (FAILURE, "x")})
    outcome, _ = red_check(BASE, after, {"t::mul": "x"})
    assert "baseline test t::add no longer passes (failure)" in outcome.problems


def test_green_check():
    ok = run(**{"t::add": (PASSED, ""), "t::mul": (PASSED, "")})
    assert green_check(ok, {"t::add", "t::mul"}, []).ok

    missing = green_check(run(**{"t::add": (PASSED, "")}), {"t::add", "t::mul"}, [])
    assert "test t::mul no longer runs" in missing.problems

    linted = green_check(ok, {"t::add"}, ["lint `ruff` exited 1"])
    assert linted.problems == ["lint `ruff` exited 1"]


def test_gap_check_reports_new_failing_tests():
    before = run(**{"t::add": (PASSED, "")})
    after = run(**{"t::add": (PASSED, ""), "t::neg": (FAILURE, "neg")})
    outcome, new_ids = gap_check(before, after)
    assert new_ids == {"t::neg"}
    assert not outcome.ok and "t::neg failed: neg" in outcome.problems[0]
