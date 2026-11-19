"""Verdict JSON Schemas, one per agent.

The session passes the schema as `output_format`, so the model is asked for
exactly this shape. The worker validates again, because the run container is
not trusted: an invalid verdict fails the step with BAD_VERDICT.
"""

from typing import Any

import jsonschema

from app.agents.roles import CODER, SECURITY, TESTER


def _obj(**props: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}

TESTER_VERDICT = _obj(
    tests={
        "type": "array",
        "description": "Every test you added in this step.",
        "items": _obj(
            id={
                "type": "string",
                "minLength": 1,
                "description": "Test ID, e.g. tests/test_export.py::test_csv_header",
            },
            criterion={"type": "string", "description": "The criterion it covers."},
            expected_failure={
                "type": "string",
                "description": "A substring of the assertion message the test fails "
                "with on the current code. Empty in a gap review.",
            },
        ),
    },
    contract={
        "type": "string",
        "description": "The interface the coder must implement: names, signatures, "
        "routes, status codes, error behaviour.",
    },
    gaps={**_STRS, "description": "Gaps or ambiguities you found."},
    cannot_reproduce={
        "type": "boolean",
        "description": "True only for a bug you could not reproduce.",
    },
    notes=_STR,
)

CODER_VERDICT = _obj(
    summary={"type": "string", "minLength": 1},
    files_changed=_STRS,
    disputes={
        "type": "array",
        "items": _obj(test_id=_STR, reason=_STR, criterion=_STR),
    },
)

SECURITY_VERDICT = _obj(
    findings={
        "type": "array",
        "items": _obj(
            severity={"enum": ["critical", "high", "medium", "low", "info"]},
            file=_STR,
            line={"type": "integer", "minimum": 0},
            title=_STR,
            blocking={"type": "boolean"},
            fix_hint=_STR,
        ),
    },
    summary=_STR,
)

SCHEMAS = {TESTER: TESTER_VERDICT, CODER: CODER_VERDICT, SECURITY: SECURITY_VERDICT}


def verdict_error(role: str, verdict: Any) -> str | None:
    """None when the verdict is valid, else a short reason."""
    try:
        jsonschema.validate(verdict, SCHEMAS[role])
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        return f"{where}: {exc.message[:300]}"
    return None
