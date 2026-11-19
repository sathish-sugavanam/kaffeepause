from app.task import BUG, FEATURE, acceptance_criteria, issue_kind


def test_criteria_from_a_heading():
    text = (
        "We need CSV export.\n\n## Acceptance criteria\n"
        "- GET /export returns text/csv\n- The first row is a header\n\n"
        "## Notes\n- not a criterion\n"
    )
    assert acceptance_criteria(text) == (
        "GET /export returns text/csv",
        "The first row is a header",
    )


def test_criteria_from_bold_heading_and_numbered_items():
    text = "**Acceptance Criteria:**\n1. One\n2) Two\n"
    assert acceptance_criteria(text) == ("One", "Two")


def test_criteria_from_checkboxes():
    assert acceptance_criteria("Do it.\n- [ ] Works\n- [x] Fast\n") == ("Works", "Fast")


def test_no_criteria():
    assert acceptance_criteria("Just make it better.\n- maybe this") == ()


def test_bug_can_use_expected_behaviour():
    text = "Steps to reproduce:\n1. Open /x\n\nExpected behaviour:\n- 200, not 500\n"
    assert acceptance_criteria(text, FEATURE) == ()
    assert "200, not 500" in acceptance_criteria(text, BUG)


def test_issue_kind_from_labels():
    assert issue_kind(["Backend", "Bug"]) == BUG
    assert issue_kind(["Feature"]) == FEATURE
