"""One run, driven by the worker: issue in, pull request out.

1. Clone the repository into a private folder, with the token. Read
   `.kaffeepause.yml` and `CLAUDE.md` from the base branch there.
2. Refuse issues without acceptance criteria, before any tokens are spent.
3. Clone the private copy into the shared run folder, without the token.
4. Start one run container and drive the tester → coder → security pipeline
   (app.pipeline) through it.
5. Check protected paths on the final commit, push, and open the PR, as a
   draft when open items are left.

The agent's working copy is never used for push or for decisions: its hooks
and git config are the agent's to change, so the worker only reads bundles.
"""

import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from app.config import WorkerSettings
from app.container import RunContainer, docker_run_container
from app.errors import ErrorCode, RunFailed
from app.helpers.github import authed_remote_url, create_pull_request
from app.inbox import InboxEvent
from app.pipeline import AgentRunner, Pipeline, PipelineResult
from app.repo_config import FILENAME, parse_repo_config
from app.task import Task, acceptance_criteria, issue_kind

log = logging.getLogger("app.orchestrator")

StartContainer = Callable[[Path], AbstractContextManager[RunContainer]]
OpenPullRequest = Callable[..., str]


def get_task_from_event(event: InboxEvent) -> Task:
    data: dict[str, Any] = event.payload.get("data") or {}
    title = data.get("title") or "Untitled issue"
    slug = (
        re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or "change"
    )
    labels = [
        str(label.get("name", ""))
        for label in data.get("labels") or []
        if isinstance(label, dict)
    ]
    kind = issue_kind(labels)
    description = data.get("description") or ""
    return Task(
        issue_id=event.issue_id or str(event.id),
        title=title,
        description=description,
        branch=f"agent/{event.issue_id}-{slug}",
        kind=kind,
        criteria=acceptance_criteria(description, kind),
    )


