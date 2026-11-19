"""Runs one agent session inside the run container.

    python -m app.agents.session <step_dir>

The worker writes `<step_dir>/input.json` and this script always leaves
`<step_dir>/output.json` (see app.agents.step for both shapes). The worker
validates the verdict; nothing here is trusted.

The target repo's own `.claude` settings and hooks never load
(`setting_sources=[]`). Skills come from the harness's local plugin.
"""

import asyncio
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query

from app.agents.hooks import WRITE_TOOLS, bash_hook, write_hook
from app.agents.roles import ROLES
from app.agents.step import ERROR_LIMIT, SUMMARY_LIMIT, StepInput, StepOutput
from app.agents.verdicts import SCHEMAS

AGENT_JUNIT_DIR = "/tmp/kp-junit-agent"
PLUGIN_DIR = Path(__file__).resolve().parent / "plugin"
MODEL_ENV = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


def build_options(step: StepInput) -> ClaudeAgentOptions:
    role = ROLES[step.role]
    scope = step.scope
    tools = list(role.tools)
    return ClaudeAgentOptions(
        cwd=scope.repo_dir,
        model=step.model,
        # Keep subagents on the same model, so cost stays inside the budget we price.
        env={
            **{name: step.model for name in MODEL_ENV},
            # So the agent can run the repo's test commands as written.
            "KP_JUNIT": AGENT_JUNIT_DIR,
        },
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": role.instructions,
        },
        tools=tools,
        allowed_tools=tools,
        permission_mode="dontAsk",
        setting_sources=[],
        plugins=[{"type": "local", "path": str(PLUGIN_DIR)}],
        skills=list(step.skills),
        hooks={
            "PreToolUse": [
                HookMatcher(
                    matcher=WRITE_TOOLS, hooks=[write_hook(scope)], on_failure="block"
                ),
                HookMatcher(
                    matcher="Bash", hooks=[bash_hook(scope)], on_failure="block"
                ),
            ]
        },
        max_turns=step.max_turns,
        max_budget_usd=step.max_budget_usd,
        output_format={"type": "json_schema", "schema": SCHEMAS[role.name]},
    )


async def run_session(step: StepInput) -> StepOutput:
    final: ResultMessage | None = None
    async for message in query(prompt=step.prompt, options=build_options(step)):
        if isinstance(message, ResultMessage):
            final = message
    if final is None:
        return StepOutput(subtype="no_result", error="session ended early")
    return StepOutput(
        subtype=final.subtype,
        verdict=final.structured_output,
        summary=(final.result or "")[:SUMMARY_LIMIT],
        turns=final.num_turns,
        cost_usd=final.total_cost_usd or 0.0,
        usage=final.usage or {},
        api_error_status=final.api_error_status,
        error="; ".join(final.errors or [])[:ERROR_LIMIT],
    )


def main(step_dir: Path) -> int:
    step = StepInput.from_json((step_dir / "input.json").read_text())
    Path(AGENT_JUNIT_DIR).mkdir(parents=True, exist_ok=True)
    print(f"{step.role} starting with {step.model}", flush=True)
    try:
        out = asyncio.run(run_session(step))
    except Exception as exc:  # noqa: BLE001 - always leave an output.json
        out = StepOutput(subtype="exception", error=type(exc).__name__)
    (step_dir / "output.json").write_text(out.to_json())
    print(
        f"{step.role} finished subtype={out.subtype} turns={out.turns} "
        f"cost=${out.cost_usd:.2f}",
        flush=True,
    )
    return 0 if out.verdict is not None else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
