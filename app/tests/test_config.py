import pytest

from app.config import load_webhook_settings, load_worker_settings


def test_worker_settings_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://agents:pw@db/agents")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
    monkeypatch.setenv("GITHUB_REPO", "o/r")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    settings = load_worker_settings()
    assert settings.agent_env == {"ANTHROPIC_API_KEY": "sk-secret"}
    assert "ghp-secret" not in repr(settings) and "sk-secret" not in repr(settings)
    assert (settings.poll_interval_seconds, settings.lease_seconds) == (2.0, 3600.0)
    assert "pw" not in repr(settings)


def test_webhook_settings_defaults(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://agents:pw@db/agents")
    settings = load_webhook_settings()
    assert settings.port == 5001
    assert "s3cret" not in repr(settings)


def test_worker_needs_an_agent_credential(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        load_worker_settings()


def test_openrouter_settings_reach_the_run_container(monkeypatch):
    for name, value in {
        "DATABASE_URL": "postgresql://a:pw@db/agents",
        "REDIS_URL": "redis://redis:6379/0",
        "GITHUB_TOKEN": "ghp-secret",
        "GITHUB_REPO": "o/r",
        "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
        "ANTHROPIC_AUTH_TOKEN": "or-secret",
        "DEV_AGENT_MODEL": "nvidia/nemotron-3.5-lightning:free",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-blanked")
    settings = load_worker_settings()
    assert settings.agent_env == {
        "ANTHROPIC_AUTH_TOKEN": "or-secret",
        "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
        "ANTHROPIC_API_KEY": "",
    }
    # Models travel in each step's input.json, not the container env.
    # DEV_AGENT_MODEL is the v0 name for the coder's model.
    assert settings.models == {"coder": "nvidia/nemotron-3.5-lightning:free"}


def test_per_agent_models_and_budget(monkeypatch):
    for name, value in {
        "DATABASE_URL": "postgresql://a:pw@db/agents",
        "REDIS_URL": "redis://redis:6379/0",
        "GITHUB_TOKEN": "ghp-secret",
        "GITHUB_REPO": "o/r",
        "ANTHROPIC_API_KEY": "sk",
        "TESTER_MODEL": "claude-haiku-5-5",
        "CODER_MODEL": "claude-opus-5-5",
        "DEV_AGENT_MODEL": "ignored-when-CODER_MODEL-is-set",
        "RUN_BUDGET_USD": "4.5",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SECURITY_MODEL", raising=False)
    settings = load_worker_settings()
    assert settings.models == {
        "tester": "claude-haiku-5-5",
        "coder": "claude-opus-5-5",
    }
    assert settings.run_budget_usd == 4.5
