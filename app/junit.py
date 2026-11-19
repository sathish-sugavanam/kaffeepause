"""Reads JUnit XML into one result per test ID.

`<failure>` is a failed assertion; `<error>` is a crash (syntax error, import
error, fixture blew up). The red check depends on telling them apart.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

PASSED, FAILURE, ERROR, SKIPPED = "passed", "failure", "error", "skipped"
# When one ID shows up twice (several report files), the worst status wins.
_RANK = {PASSED: 0, SKIPPED: 1, FAILURE: 2, ERROR: 3}


@dataclass(frozen=True)
class Case:
    id: str
    status: str
    message: str = ""


Results = dict[str, Case]


def case_id(classname: str, name: str) -> str:
    return f"{classname}::{name}" if classname else name


def counts(results: Results) -> dict[str, int]:
    out = dict.fromkeys(_RANK, 0)
    for case in results.values():
        out[case.status] += 1
    return out


def parse_junit(paths: list[Path]) -> Results:
    results: Results = {}
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            raise ValueError(f"{path.name} is not valid JUnit XML: {exc}") from None
        for case in root.iter("testcase"):
            result = _case_result(case)
            seen = results.get(result.id)
            if seen is None or _RANK[result.status] > _RANK[seen.status]:
                results[result.id] = result
    return results


def _case_result(case: ET.Element) -> Case:
    tid = case_id(case.get("classname", ""), case.get("name", ""))
    for tag, status in (("error", ERROR), ("failure", FAILURE), ("skipped", SKIPPED)):
        node = case.find(tag)
        if node is not None:
            message = " ".join(
                part for part in (node.get("message", ""), node.text or "") if part
            )
            return Case(tid, status, message.strip()[:4000])
    return Case(tid, PASSED)


def _normalise(tid: str) -> str:
    # `tests/test_x.py::test_a`, `tests.test_x::test_a` and `tests::test_x::test_a`
    # all name the same pytest test; other runners use similar shapes.
    tid = re.sub(r"\.(py|js|ts|tsx|jsx|rb|go|rs|java|kt)(?=::|$)", "", tid)
    return re.sub(r"[/.\\#]+|::", "::", tid).strip(":")


def match_test_id(claimed: str, ids: set[str] | list[str]) -> str | None:
    """Finds the JUnit ID an agent meant. Exact first, then one unique suffix match."""
    if claimed in ids:
        return claimed
    want = _normalise(claimed)
    by_norm = {_normalise(i): i for i in ids}
    if want in by_norm:
        return by_norm[want]
    hits = [
        i
        for n, i in by_norm.items()
        if n.endswith("::" + want) or want.endswith("::" + n)
    ]
    return hits[0] if len(hits) == 1 else None
