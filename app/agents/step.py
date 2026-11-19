"""The contract between the worker and one agent session in the run container.

The worker writes a StepInput as `input.json`; the session writes a StepOutput
as `output.json`. Both sides use these types, so the shape lives in one place.
StepOutput.parse treats its input as untrusted: the container wrote it.
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from app.agents.hooks import Scope

SUMMARY_LIMIT = 20_000
ERROR_LIMIT = 2_000


@dataclass(frozen=True)
class StepInput:
    role: str
    model: str
    prompt: str
    skills: tuple[str, ...]
    max_turns: int
    max_budget_usd: float
    scope: Scope

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> StepInput:
        raw = json.loads(text)
        return cls(
            **{
                **raw,
                "skills": tuple(raw["skills"]),
                "scope": Scope.from_dict(raw["scope"]),
            }
        )


@dataclass(frozen=True)
class StepOutput:
    subtype: str
    verdict: Any = None
    summary: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    api_error_status: int | None = None
    error: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def parse(cls, text: str | None) -> StepOutput:
        """Never raises: anything malformed becomes an output with no verdict."""
        if text is None:
            return cls(subtype="no_output")
        try:
            raw = json.loads(text)
        except ValueError:
            return cls(subtype="bad_output")
        if not isinstance(raw, dict):
            return cls(subtype="bad_output")
        usage = raw.get("usage")
        return cls(
            subtype=str(raw.get("subtype", ""))[:60],
            verdict=raw.get("verdict"),
            summary=str(raw.get("summary", ""))[:SUMMARY_LIMIT],
            turns=_number(raw.get("turns"), int),
            cost_usd=max(_number(raw.get("cost_usd"), float), 0.0),
            usage=usage if isinstance(usage, dict) else {},
            api_error_status=_number(raw.get("api_error_status"), int) or None,
            error=str(raw.get("error", ""))[:ERROR_LIMIT],
        )


def _number[T: (int, float)](value: Any, kind: type[T]) -> T:
    if isinstance(value, bool):
        return kind(0)
    try:
        return kind(value)
    except TypeError, ValueError, OverflowError:
        return kind(0)
