"""The readiness gate: one Jev call judges an issue before any agent runs.

The regex in app.task decides whether an issue has acceptance criteria at all.
Jev (TypeSafe's System One model, docs.typesafe.ai) answers whether those
criteria are worth a run: are they testable, is the change small enough, and is
an unlabelled issue really a bug. Each answer is a calibrated probability, and
the thresholds below turn them into decisions.

The gate fails open. If Jev is unreachable or answers badly, the run goes ahead
on the regex gate alone, because a run is still bounded by its budget.

The issue text is untrusted, but Jev only returns numbers, so the most a crafted
issue can do is pass or fail this gate.
"""

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass

import httpx2
from typesafe_sdk import Choice, Noul, RetryPolicy, Score, TypeSafeClient, TypeSafeError

from app.task import BUG, FEATURE, Task, acceptance_criteria

log = logging.getLogger("app.intake")

DEFAULT_MODEL = "jev-latest"
TIMEOUT_SECONDS = 20.0

# Below this, the criteria are too vague for the tester to write a failing test.
TESTABLE_MIN = 0.5
# At or above this, the change is too large for one run.
LARGE_MAX = 0.6
# At or above this, an issue without the Bug label is treated as a bug. High,
# because a wrong guess sends a feature to the reproduce-bug skill.
BUG_MIN = 0.9

SCOPE_LEVELS = (
    "One small, focused change to a function, route or command",
    "A few files for one feature or fix",
    "Large or cross-cutting: many modules, a migration, or a redesign",
)
LARGE = len(SCOPE_LEVELS) - 1

QUESTIONS = {
    "testable": Noul(
        instructions="Every acceptance criterion describes observable behaviour "
        "that an automated test could check",
    ),
    "scope": Score(
        instructions="How large the code change needed for this issue is",
        criteria=list(SCOPE_LEVELS),
    ),
    "kind": Choice(
        instructions="What kind of issue this is",
        criteria={
            BUG: "Existing behaviour is broken or wrong",
            FEATURE: "New behaviour, or a deliberate change to existing behaviour",
        },
    ),
}


@dataclass(frozen=True)
class IssueReview:
    testable: float  # probability the criteria are testable
    large: float  # probability the change is "large"
    bug: float  # probability the issue is a bug


ReviewIssue = Callable[[Task], IssueReview | None]


def issue_state(task: Task) -> str:
    criteria = "\n".join(f"- {c}" for c in task.criteria) or "(none found)"
    return (
        f"Title: {task.title}\n\n"
        f"Description:\n{task.description}\n\n"
        f"Acceptance criteria parsed from the description:\n{criteria}\n"
    )


def jev_reviewer(
    api_key: str,
    model: str = DEFAULT_MODEL,
    *,
    transport: httpx2.BaseTransport | None = None,
) -> ReviewIssue:
    """A reviewer that asks Jev. Returns None instead of raising on any API error."""

    def review(task: Task) -> IssueReview | None:
        try:
            with TypeSafeClient(
                api_key=api_key,
                model=model,
                timeout=TIMEOUT_SECONDS,
                retry=RetryPolicy(max_retries=1),
                transport=transport,
            ) as client:
                response = client.system_one(
                    state=issue_state(task), questions=QUESTIONS
                )
            return IssueReview(
                testable=response.nouls["testable"].noul,
                large=response.scores["scope"].probabilities.get(LARGE, 0.0),
                bug=response.choices["kind"].probabilities.get(BUG, 0.0),
            )
        except (TypeSafeError, KeyError) as exc:
            # The type only: an error message could quote the request.
            log.warning(
                "readiness check skipped for %s: %s", task.issue_id, type(exc).__name__
            )
            return None

    return review


def with_kind(task: Task, review: IssueReview) -> Task:
    """An unlabelled issue that Jev is confident is a bug becomes one."""
    if task.kind == FEATURE and review.bug >= BUG_MIN:
        return dataclasses.replace(
            task, kind=BUG, criteria=acceptance_criteria(task.description, BUG)
        )
    return task


def readiness_problem(review: IssueReview) -> str | None:
    """Why the issue is not ready for a run, or None when it is."""
    if review.testable < TESTABLE_MIN:
        return (
            "the acceptance criteria don't describe behaviour a test can check "
            f"(testable: {review.testable:.2f}); make each one observable"
        )
    if review.large >= LARGE_MAX:
        return (
            f"the change looks too large for one run (large: {review.large:.2f}); "
            "split the issue into smaller ones"
        )
    return None
