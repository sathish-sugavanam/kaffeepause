"""Settings for the agent services, read from the environment once at boot."""

import os
from dataclasses import dataclass, field

# Credentials the run container may receive. Whichever are set get passed on.
# ANTHROPIC_AUTH_TOKEN with ANTHROPIC_BASE_URL is how a gateway such as OpenRouter is used.
AGENT_CREDENTIALS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
)
# Other settings passed through to the run container when set.
AGENT_SETTINGS = ("ANTHROPIC_BASE_URL",)
# Model per agent; unset ones use the role's default (app.agents.roles).
MODEL_SETTINGS = {
    "tester": "TESTER_MODEL",
    "coder": "CODER_MODEL",
    "security": "SECURITY_MODEL",
}


def agent_env() -> dict[str, str]:
    env = {
        k: os.environ[k]
        for k in AGENT_CREDENTIALS + AGENT_SETTINGS
        if os.environ.get(k)
    }
    if "ANTHROPIC_AUTH_TOKEN" in env:
        # A gateway token must not be shadowed by an API key (OpenRouter's instructions).
        env["ANTHROPIC_API_KEY"] = ""
    return env


def models_from_env() -> dict[str, str]:
    models = {
        role: os.environ[k] for role, k in MODEL_SETTINGS.items() if os.environ.get(k)
    }
    # DEV_AGENT_MODEL is the v0 name for the coder's model.
    if "coder" not in models and os.environ.get("DEV_AGENT_MODEL"):
        models["coder"] = os.environ["DEV_AGENT_MODEL"]
    return models


@dataclass(frozen=True)
class WebhookSettings:
    linear_webhook_secret: str = field(repr=False)
    database_url: str = field(repr=False)
    port: int = 5001


@dataclass(frozen=True)
class WorkerSettings:
    database_url: str = field(repr=False)
    broker_url: str = field(repr=False)
    github_token: str = field(default="", repr=False)
    github_repo: str = ""
    agent_env: dict[str, str] = field(default_factory=dict, repr=False)
    models: dict[str, str] = field(default_factory=dict)
    run_budget_usd: float = 10.0
    poll_interval_seconds: float = 2.0
    lease_seconds: float = 3600.0
    run_image: str = "kaffeepause"
    work_dir: str = "/work"  # the worker's private clones; never shared


def load_webhook_settings() -> WebhookSettings:
    return WebhookSettings(
        linear_webhook_secret=os.environ["LINEAR_WEBHOOK_SECRET"],
        database_url=os.environ["DATABASE_URL"],
        port=int(os.environ.get("PORT", "5001")),
    )


def load_worker_settings() -> WorkerSettings:
    if not any(os.environ.get(k) for k in AGENT_CREDENTIALS):
        raise RuntimeError(f"set one of {', '.join(AGENT_CREDENTIALS)} for the agents")
    return WorkerSettings(
        database_url=os.environ["DATABASE_URL"],
        broker_url=os.environ["REDIS_URL"],
        github_token=os.environ["GITHUB_TOKEN"],
        github_repo=os.environ["GITHUB_REPO"],
        agent_env=agent_env(),
        models=models_from_env(),
        run_budget_usd=float(os.environ.get("RUN_BUDGET_USD", "10")),
        poll_interval_seconds=float(
            os.environ.get("WORKER_POLL_INTERVAL_SECONDS", "2")
        ),
        lease_seconds=float(os.environ.get("WORKER_LEASE_SECONDS", "3600")),
    )
