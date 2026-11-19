import json

import httpx2
import pytest

from app.intake import IssueReview, jev_reviewer, readiness_problem, with_kind
from app.task import BUG, FEATURE, Task

KEY = "ts-test-key"

BUG_REPORT = """\
Exporting a report crashes.

Steps to reproduce:
- Open a report
- Click Export

Expected behaviour:
- A CSV file downloads
"""


def task(description: str = "", criteria=("GET /export returns 200",), kind=FEATURE):
    return Task(
        issue_id="SIG-9",
        title="Export reports",
        description=description,
        branch="agent/SIG-9-export-reports",
        kind=kind,
        criteria=tuple(criteria),
    )


def answers(testable=0.95, scope=(0.7, 0.3, 0.0), bug=0.1):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "testable": {"type": "noul", "noul": testable},
            "scope": {
                "type": "score",
                "score": scope[1] / 2 + scope[2],
                "confidence": 0.8,
                "legend": {"0": "small", "1": "medium", "2": "large"},
                "probabilities": {str(i): p for i, p in enumerate(scope)},
            },
            "kind": {
                "type": "choice",
                "choice": BUG if bug >= 0.5 else FEATURE,
                "confidence": 0.9,
                "probabilities": {BUG: bug, FEATURE: 1 - bug},
            },
        },
        "usage": {"input_tokens": 300, "output_tokens": 40},
    }


def mock_jev(status=200, body=None, seen=None):
    def handle(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return httpx2.Response(status, json=body if body is not None else answers())

    return httpx2.MockTransport(handle)


# ---- jev_reviewer -------------------------------------------------------------


def test_asks_jev_about_the_issue_and_reads_probabilities():
    seen: list[httpx2.Request] = []
    review = jev_reviewer(
        KEY, transport=mock_jev(body=answers(0.8, (0.1, 0.2, 0.7), 0.95), seen=seen)
    )(task("Add a CSV export."))
    assert review == IssueReview(testable=0.8, large=0.7, bug=0.95)

    (request,) = seen
    assert request.method == "POST" and request.url.path.endswith("/systemone")
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "jev-latest"
    assert set(body["questions"]) == {"testable", "scope", "kind"}
    assert "Add a CSV export." in json.dumps(body["state"])
    assert "GET /export returns 200" in json.dumps(body["state"])


def test_uses_the_configured_model():
    seen: list[httpx2.Request] = []
    jev_reviewer(KEY, "jev-1.13.0", transport=mock_jev(seen=seen))(task())
    assert json.loads(seen[0].content)["model"] == "jev-1.13.0"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"error": "bad key"}),
        (400, {"error": "bad request"}),
        (200, {"model": "jev", "answers": {}, "usage": {}}),  # answers missing
        (200, {"unexpected": True}),
    ],
)
def test_api_problems_skip_the_gate(status, body, caplog):
    review = jev_reviewer(KEY, transport=mock_jev(status, body))(task())
    assert review is None
    assert "readiness check skipped for SIG-9" in caplog.text
    assert KEY not in caplog.text


# ---- decisions ---------------------------------------------------------------


def test_ready_issue_has_no_problem():
    assert readiness_problem(IssueReview(testable=0.9, large=0.1, bug=0.0)) is None


def test_untestable_criteria_are_refused():
    problem = readiness_problem(IssueReview(testable=0.2, large=0.1, bug=0.0))
    assert problem and "test can check" in problem and "0.20" in problem


def test_large_change_is_refused():
    problem = readiness_problem(IssueReview(testable=0.9, large=0.6, bug=0.0))
    assert problem and "too large" in problem


def test_thresholds_are_inclusive_on_the_safe_side():
    assert readiness_problem(IssueReview(testable=0.5, large=0.59, bug=0.0)) is None


def test_confident_bug_without_label_becomes_a_bug():
    unlabelled = task(BUG_REPORT, criteria=())
    became = with_kind(unlabelled, IssueReview(testable=0.9, large=0.0, bug=0.9))
    assert became.kind == BUG
    assert became.criteria == ("Open a report", "Click Export", "A CSV file downloads")


def test_unsure_bug_stays_a_feature():
    unlabelled = task(BUG_REPORT, criteria=())
    assert with_kind(unlabelled, IssueReview(0.9, 0.0, bug=0.89)) is unlabelled


def test_labelled_bug_is_never_downgraded():
    labelled = task(BUG_REPORT, kind=BUG)
    assert with_kind(labelled, IssueReview(0.9, 0.0, bug=0.0)) is labelled
