"""The tester → coder → security pipeline (PRD sections 3 and 5), run by the worker.

    setup + baseline ─ red ─▶ BASELINE_RED
    tester ─▶ red check ─(fail, 1 retry)─▶ RED_CHECK_FAILED
    coder ─▶ review: dispute? green? gap review (once)? security?
          ─ objection ─▶ back (a dispute goes to the tester, the rest to the coder)
          ─ none ─────▶ ready
    Each objection uses one of MAX_RETURNS. With none left: a draft PR.

The routing is plain Python. Agents run in the run container; their work comes
back only as git bundles, which this module fetches into the worker's private
clone and inspects there. Files cross the container boundary only through the
RunContainer API, never through a shared folder.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import prompts
from app.agents.hooks import scope_for
from app.agents.roles import CODER, ROLES, SECURITY, TESTER, skill_name
from app.agents.step import StepInput, StepOutput
from app.agents.verdicts import verdict_error
from app.checks import (
    CheckOutcome,
    SuiteRun,
    baseline_check,
    gap_check,
    green_check,
    red_check,
)
from app.container import RunContainer, shell
from app.errors import ErrorCode, RunFailed
from app.git import GitError, changed_files, git, is_ancestor
from app.junit import PASSED, counts, match_test_id, parse_junit
from app.repo_config import RepoConfig, matches_any
from app.scanners import Scan, run_scans
from app.task import BUG, Task

log = logging.getLogger("app.pipeline")

MAX_RETURNS = 2
DEFAULT_BUDGET_USD = 10.0
MIN_STEP_BUDGET_USD = 0.05

AgentRunner = Callable[[StepInput], StepOutput]


@dataclass
class StepRecord:
    name: str
    kind: str  # "agent" or "check"
    started: float
    ended: float = 0.0
    ok: bool = False
    model: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    layers: dict[str, dict[str, int]] = field(default_factory=dict)
    verdict: Any = None
    note: str = ""


@dataclass
class PipelineResult:
    ready: bool
    head: str
    tester_verdict: dict
    tests: dict[str, str]  # JUnit test ID -> criterion
    coder_verdict: dict | None
    security_verdict: dict | None
    open_items: list[str]
    scans: list[Scan]
    steps: list[StepRecord]
    returns_used: int

    @property
    def cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.steps)

    @property
    def turns(self) -> int:
        return sum(s.turns for s in self.steps)


@dataclass(frozen=True)
class Return:
    """An objection to the coder's change: what is open, and what to tell them."""

    items: list[str]
    feedback: str
    dispute: bool = False  # goes back to the tester instead


def container_agent_runner(container: RunContainer) -> AgentRunner:
    """Runs app.agents.session in the container, passing files via its API."""
    n = 0

    def run(step: StepInput) -> StepOutput:
        nonlocal n
        n += 1
        step_dir = f"{container.workdir}/steps/{n:02d}-{step.role}"
        container.put(step_dir, {"input.json": step.to_json().encode()})
        container.exec(
            [*container.python, "-m", "app.agents.session", step_dir], stream=True
        )
        raw = container.get(step_dir).get("output.json")
        return StepOutput.parse(raw.decode(errors="replace") if raw else None)

    return run