def git(repo: Path, *args: str) -> str:
    # Hooks are off for every git call the worker makes.
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def show_file(repo: Path, ref: str, path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else None


def run_dev_task(
    event: InboxEvent,
    settings: WorkerSettings,
    *,
    start_container: StartContainer,
    open_pull_request: OpenPullRequest,
    run_agent: AgentRunner | None = None,
    remote_url: str | None = None,
) -> str:
    task = get_task_from_event(event)
    remote = remote_url or authed_remote_url(
        settings.github_token, settings.github_repo
    )
    private = Path(settings.work_dir) / f"task-{event.id}"
    run_dir = Path(settings.runs_dir) / f"task-{event.id}"
    for folder in (private, run_dir):
        shutil.rmtree(folder, ignore_errors=True)
    private.parent.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True)

    try:
        try:
            subprocess.run(
                ["git", "clone", "--quiet", remote, str(private)],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            raise RunFailed(ErrorCode.UPSTREAM_API, "git clone failed") from None
        config = parse_repo_config(show_file(private, "HEAD", FILENAME))
        current = git(private, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if config.base_branch != current:
            # The config on the real base branch is the one that counts.
            git(private, "checkout", "--quiet", "-B", config.base_branch,
                f"origin/{config.base_branch}")  # fmt: skip
            config = parse_repo_config(show_file(private, "HEAD", FILENAME))
        if config.unused:
            log.warning(
                "%s: %s not supported yet, ignored", FILENAME, ", ".join(config.unused)
            )
        if not task.criteria:
            raise RunFailed(
                ErrorCode.NO_CRITERIA,
                "the issue has no acceptance criteria (an 'Acceptance criteria' "
                "list or checkboxes)",
            )

        git(run_dir, "clone", "--quiet", str(private), "repo")
        git(run_dir / "repo", "checkout", "--quiet", "-b", task.branch)

        log.info("starting run for %s on %s", task.issue_id, task.branch)
        with start_container(run_dir) as container:
            pipeline = Pipeline(
                task=task,
                config=config,
                run_dir=run_dir,
                private=private,
                container=container,
                models=settings.models,
                budget_usd=settings.run_budget_usd,
                claude_md=show_file(private, "HEAD", "CLAUDE.md") or "",
                run_agent=run_agent,
            )
            result = pipeline.run()

        if result.head == pipeline.base:
            raise RunFailed(ErrorCode.UNKNOWN, "the run produced no commits")
        pipeline.check_protected(result.head)
        git(private, "branch", "--force", task.branch, result.head)
        try:
            git(private, "push", "--quiet", remote, f"{task.branch}:{task.branch}")
        except subprocess.CalledProcessError as exc:
            # Never put exc in the message: its command line holds the token.
            raise RunFailed(
                ErrorCode.PUSH_FAILED, f"git push exited {exc.returncode}"
            ) from None
        url = open_pull_request(
            head=task.branch,
            base=config.base_branch,
            title=f"{task.issue_id}: {task.title}",
            body=pull_request_body(task, result),
            draft=not result.ready,
        )
        log.info(
            "opened %s pull request %s for %s: turns=%s cost=$%.2f",
            "ready" if result.ready else "draft",
            url,
            task.issue_id,
            result.turns,
            result.cost_usd,
        )
        return url
    finally:
        shutil.rmtree(private, ignore_errors=True)
        shutil.rmtree(run_dir, ignore_errors=True)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def pull_request_body(task: Task, result: PipelineResult) -> str:
    parts: list[str] = []
    if not result.ready:
        parts.append(
            "> [!WARNING]\n> Draft: the harness ran out of feedback returns with "
            "these items still open.\n\n"
            + "\n".join(f"- {_cell(i)}" for i in result.open_items)
        )
    summary = (result.coder_verdict or {}).get("summary", "").strip()
    parts.append(summary or "No summary from the coder.")

    rows = "\n".join(
        f"| {_cell(criterion)} | `{_cell(tid)}` |"
        for tid, criterion in sorted(result.tests.items(), key=lambda kv: kv[1])
    )
    criteria = "\n".join(f"{i}. {_cell(c)}" for i, c in enumerate(task.criteria, 1))
    parts.append(
        f"### Acceptance criteria\n\n{criteria}\n\n"
        f"| Criterion | Test |\n| --- | --- |\n{rows}"
    )

    findings = (result.security_verdict or {}).get("findings", [])
    if findings:
        lines = "\n".join(
            f"| {f['severity']} | {'yes' if f['blocking'] else 'no'} | "
            f"`{_cell(f['file'])}:{f['line']}` | {_cell(f['title'])} |"
            for f in findings
        )
        sec = f"| Severity | Blocking | Where | Finding |\n| --- | --- | --- | --- |\n{lines}"
    elif result.security_verdict is not None:
        sec = "No findings."
    else:
        sec = "Not reached."
    scans = ", ".join(
        f"{name}: {'ran' if where.startswith('/') else where}"
        for name, where in sorted(result.scans.items())
    )
    parts.append(
        f"### Security review\n\n{sec}" + (f"\n\nScanners: {scans}." if scans else "")
    )

    steps = "\n".join(
        f"| {s.name} | {s.model or '-'} | {s.turns or '-'} | "
        f"{f'${s.cost_usd:.2f}' if s.kind == 'agent' else '-'} | "
        f"{'ok' if s.ok else _cell(s.note) or 'failed'} |"
        for s in result.steps
    )
    parts.append(
        "### Run\n\n| Step | Model | Turns | Cost | Result |\n| --- | --- | --- | --- | --- |\n"
        f"{steps}\n\nTotal: {result.turns} turns, about ${result.cost_usd:.2f}. "
        f"Feedback returns used: {result.returns_used}."
    )
    parts.append(
        f"---\nLinear issue: {task.issue_id}.\n\n🤖 Opened by the Kaffeepause agents."
    )
    return "\n\n".join(parts)


def docker_container_starter(settings: WorkerSettings) -> StartContainer:
    def start(_run_dir: Path):
        return docker_run_container(
            settings.run_image,
            env=settings.agent_env,
            runs_volume=settings.runs_volume,
            runs_dir=settings.runs_dir,
        )

    return start


def github_pull_request_opener(settings: WorkerSettings) -> OpenPullRequest:
    def open_pr(**kwargs: Any) -> str:
        return create_pull_request(
            settings.github_token, settings.github_repo, **kwargs
        )

    return open_pr
