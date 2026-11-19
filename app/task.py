"""The issue as the pipeline sees it, and the acceptance-criteria gate."""

import re
from dataclasses import dataclass, field

FEATURE, BUG = "feature", "bug"

_HEADING = re.compile(r"^\s*(?:#{1,6}\s*|\*\*|__)?\s*(.+?)\s*(?:\*\*|__)?\s*:?\s*$")
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


def _section_items(lines: list[str], heading: re.Pattern[str]) -> list[str]:
    items: list[str] = []
    inside = False
    for line in lines:
        if not line.strip():
            continue
        item = _ITEM.match(line)
        if item and inside:
            items.append(item.group(1))
            continue
        is_heading = line.lstrip().startswith(("#", "**", "__")) or (
            not item and line.rstrip().endswith(":")
        )
        if is_heading:
            title = _HEADING.match(line)
            inside = bool(title and heading.search(title.group(1)))
        elif inside and not item and items:
            inside = False
    return items


def acceptance_criteria(description: str, kind: str = FEATURE) -> tuple[str, ...]:
    """Criteria from an "Acceptance criteria" section, else from checkboxes.

    A bug report may instead give "Steps to reproduce" and "Expected behaviour".
    """
    lines = description.splitlines()
    items = _section_items(lines, _CRITERIA)
    if not items:
        items = [m.group(1) for line in lines if (m := _CHECKBOX.match(line))]
    if not items and kind == BUG:
        items = _section_items(lines, _BUG_SECTIONS)
    return tuple(items)


def issue_kind(labels: list[str]) -> str:
    return BUG if any(label.strip().lower() == "bug" for label in labels) else FEATURE
