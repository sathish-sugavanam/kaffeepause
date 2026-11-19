"""The issue as the pipeline sees it, and the acceptance-criteria gate."""

import re
from dataclasses import dataclass, field

FEATURE, BUG = "feature", "bug"

_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.+\S)\s*$")
_CHECKBOX = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s+(.+\S)\s*$")
_CRITERIA = re.compile(r"acceptance criteria", re.I)
_BUG_SECTIONS = re.compile(r"(expected (behaviou?r|result)|steps to reproduce)", re.I)


@dataclass(frozen=True)
class Task:
    issue_id: str
    title: str
    description: str
    branch: str
    kind: str = FEATURE
    criteria: tuple[str, ...] = field(default=())


def _heading(line: str) -> str | None:
    """`## Title`, `**Title:**` or a short `Title:` line; None for anything else."""
    text = line.strip()
    if text.startswith("#"):
        return text.lstrip("#").strip()
    if text.startswith(("**", "__")):
        return text.strip("*_ :")
    if text.endswith(":") and len(text) <= 60 and not _ITEM.match(line):
        return text[:-1]
    return None


def _sections(description: str) -> list[tuple[str, list[str]]]:
    """The description as (heading, list items) pairs. Text before any heading
    goes under the heading ""."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in description.splitlines():
        if item := _ITEM.match(line):
            sections[-1][1].append(item.group(1))
        elif (title := _heading(line)) is not None:
            sections.append((title, []))
    return sections


def acceptance_criteria(description: str, kind: str = FEATURE) -> tuple[str, ...]:
    """Criteria from an "Acceptance criteria" section, else from checkboxes.

    A bug report may instead give "Steps to reproduce" and "Expected behaviour".
    """
    sections = _sections(description)

    def under(heading: re.Pattern[str]) -> list[str]:
        return [
            item for title, items in sections if heading.search(title) for item in items
        ]

    found = under(_CRITERIA) or [
        m.group(1) for line in description.splitlines() if (m := _CHECKBOX.match(line))
    ]
    if not found and kind == BUG:
        found = under(_BUG_SECTIONS)
    return tuple(found)


def issue_kind(labels: list[str]) -> str:
    return BUG if any(label.strip().lower() == "bug" for label in labels) else FEATURE
