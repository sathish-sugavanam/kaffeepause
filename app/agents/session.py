"""Runs one agent session inside the run container.

    python -m app.agents.session <step_dir>

The worker writes `<step_dir>/input.json`: the role, model, prompt, skills,
turn cap, remaining budget and write/Bash scope. This script runs the session
and always leaves `<step_dir>/output.json`: the raw verdict, turns, cost and
token usage. The worker validates the verdict; nothing here is trusted.

The target repo's own `.claude` settings and hooks never load
(`setting_sources=[]`). Skills come from the harness's local plugin.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.agents.hooks import WRITE_TOOLS, Scope, bash_hook, write_hook
from app.agents.roles import ROLES
from app.agents.verdicts import SCHEMAS

AGENT_JUNIT_DIR = "/tmp/kp-junit-agent"
PLUGIN_DIR = Path(__file__).resolve().parent / "plugin"
MODEL_ENV = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


def build_options(step: dict[str, Any]) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    role = ROLES[step["role"]]
    Path(AGENT_JUNIT_DIR).mkdir(parents=True, exist_ok=True)
    scope = Scope.from_dict(step["scope"])
    tools = list(role.tools)
    return ClaudeAgentOptions(
        cwd=scope.repo_dir,
        model=step["model"],
        # Keep subagents on the same model, so cost stays inside the budget we price.
        env={
            **{name: step["model"] for name in MODEL_ENV},
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
        skills=list(step.get("skills", [])),
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
        max_turns=int(step.get("max_turns") or role.max_turns),
        max_budget_usd=float(step["max_budget_usd"]),
        output_format={"type": "json_schema", "schema": SCHEMAS[role.name]},
    )


async def run_session(step: dict[str, Any]) -> dict[str, Any]:
    from claude_agent_sdk import ResultMessage, query

    final: ResultMessage | None = None
    async for message in query(prompt=step["prompt"], options=build_options(step)):
        if isinstance(message, ResultMessage):
            final = message
    if final is None:
        return {"ok": False, "subtype": "no_result", "error": "session ended early"}
    return {
        "ok": not final.is_error and final.structured_output is not None,
        "subtype": final.subtype,
        "verdict": final.structured_output,
        "summary": (final.result or "")[:20_000],
        "turns": final.num_turns,
        "cost_usd": final.total_cost_usd or 0.0,
        "usage": final.usage or {},
        "api_error_status": final.api_error_status,
        "error": "; ".join(final.errors or [])[:2_000],
    }


def main(step_dir: Path) -> int:
    step = json.loads((step_dir / "input.json").read_text())
    print(f"{step['role']} starting with {step['model']}", flush=True)
    try:
        out = asyncio.run(run_session(step))
    except Exception as exc:  # noqa: BLE001 - always leave an output.json
        out = {"ok": False, "subtype": "exception", "error": type(exc).__name__}
    (step_dir / "output.json").write_text(json.dumps(out))
    print(
        f"{step['role']} finished ok={out['ok']} subtype={out.get('subtype')} "
        f"turns={out.get('turns', 0)} cost=${out.get('cost_usd', 0.0):.2f}",
        flush=True,
    )
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
