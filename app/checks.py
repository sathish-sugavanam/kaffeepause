"""Baseline, red, green and gap-review checks (PRD section 5).

These are pure functions over parsed JUnit results. The pipeline runs the
commands in the run container and hands the parsed results here, so every
decision about "did this pass" is made by worker code, never by a model.
"""

from dataclasses import dataclass, field

from app.junit import ERROR, FAILURE, PASSED, SKIPPED, Results, counts, match_test_id


@dataclass
class SuiteRun:
    """One run of the repo's test command(s), every layer together."""

    results: Results
    layers: dict[str, dict[str, int]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # e.g. no JUnit, timeout
    lint_failures: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return counts(self.results)


@dataclass
class CheckOutcome:
    ok: bool
    problems: list[str] = field(default_factory=list)

    def feedback(self) -> str:
        return "\n".join(f"- {p}" for p in self.problems)


def _broken(run: SuiteRun) -> list[str]:
    return [
        f"{r.id} {'crashed (<error>)' if r.status == ERROR else 'failed'}: "
        f"{r.message[:500] or '(no message)'}"
        for r in sorted(run.results.values(), key=lambda r: r.id)
        if r.status in (FAILURE, ERROR)
    ]


def baseline_check(run: SuiteRun) -> CheckOutcome:
    problems = run.problems + _broken(run)
    return CheckOutcome(not problems, problems)


def red_check(
    baseline: SuiteRun, run: SuiteRun, expected: dict[str, str]
) -> tuple[CheckOutcome, dict[str, str]]:
    """Every new test must fail on an assertion with the stated message.

    `expected` maps the tester's claimed test ID to its expected failure
    substring. Returns the outcome and the claimed-to-JUnit ID mapping.
    """
    problems = list(run.problems)
    new_ids = set(run.results) - set(baseline.results)
    if not new_ids and not problems:
        problems.append("no new tests appeared in the JUnit report")

    matched: dict[str, str] = {}
    for claimed, message in expected.items():
        tid = match_test_id(claimed, new_ids)
        if tid is None:
            problems.append(
                f"test `{claimed}` is not among the new test IDs in the JUnit "
                f"report: {', '.join(sorted(new_ids)) or '(none)'}"
            )
            continue
        matched[claimed] = tid
        result = run.results[tid]
        if result.status == ERROR:
            problems.append(
                f"{tid} crashed (<error>) instead of failing an assertion: "
                f"{result.message[:500]}"
            )
        elif result.status == PASSED:
            problems.append(f"{tid} already passes on the base code")
        elif result.status == SKIPPED:
            problems.append(f"{tid} was skipped")
        elif message not in result.message:
            problems.append(
                f"{tid} failed, but its message does not contain the expected "
                f"text {message!r}. Actual: {result.message[:500]!r}"
            )

    for tid in sorted(new_ids - set(matched.values())):
        problems.append(f"new test {tid} is not listed in the verdict")

    for tid, before in baseline.results.items():
        now = run.results.get(tid)
        if now is None:
            problems.append(f"baseline test {tid} disappeared")
        elif before.status == PASSED and now.status != PASSED:
            problems.append(f"baseline test {tid} no longer passes ({now.status})")
    return CheckOutcome(not problems, problems), matched


def green_check(
    run: SuiteRun, required_ids: set[str], lint_failures: list[str]
) -> CheckOutcome:
    """All tests pass, the tests we rely on are all still there, lint is clean."""
    problems = list(run.problems) + _broken(run)
    for tid in sorted(required_ids):
        if tid not in run.results:
            problems.append(f"test {tid} no longer runs")
        elif run.results[tid].status == SKIPPED:
            problems.append(f"test {tid} is now skipped")
    problems += lint_failures
    return CheckOutcome(not problems, problems)


def gap_check(before: SuiteRun, run: SuiteRun) -> tuple[CheckOutcome, set[str]]:
    """The tester's gap-review tests must pass against the coder's code."""
    new_ids = set(run.results) - set(before.results)
    problems = list(run.problems) + _broken(run)
    return CheckOutcome(not problems, problems), new_ids
