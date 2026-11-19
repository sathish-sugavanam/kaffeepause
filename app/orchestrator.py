"""One run, driven by the worker: issue in, pull request out.

1. Clone the repository into a private folder. The token travels only in the
   environment (helpers.github.git_auth_env). Read `.kaffeepause.yml` and
   `CLAUDE.md` from the base branch there.
2. Refuse issues without acceptance criteria, before any tokens are spent.
   When a TypeSafe key is set, Jev also judges whether the criteria are
   testable and the change small enough (app.intake).
3. Start one run container, copy a token-free clone into it, and drive the
   tester → coder → security pipeline (app.pipeline) through it.
4. Check protected paths and credentials on the final commit, push, and open
   the PR, as a draft when open items are left.

The agent's working copy is never used for push or for decisions: its hooks
and git config are the agent's to change, so the worker only reads bundles.
"""

import logging
import re
import shutil
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from app.config import WorkerSettings
from app.container import RunContainer, docker_run_container
from app.errors import ErrorCode, RunFailed
from app.git import GitError, git, show_file
from app.helpers.github import create_pull_request, git_auth_env, remote_url
from app.inbox import InboxEvent
from app.intake import ReviewIssue, jev_reviewer, readiness_problem, with_kind
from app.pipeline import AgentRunner, Pipeline
from app.repo_config import FILENAME, RepoConfig, parse_repo_config
from app.report import Redactor, pull_request_body
from app.task import Task, acceptance_criteria, issue_kind

log = logging.getLogger("app.orchestrator")

StartContainer = Callable[[], AbstractContextManager[RunContainer]]
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


def read_config(private: Path) -> RepoConfig:
    """The config on the real base branch is the one that counts."""
    config = parse_repo_config(show_file(private, "HEAD", FILENAME))
    if config.base_branch != git(private, "rev-parse", "--abbrev-ref", "HEAD").strip():
        try:
            git(private, "checkout", "--quiet", "-B", config.base_branch,
                f"origin/{config.base_branch}")  # fmt: skip
        except GitError:
            raise RunFailed(
                ErrorCode.CONFIG_INVALID,
                f"{FILENAME}: base_branch `{config.base_branch}` does not exist",
            ) from None
        config = parse_repo_config(show_file(private, "HEAD", FILENAME))
    if config.unused:
        log.warning(
            "%s: %s not supported yet, ignored", FILENAME, ", ".join(config.unused)
        )
    return config


def run_dev_task(
    event: InboxEvent,
    settings: WorkerSettings,
    *,
    start_container: StartContainer,
    open_pull_request: OpenPullRequest,
    run_agent: AgentRunner | None = None,
    review_issue: ReviewIssue | None = None,
    remote: str | None = None,
) -> str:
    task = get_task_from_event(event)
    remote = remote or remote_url(settings.github_repo)
    auth = git_auth_env(settings.github_token) if settings.github_token else {}
    redact = Redactor(
        [*settings.agent_env.values(), settings.github_token, settings.typesafe_api_key]
    )
    work = Path(settings.work_dir)
    private, seed = work / f"task-{event.id}", work / f"task-{event.id}-seed"
    for folder in (private, seed):
        shutil.rmtree(folder, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    try:
        try:
            git(work, "clone", "--quiet", remote, str(private), env=auth)
        except GitError:
            raise RunFailed(ErrorCode.UPSTREAM_API, "git clone failed") from None
        config = read_config(private)
        review = review_issue(task) if review_issue else None
        if review:
            task = with_kind(task, review)
        if not task.criteria:
            raise RunFailed(
                ErrorCode.NO_CRITERIA,
                "the issue has no acceptance criteria (an 'Acceptance criteria' "
                "list or checkboxes)",
            )
        if review and (problem := readiness_problem(review)):
            raise RunFailed(ErrorCode.ISSUE_NOT_READY, problem)
        git(work, "clone", "--quiet", str(private), str(seed))
        git(seed, "checkout", "--quiet", "-b", task.branch)

        log.info("starting run for %s on %s", task.issue_id, task.branch)
        try:
            with start_container() as container:
                container.put_tree(seed, f"{container.workdir}/repo")
                pipeline = Pipeline(
                    task=task,
                    config=config,
                    private=private,
                    container=container,
                    models=settings.models,
                    budget_usd=settings.run_budget_usd,
                    claude_md=show_file(private, "HEAD", "CLAUDE.md") or "",
                    run_agent=run_agent,
                )
                result = pipeline.run()
        except RunFailed as exc:
            # Failure text quotes test output and verdicts, which an agent controls.
            raise RunFailed(exc.code, redact(exc.message)) from None

        if result.head == pipeline.base:
            raise RunFailed(ErrorCode.UNKNOWN, "the run produced no commits")
        pipeline.check_protected(result.head)
        span = f"{pipeline.base}..{result.head}"
        if redact.found_in(
            git(private, "diff", span) + git(private, "log", "--format=%B", span)
        ):
            raise RunFailed(
                ErrorCode.SECRET_IN_CHANGE,
                "the change contains a credential from the run container; not pushed",
            )
        git(private, "branch", "--force", task.branch, result.head)
        try:
            git(
                private,
                "push",
                "--quiet",
                remote,
                f"{task.branch}:{task.branch}",
                env=auth,
            )
        except GitError as exc:
            raise RunFailed(
                ErrorCode.PUSH_FAILED, f"git push exited {exc.returncode}"
            ) from None
        url = open_pull_request(
            head=task.branch,
            base=config.base_branch,
            title=f"{task.issue_id}: {task.title}",
            body=pull_request_body(task, result, redact),
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
        for folder in (private, seed):
            shutil.rmtree(folder, ignore_errors=True)


def docker_container_starter(settings: WorkerSettings) -> StartContainer:
    def start():
        return docker_run_container(settings.run_image, env=settings.agent_env)

    return start


def github_pull_request_opener(settings: WorkerSettings) -> OpenPullRequest:
    def open_pr(**kwargs: Any) -> str:
        return create_pull_request(
            settings.github_token, settings.github_repo, **kwargs
        )

    return open_pr


def issue_reviewer(settings: WorkerSettings) -> ReviewIssue | None:
    if not settings.typesafe_api_key:
        return None
    return jev_reviewer(settings.typesafe_api_key, settings.typesafe_model)
