"""The pull request body, and the guards on what agents may put in it.

Agent-written text (summaries, findings, test names) is untrusted. Before it
reaches GitHub, every known secret value is redacted and @mentions are
defused, so an agent can neither leak the run container's credentials nor
ping arbitrary GitHub users from a PR body.
"""

import re

from app.pipeline import PipelineResult
from app.task import Task

MIN_SECRET_LENGTH = 8  # shorter values would redact ordinary words
_MENTION = re.compile(r"@(?=[A-Za-z0-9-])")


class Redactor:
    def __init__(self, secrets: list[str] | tuple[str, ...]) -> None:
        self.secrets = sorted(
            {s for s in secrets if len(s) >= MIN_SECRET_LENGTH}, key=len, reverse=True
        )

    def found_in(self, text: str) -> bool:
        return any(s in text for s in self.secrets)

    def __call__(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        return text


def pull_request_body(task: Task, result: PipelineResult, redact: Redactor) -> str:
    def untrusted(text: str) -> str:
        return _MENTION.sub("@\u200b", redact(str(text)))

    def cell(text: str) -> str:
        return untrusted(text).replace("|", "\\|").replace("\n", " ").strip()

    parts: list[str] = []
    if not result.ready:
        parts.append(
            "> [!WARNING]\n> Draft: the harness ran out of feedback returns with "
            "these items still open.\n\n"
            + "\n".join(f"- {cell(i)}" for i in result.open_items)
        )
    summary = (result.coder_verdict or {}).get("summary", "").strip()
    parts.append(untrusted(summary) or "No summary from the coder.")

    criteria = "\n".join(f"{i}. {cell(c)}" for i, c in enumerate(task.criteria, 1))
    rows = "\n".join(
        f"| {cell(criterion)} | `{cell(tid)}` |"
        for tid, criterion in sorted(result.tests.items(), key=lambda kv: kv[1])
    )
    parts.append(
        f"### Acceptance criteria\n\n{criteria}\n\n"
        f"| Criterion | Test |\n| --- | --- |\n{rows}"
    )

    if result.security_verdict is None:
        security = "Not reached."
    elif findings := result.security_verdict["findings"]:
        lines = "\n".join(
            f"| {f['severity']} | {'yes' if f['blocking'] else 'no'} | "
            f"`{cell(f['file'])}:{f['line']}` | {cell(f['title'])} |"
            for f in findings
        )
        security = (
            "| Severity | Blocking | Where | Finding |\n| --- | --- | --- | --- |\n"
            + lines
        )
    else:
        security = "No findings."
    if result.scans:
        ran = ", ".join(
            f"{s.name}: {'ran' if s.report else cell(s.note)}" for s in result.scans
        )
        security += f"\n\nScanners: {ran}."
    parts.append(f"### Security review\n\n{security}")

    steps = "\n".join(
        f"| {s.name} | {s.model or '-'} | {s.turns or '-'} | "
        f"{f'${s.cost_usd:.2f}' if s.kind == 'agent' else '-'} | "
        f"{'ok' if s.ok else cell(s.note) or 'failed'} |"
        for s in result.steps
    )
    parts.append(
        "### Run\n\n| Step | Model | Turns | Cost | Result |\n"
        f"| --- | --- | --- | --- | --- |\n{steps}\n\n"
        f"Total: {result.turns} turns, about ${result.cost_usd:.2f}. "
        f"Feedback returns used: {result.returns_used}."
    )
    parts.append(
        f"---\nLinear issue: {task.issue_id}.\n\n🤖 Opened by the Kaffeepause agents."
    )
    return "\n\n".join(parts)