class Pipeline:
    def __init__(
        self,
        *,
        task: Task,
        config: RepoConfig,
        private: Path,
        container: RunContainer,
        models: dict[str, str] | None = None,
        budget_usd: float = DEFAULT_BUDGET_USD,
        claude_md: str = "",
        run_agent: AgentRunner | None = None,
    ) -> None:
        self.task, self.config, self.container = task, config, container
        self.private = private
        self.repo = f"{container.workdir}/repo"
        self.models = {
            r: (models or {}).get(r) or ROLES[r].default_model for r in ROLES
        }
        self.budget_usd = budget_usd
        self.claude_md = claude_md
        self.run_agent = run_agent or container_agent_runner(container)
        self.base = git(private, "rev-parse", "HEAD").strip()
        self.head = self.tester_sha = self.base
        self.tester_verdict: dict = {}
        self.coder_verdict: dict | None = None
        self.security_verdict: dict | None = None
        self.tests: dict[str, str] = {}
        self.required: set[str] = set()  # tests that must keep passing
        self.known: set[str] = set()  # test IDs before the gap review
        self.scans: list[Scan] = []
        self.gap_done = False
        self.returns_left = MAX_RETURNS
        self.steps: list[StepRecord] = []
        self._n = 0

    # ---- the routing ----------------------------------------------------

    def run(self) -> PipelineResult:
        self._baseline()
        self._tester_round()
        feedback = ""
        while True:
            self._coder(feedback)
            back = self._review()
            if back is None:
                return self._result(ready=True)
            if self.returns_left == 0:
                return self._result(ready=False, open_items=back.items)
            self.returns_left -= 1
            log.info("returning (%s left): %s", self.returns_left, _first(back.items))
            if back.dispute:
                self._reset_to(self.tester_sha)
                self._tester_round(dispute=back.feedback)
                feedback = ""
            else:
                feedback = back.feedback

    def _review(self) -> Return | None:
        """The gates after each coder pass, in order. The first objection wins."""
        return (
            self._disputes()
            or self._green()
            or self._gap_review()
            or self._security_review()
        )

    def _result(self, *, ready: bool, open_items: list[str] = ()) -> PipelineResult:
        return PipelineResult(
            ready=ready,
            head=self.head,
            tester_verdict=self.tester_verdict,
            tests=self.tests,
            coder_verdict=self.coder_verdict,
            security_verdict=self.security_verdict,
            open_items=list(open_items),
            scans=self.scans,
            steps=self.steps,
            returns_used=MAX_RETURNS - self.returns_left,
        )

    # ---- the steps ------------------------------------------------------

    def _baseline(self) -> None:
        for command in self.config.setup:
            res = self.container.exec(
                shell(command), cwd=self.repo, timeout=self.config.timeout_seconds
            )
            if res.exit_code != 0:
                raise RunFailed(
                    ErrorCode.BASELINE_RED,
                    f"setup `{command}` exited {res.exit_code}"
                    + (" (timed out)" if res.timed_out else ""),
                )
        self.baseline = self._suite("baseline")
        outcome = baseline_check(self.baseline)
        self._record_check("baseline", self.baseline, outcome)
        if not outcome.ok:
            raise RunFailed(ErrorCode.BASELINE_RED, _first(outcome.problems))
        self.required = {
            tid for tid, case in self.baseline.results.items() if case.status == PASSED
        }

    def _tester_round(self, *, dispute: str = "") -> None:
        skills = ["reproduce-bug" if self.task.kind == BUG else "write-failing-tests"]
        start = self.head
        feedback = ""
        problems: list[str] = []
        for _attempt in (1, 2):
            verdict = self._agent(
                TESTER,
                prompts.tester(
                    self.task,
                    self.config,
                    self.claude_md,
                    feedback=feedback,
                    dispute=dispute,
                ),
                skills,
            )
            if verdict["cannot_reproduce"]:
                raise RunFailed(ErrorCode.CANNOT_REPRODUCE, verdict["notes"][:500])
            self._commit(f"{self.task.issue_id}: failing tests")
            self._check_tester_scope(start)
            run = self._suite("red")
            expected = {t["id"]: t["expected_failure"] for t in verdict["tests"]}
            outcome, matched = red_check(self.baseline, run, expected)
            self._record_check("red", run, outcome)
            if outcome.ok:
                self.tester_verdict = verdict
                self.tester_sha = self.head
                criteria = {t["id"]: t["criterion"] for t in verdict["tests"]}
                self.tests = {tid: criteria[c] for c, tid in matched.items()}
                self.required |= set(matched.values())
                return
            problems, feedback = outcome.problems, outcome.feedback()
        raise RunFailed(ErrorCode.RED_CHECK_FAILED, _first(problems))

    def _coder(self, feedback: str) -> None:
        self.coder_verdict = self._agent(
            CODER,
            prompts.coder(
                self.task,
                self.config,
                self.claude_md,
                self.tester_verdict,
                feedback=feedback,
            ),
            ["implement-to-contract"],
        )
        self._commit(f"{self.task.issue_id}: implement {self.task.title}")
        self._check_coder_scope()

    def _disputes(self) -> Return | None:
        items = [
            f"Disputed test {d['test_id']}: {d['reason']} (criterion: {d['criterion']})"
            for d in self.coder_verdict["disputes"]
        ]
        return Return(items, _bullets(items), dispute=True) if items else None

    def _green(self) -> Return | None:
        run = self._suite("green", lint=True)
        outcome = green_check(run, self.required, run.lint_failures)
        self._record_check("green", run, outcome)
        self.known = set(run.results)
        if outcome.ok:
            return None
        return Return(outcome.problems, "Green check failed:\n" + outcome.feedback())

    def _gap_review(self) -> Return | None:
        if self.gap_done:
            return None
        self.gap_done = True
        coder_sha = self.head
        verdict = self._agent(
            TESTER,
            prompts.gap_review(
                self.task,
                self.config,
                self.claude_md,
                self.tester_verdict["tests"],
                self._diff(self.tester_sha, coder_sha),
            ),
            ["test-gap-review"],
        )
        self._commit(f"{self.task.issue_id}: gap-review tests")
        self._check_tester_scope(coder_sha)
        self.tester_sha = self.head
        run = self._suite("gap")
        outcome, new_ids = gap_check(self.known, run)
        self._record_check("gap", run, outcome)
        for tid in new_ids:
            self.tests[tid] = "gap review"
        for t in verdict["tests"]:
            if tid := match_test_id(t["id"], new_ids):
                self.tests[tid] = t["criterion"] or "gap review"
        self.required |= new_ids
        if outcome.ok:
            return None
        return Return(
            outcome.problems,
            "The tester's gap review added tests that fail against your code:\n"
            + outcome.feedback(),
        )

    def _security_review(self) -> Return | None:
        self.scans = run_scans(
            self.container,
            repo=self.repo,
            out_dir=self._dir("scans", "security"),
            base=self.base,
            changed=changed_files(self.private, self.base, self.head),
            present=changed_files(self.private, self.base, self.head, deleted=False),
        )
        self._restore_tree()
        self.security_verdict = self._agent(
            SECURITY,
            prompts.security(
                self.task, self.config, self._diff(self.base, self.head), self.scans
            ),
            ["security-review"],
        )
        items = [
            _finding(f) for f in self.security_verdict["findings"] if f["blocking"]
        ]
        if not items:
            return None
        return Return(items, "Blocking security findings:\n" + _bullets(items))

    # ---- agents ---------------------------------------------------------

    def _agent(self, role: str, prompt: str, skills: list[str]) -> dict:
        remaining = self.budget_usd - sum(s.cost_usd for s in self.steps)
        if remaining < MIN_STEP_BUDGET_USD:
            raise RunFailed(
                ErrorCode.BUDGET_EXCEEDED, f"${self.budget_usd:.2f} run budget spent"
            )
        step = StepInput(
            role=role,
            model=self.models[role],
            prompt=prompt,
            skills=tuple(skill_name(s) for s in skills),
            max_turns=ROLES[role].max_turns,
            max_budget_usd=round(remaining, 4),
            scope=scope_for(role, self.repo, self.config),
        )
        record = StepRecord(
            name=role, kind="agent", started=time.time(), model=step.model
        )
        self.steps.append(record)
        out = self.run_agent(step)
        record.ended = time.time()
        record.turns, record.cost_usd, record.usage = out.turns, out.cost_usd, out.usage
        record.verdict = out.verdict
        log.info(
            "%s step: subtype=%s turns=%s cost=$%.2f",
            role,
            out.subtype,
            out.turns,
            out.cost_usd,
        )
        if out.subtype == "error_max_budget_usd":
            raise RunFailed(ErrorCode.BUDGET_EXCEEDED, f"during the {role} step")
        if out.api_error_status:
            raise RunFailed(
                ErrorCode.UPSTREAM_API, f"{role}: API returned {out.api_error_status}"
            )
        if out.verdict is None:
            raise RunFailed(
                ErrorCode.BAD_VERDICT, f"{role} ended without a verdict ({out.subtype})"
            )
        if reason := verdict_error(role, out.verdict):
            raise RunFailed(ErrorCode.BAD_VERDICT, f"{role}: {reason}")
        record.ok = True
        return out.verdict

    # ---- checks ---------------------------------------------------------

    def _suite(self, label: str, *, lint: bool = False) -> SuiteRun:
        # A restart stops anything an agent left running in the background.
        self.container.reset()
        run = SuiteRun({})
        timeout = self.config.timeout_seconds
        for layer, command in self.config.test.items():
            jdir = self._dir("junit", f"{label}-{layer}")
            self.container.put(jdir, {})
            res = self.container.exec(
                shell(command), cwd=self.repo, env={"KP_JUNIT": jdir}, timeout=timeout
            )
            if res.timed_out:
                run.problems.append(f"`{layer}` tests timed out after {timeout:.0f}s")
                continue
            reports = {
                name: data
                for name, data in self.container.get(jdir).items()
                if self.config.is_junit_report(name)
            }
            if not reports:
                run.problems.append(
                    f"`{layer}` tests wrote no JUnit XML (exit {res.exit_code}). "
                    f"Output tail:\n{res.output[-1500:]}"
                )
                continue
            try:
                results = parse_junit(reports)
            except ValueError as exc:
                run.problems.append(f"`{layer}`: {exc}")
                continue
            run.layers[layer] = counts(results)
            run.results.update(results)
        for command in self.config.lint if lint else ():
            res = self.container.exec(shell(command), cwd=self.repo, timeout=timeout)
            if res.exit_code != 0:
                run.lint_failures.append(
                    f"lint `{command}` exited {res.exit_code}:\n{res.output[-1500:]}"
                )
        self._restore_tree()
        return run

    def _record_check(self, name: str, run: SuiteRun, outcome: CheckOutcome) -> None:
        now = time.time()
        self.steps.append(
            StepRecord(
                name=name,
                kind="check",
                started=now,
                ended=now,
                ok=outcome.ok,
                counts=run.counts,
                layers=run.layers,
                note=_first(outcome.problems),
            )
        )
        log.info("%s check ok=%s counts=%s", name, outcome.ok, run.counts)

    def _check_tester_scope(self, since: str) -> None:
        outside = [
            f
            for f in changed_files(self.private, since, self.head)
            if not matches_any(f, self.config.tester_owned)
            or matches_any(f, self.config.protected)
        ]
        if outside:
            raise RunFailed(
                ErrorCode.PROTECTED_PATH,
                f"tester changed files outside its scope: {', '.join(outside[:10])}",
            )

    def _check_coder_scope(self) -> None:
        tester_files = [
            f
            for f in changed_files(self.private, self.tester_sha, self.head)
            if matches_any(f, self.config.tester_owned)
        ]
        if tester_files:
            raise RunFailed(
                ErrorCode.TESTER_FILES_CHANGED, ", ".join(tester_files[:10])
            )
        self.check_protected(self.head)

    def check_protected(self, sha: str) -> None:
        blocked = [
            f
            for f in changed_files(self.private, self.base, sha)
            if matches_any(f, self.config.protected)
        ]
        if blocked:
            raise RunFailed(ErrorCode.PROTECTED_PATH, ", ".join(blocked[:10]))

    # ---- git ------------------------------------------------------------

    def _commit(self, message: str) -> None:
        out_dir = self._dir("out", "commit")
        self.container.put(out_dir, {})
        res = self._gitops("commit", self.repo, self.config.base_branch, message,
                           f"{out_dir}/head.bundle")  # fmt: skip
        if res.exit_code != 0:
            raise RunFailed(ErrorCode.UNKNOWN, f"commit step exited {res.exit_code}")
        bundle = self.container.get(out_dir).get("head.bundle")
        sha = self.base if bundle is None else self._fetch(bundle)
        if not is_ancestor(self.private, self.head, sha):
            raise RunFailed(
                ErrorCode.TESTER_FILES_CHANGED,
                f"the branch history was rewritten and lost {self.head[:10]}",
            )
        self.head = sha

    def _fetch(self, bundle: bytes) -> str:
        path = self.private / ".git" / "kp-incoming.bundle"
        path.write_bytes(bundle)
        try:
            git(self.private, "fetch", "--quiet", str(path), "+HEAD:refs/kp/head")
        except GitError:
            raise RunFailed(
                ErrorCode.UNKNOWN, "the agent's branch could not be read"
            ) from None
        finally:
            path.unlink(missing_ok=True)
        return git(self.private, "rev-parse", "refs/kp/head").strip()

    def _reset_to(self, sha: str) -> None:
        res = self._gitops("reset", self.repo, sha)
        if res.exit_code != 0:
            raise RunFailed(ErrorCode.UNKNOWN, f"reset step exited {res.exit_code}")
        self.head = sha

    def _restore_tree(self) -> None:
        # Tests and scanners may leave files behind. Drop them so they don't land
        # in the next agent commit (and, under tests/, trip TESTER_FILES_CHANGED).
        self._gitops("reset", self.repo, "HEAD")

    def _gitops(self, *args: str):
        return self.container.exec(
            [*self.container.python, "-m", "app.agents.gitops", *args]
        )

    def _diff(self, a: str, b: str) -> str:
        return git(self.private, "diff", a, b)

    def _dir(self, kind: str, label: str) -> str:
        self._n += 1
        return f"{self.container.workdir}/{kind}/{self._n:02d}-{label}"


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items)


def _first(items: list[str]) -> str:
    return (items[0] if items else "")[:500] + (
        f" (+{len(items) - 1} more)" if len(items) > 1 else ""
    )


def _finding(f: dict) -> str:
    return (
        f"[{f['severity']}] {f['file']}:{f['line']} {f['title']}. Fix: {f['fix_hint']}"
    )
