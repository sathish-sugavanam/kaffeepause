"""The tester → coder → security pipeline (PRD sections 3 and 5), run by the worker.

    setup + baseline ─ red ─▶ BASELINE_RED
    tester ─▶ red check ─(fail, 1 retry)─▶ RED_CHECK_FAILED
    coder ─▶ dispute? ─▶ tester again (implementation discarded)
          ─▶ green check ─▶ tester gap review (once) ─▶ security review ─▶ ready
    Any return to the coder (dispute, failed green, failing gap test, blocking
    finding) uses one of MAX_RETURNS. With none left the run ends as a draft PR.

The routing is plain Python. Agents run in the run container; their work comes
back only as git bundles, which this module fetches into the worker's private
clone and inspects there. The agent's working copy is never read for decisions.
"""

import glob
import json
import logging
import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import prompts
from app.agents.hooks import READ_ONLY_GIT, SCANNERS, command_prefix
from app.agents.roles import CODER, ROLES, SECURITY, TESTER, skill_name
from app.agents.verdicts import verdict_error
from app.checks import (
    CheckOutcome,
    TestRun,
    baseline_check,
    gap_check,
    green_check,
    red_check,
)
from app.container import RunContainer, shell
from app.errors import ErrorCode, RunFailed
from app.junit import match_test_id, parse_junit
from app.repo_config import RepoConfig, matches_any
from app.task import BUG, Task

log = logging.getLogger("app.pipeline")

MAX_RETURNS = 2
DEFAULT_BUDGET_USD = 10.0
MIN_STEP_BUDGET_USD = 0.05
SCAN_TIMEOUT_SECONDS = 10 * 60
OUTPUT_LIMIT = 1_000_000
MANIFESTS = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "requirements.txt", "pyproject.toml", "uv.lock", "poetry.lock", "Pipfile.lock",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock",
    "pom.xml", "build.gradle", "build.gradle.kts", "gradle.lockfile",
    "composer.json", "composer.lock", "mix.lock", "pubspec.lock",
}  # fmt: skip

AgentRunner = Callable[[Path], None]  # runs the session for one step folder


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
    exit_codes: list[int] = field(default_factory=list)
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
    scans: dict[str, str]
    steps: list[StepRecord]
    returns_used: int

    @property
    def cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.steps)

    @property
    def turns(self) -> int:
        return sum(s.turns for s in self.steps)


class _Draft(Exception):
    """No feedback returns left: stop and open a draft PR."""

    def __init__(self, items: list[str]) -> None:
        self.items = items


class Pipeline:
    def __init__(
        self,
        *,
        task: Task,
        config: RepoConfig,
        run_dir: Path,
        private: Path,
        container: RunContainer,
        models: dict[str, str] | None = None,
        budget_usd: float = DEFAULT_BUDGET_USD,
        claude_md: str = "",
        run_agent: AgentRunner | None = None,
    ) -> None:
        self.task, self.config, self.container = task, config, container
        self.run_dir, self.private = run_dir, private
        self.repo = run_dir / "repo"
        self.models = {
            r: (models or {}).get(r) or ROLES[r].default_model for r in ROLES
        }
        self.budget_usd = budget_usd
        self.claude_md = claude_md
        self.run_agent = run_agent or self._run_agent_in_container
        self.steps: list[StepRecord] = []
        self.returns_left = MAX_RETURNS
        self.base = self._git("rev-parse", "HEAD").strip()
        self.head = self.base
        self.tester_sha = self.base
        self.tester_verdict: dict = {}
        self.tests: dict[str, str] = {}
        self.required: set[str] = set()
        self.scans: dict[str, str] = {}
        self._n = 0

    # ---- the routing ----------------------------------------------------

    def run(self) -> PipelineResult:
        self._setup()
        self.baseline = self._test_run("baseline")
        outcome = baseline_check(self.baseline)
        self._record_check("baseline", self.baseline, outcome)
        if not outcome.ok:
            raise RunFailed(ErrorCode.BASELINE_RED, _first(outcome.problems))
        self.required = {
            tid for tid, r in self.baseline.results.items() if r.status == "passed"
        }

        self._tester_round()
        coder_verdict: dict | None = None
        security_verdict: dict | None = None
        feedback = ""
        gap_done = False
        ready = False
        open_items: list[str] = []
        try:
            while True:
                coder_verdict = self._agent(
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

                if disputes := coder_verdict["disputes"]:
                    items = [
                        f"Disputed test {d['test_id']}: {d['reason']} "
                        f"(criterion: {d['criterion']})"
                        for d in disputes
                    ]
                    self._use_return(items)
                    self._reset_to(self.tester_sha)
                    self._tester_round(dispute="\n".join(f"- {i}" for i in items))
                    feedback = ""
                    continue

                green = self._green()
                if not green.ok:
                    self._use_return(green.problems)
                    feedback = "Green check failed:\n" + green.feedback()
                    continue

                if not gap_done:
                    gap_done = True
                    gap = self._gap_review()
                    if not gap.ok:
                        self._use_return(gap.problems)
                        feedback = (
                            "The tester's gap review added tests that fail against "
                            "your code:\n" + gap.feedback()
                        )
                        continue

                security_verdict = self._security_review()
                blocking = [f for f in security_verdict["findings"] if f["blocking"]]
                if blocking:
                    items = [_finding(f) for f in blocking]
                    self._use_return(items)
                    feedback = "Blocking security findings:\n" + "\n".join(
                        f"- {i}" for i in items
                    )
                    continue
                ready = True
                break
        except _Draft as draft:
            open_items = draft.items

        return PipelineResult(
            ready=ready,
            head=self.head,
            tester_verdict=self.tester_verdict,
            tests=self.tests,
            coder_verdict=coder_verdict,
            security_verdict=security_verdict,
            open_items=open_items,
            scans=self.scans,
            steps=self.steps,
            returns_used=MAX_RETURNS - self.returns_left,
        )

    def _use_return(self, items: list[str]) -> None:
        if self.returns_left == 0:
            raise _Draft(items)
        self.returns_left -= 1
        log.info("feedback return used, %s left: %s", self.returns_left, _first(items))

    # ---- tester ---------------------------------------------------------

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
                raise RunFailed(
                    ErrorCode.CANNOT_REPRODUCE, verdict.get("notes", "")[:500]
                )
            self._commit(f"{self.task.issue_id}: failing tests")
            self._check_tester_scope(start)
            run = self._test_run("red")
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
            problems = outcome.problems
            feedback = outcome.feedback()
        raise RunFailed(ErrorCode.RED_CHECK_FAILED, _first(problems))

    def _gap_review(self) -> CheckOutcome:
        before = self.last_green
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
        run = self._test_run("gap")
        outcome, new_ids = gap_check(before, run)
        self._record_check("gap", run, outcome)
        for tid in new_ids:
            self.tests[tid] = "gap review"
        for t in verdict["tests"]:
            if tid := match_test_id(t["id"], new_ids):
                self.tests[tid] = t["criterion"] or "gap review"
        self.required |= new_ids
        return outcome

    # ---- coder ----------------------------------------------------------

    def _green(self) -> CheckOutcome:
        run = self._test_run("green", lint=True)
        outcome = green_check(run, self.required, run.lint_failures)
        self._record_check("green", run, outcome)
        if outcome.ok:
            self.last_green = run
        return outcome

    def _check_tester_scope(self, since: str) -> None:
        changed = self._changed(since, self.head)
        outside = [
            f
            for f in changed
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
            for f in self._changed(self.tester_sha, self.head)
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
            for f in self._changed(self.base, sha)
            if matches_any(f, self.config.protected)
        ]
        if blocked:
            raise RunFailed(ErrorCode.PROTECTED_PATH, ", ".join(blocked[:10]))

    # ---- security -------------------------------------------------------

    def _security_review(self) -> dict:
        self.scans = self._scan()
        return self._agent(
            SECURITY,
            prompts.security(
                self.task, self.config, self._diff(self.base, self.head), self.scans
            ),
            ["security-review"],
        )

    def _scan(self) -> dict[str, str]:
        self._n += 1
        out = self.run_dir / "scans" / f"{self._n:02d}"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        present = self._git(
            "diff",
            "--name-only",
            "--no-renames",
            "--diff-filter=d",
            self.base,
            self.head,
        ).split("\n")
        present = [f for f in present if f]
        commands = {
            "gitleaks": "gitleaks git --no-banner --exit-code 0 --report-format json "
            f"--report-path {out}/gitleaks.json "
            f"--log-opts={shlex.quote(f'{self.base}..HEAD')} .",
        }
        if present:
            files = " ".join(shlex.quote(f) for f in present)
            commands["semgrep"] = (
                "semgrep scan --config p/default --metrics off --quiet --json "
                f"--output {out}/semgrep.json {files}"
            )
        if any(
            os.path.basename(f) in MANIFESTS
            for f in self._changed(self.base, self.head)
        ):
            commands["osv-scanner"] = (
                f"osv-scanner scan source -r --format json . > {out}/osv-scanner.json"
            )
        scans: dict[str, str] = {}
        for name, command in commands.items():
            res = self.container.exec(
                shell(command), cwd=str(self.repo), timeout=SCAN_TIMEOUT_SECONDS
            )
            report = out / f"{name}.json"
            if res.exit_code == 127:
                scans[name] = "not installed in the run container"
            elif res.timed_out:
                scans[name] = "timed out"
            elif report.is_file() and report.stat().st_size:
                scans[name] = str(report)
            else:
                scans[name] = f"no report (exit {res.exit_code}): {res.output[-300:]}"
        if "semgrep" not in commands:
            scans["semgrep"] = "skipped: no changed files"
        self._restore_tree()
        return scans

    # ---- agents ---------------------------------------------------------

    def _agent(self, role: str, prompt: str, skills: list[str]) -> dict:
        remaining = self.budget_usd - sum(s.cost_usd for s in self.steps)
        if remaining < MIN_STEP_BUDGET_USD:
            raise RunFailed(
                ErrorCode.BUDGET_EXCEEDED, f"${self.budget_usd:.2f} run budget spent"
            )
        self._n += 1
        step_dir = self.run_dir / "steps" / f"{self._n:02d}-{role}"
        shutil.rmtree(step_dir, ignore_errors=True)
        step_dir.mkdir(parents=True)
        model = self.models[role]
        (step_dir / "input.json").write_text(
            json.dumps(
                {
                    "role": role,
                    "model": model,
                    "prompt": prompt,
                    "skills": [skill_name(s) for s in skills],
                    "max_turns": ROLES[role].max_turns,
                    "max_budget_usd": round(remaining, 4),
                    "scope": self._scope(role),
                }
            )
        )
        record = StepRecord(name=role, kind="agent", started=time.time(), model=model)
        self.steps.append(record)
        self.run_agent(step_dir)
        out = _read_output(step_dir / "output.json")
        record.ended = time.time()
        record.turns = _int(out.get("turns"))
        record.cost_usd = max(_float(out.get("cost_usd")), 0.0)
        record.usage = out.get("usage") if isinstance(out.get("usage"), dict) else {}
        record.verdict = out.get("verdict")
        subtype = str(out.get("subtype", ""))
        log.info(
            "%s step: subtype=%s turns=%s cost=$%.2f",
            role,
            subtype,
            record.turns,
            record.cost_usd,
        )
        if subtype == "error_max_budget_usd":
            raise RunFailed(ErrorCode.BUDGET_EXCEEDED, f"during the {role} step")
        if out.get("api_error_status"):
            raise RunFailed(
                ErrorCode.UPSTREAM_API,
                f"{role}: API returned {_int(out['api_error_status'])}",
            )
        if out.get("verdict") is None:
            raise RunFailed(
                ErrorCode.BAD_VERDICT,
                f"{role} ended without a verdict ({subtype[:60]})",
            )
        if reason := verdict_error(role, out["verdict"]):
            raise RunFailed(ErrorCode.BAD_VERDICT, f"{role}: {reason}")
        record.ok = True
        return out["verdict"]

    def _scope(self, role: str) -> dict[str, Any]:
        repo = str(self.repo)
        if role == TESTER:
            prefixes = [command_prefix(c) for c in self.config.test.values()]
            return {
                "repo_dir": repo,
                "write_allow": list(self.config.tester_owned),
                "write_deny": list(self.config.protected),
                "bash_prefixes": [p for p in prefixes if p] + list(READ_ONLY_GIT),
            }
        if role == CODER:
            return {
                "repo_dir": repo,
                "write_allow": None,
                "write_deny": list(self.config.tester_owned + self.config.protected),
                "bash_prefixes": None,
            }
        return {
            "repo_dir": repo,
            "write_allow": [],
            "write_deny": ["**"],
            "bash_prefixes": list(SCANNERS + READ_ONLY_GIT),
        }

    def _run_agent_in_container(self, step_dir: Path) -> None:
        self.container.exec(
            [*self.container.python, "-m", "app.agents.session", str(step_dir)],
            stream=True,
        )

    # ---- checks ---------------------------------------------------------

    def _setup(self) -> None:
        for command in self.config.setup:
            res = self.container.exec(
                shell(command), cwd=str(self.repo), timeout=self.config.timeout_seconds
            )
            if res.exit_code != 0:
                raise RunFailed(
                    ErrorCode.BASELINE_RED,
                    f"setup `{command}` exited {res.exit_code}"
                    + (" (timed out)" if res.timed_out else ""),
                )

    def _test_run(self, label: str, *, lint: bool = False) -> TestRun:
        # A restart stops anything an agent left running in the background.
        self.container.reset()
        run = TestRun({})
        self._n += 1
        for layer, command in self.config.test.items():
            jdir = self.run_dir / "junit" / f"{self._n:02d}-{label}" / layer
            shutil.rmtree(jdir, ignore_errors=True)
            jdir.mkdir(parents=True)
            res = self.container.exec(
                shell(command),
                cwd=str(self.repo),
                env={"KP_JUNIT": str(jdir)},
                timeout=self.config.timeout_seconds,
            )
            if res.timed_out:
                run.problems.append(
                    f"`{layer}` tests timed out after {self.config.timeout_seconds:.0f}s"
                )
                continue
            files = _junit_files(self.config.junit, jdir)
            if not files:
                run.problems.append(
                    f"`{layer}` tests wrote no JUnit XML (exit {res.exit_code}). "
                    f"Output tail:\n{res.output[-1500:]}"
                )
                continue
            try:
                results = parse_junit(files)
            except ValueError as exc:
                run.problems.append(f"`{layer}`: {exc}")
                continue
            run.layers[layer] = TestRun(results).counts()
            run.results.update(results)
        if lint:
            for command in self.config.lint:
                res = self.container.exec(
                    shell(command),
                    cwd=str(self.repo),
                    timeout=self.config.timeout_seconds,
                )
                if res.exit_code != 0:
                    run.lint_failures.append(
                        f"lint `{command}` exited {res.exit_code}:\n{res.output[-1500:]}"
                    )
        self._restore_tree()
        return run

    def _record_check(self, name: str, run: TestRun, outcome: CheckOutcome) -> None:
        now = time.time()
        self.steps.append(
            StepRecord(
                name=name,
                kind="check",
                started=now,
                ended=now,
                ok=outcome.ok,
                counts=run.counts(),
                layers=run.layers,
                note=_first(outcome.problems),
            )
        )
        log.info("%s check ok=%s counts=%s", name, outcome.ok, run.counts())

    # ---- git ------------------------------------------------------------

    def _commit(self, message: str) -> None:
        bundle = self.run_dir / "steps" / "head.bundle"
        res = self.container.exec(
            [
                *self.container.python,
                "-m",
                "app.agents.gitops",
                "commit",
                str(self.repo),
                self.config.base_branch,
                message,
                str(bundle),
            ]
        )
        if res.exit_code != 0:
            raise RunFailed(ErrorCode.UNKNOWN, f"commit step exited {res.exit_code}")
        if bundle.is_file():
            try:
                self._git("fetch", "--quiet", str(bundle), "+HEAD:refs/kp/head")
            except subprocess.CalledProcessError:
                raise RunFailed(
                    ErrorCode.UNKNOWN, "the agent's branch could not be read"
                ) from None
            sha = self._git("rev-parse", "refs/kp/head").strip()
        else:
            sha = self.base
        if not self._is_ancestor(self.head, sha):
            raise RunFailed(
                ErrorCode.TESTER_FILES_CHANGED,
                f"the branch history was rewritten and lost {self.head[:10]}",
            )
        self.head = sha

    def _reset_to(self, sha: str) -> None:
        res = self.container.exec(
            [
                *self.container.python,
                "-m",
                "app.agents.gitops",
                "reset",
                str(self.repo),
                sha,
            ]
        )
        if res.exit_code != 0:
            raise RunFailed(ErrorCode.UNKNOWN, f"reset step exited {res.exit_code}")
        self.head = sha

    def _restore_tree(self) -> None:
        # Tests and scanners may leave files behind. Drop them so they don't land
        # in the next agent commit (and, under tests/, trip TESTER_FILES_CHANGED).
        self.container.exec(
            [
                *self.container.python,
                "-m",
                "app.agents.gitops",
                "reset",
                str(self.repo),
                "HEAD",
            ]
        )

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.private,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def _changed(self, a: str, b: str) -> list[str]:
        return [
            f
            for f in self._git("diff", "--name-only", "--no-renames", a, b).split("\n")
            if f
        ]

    def _diff(self, a: str, b: str) -> str:
        return self._git("diff", a, b)

    def _is_ancestor(self, a: str, b: str) -> bool:
        return (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", a, b],
                cwd=self.private,
                capture_output=True,
            ).returncode
            == 0
        )


def _junit_files(pattern: str, jdir: Path) -> list[Path]:
    expanded = pattern.replace("${KP_JUNIT}", str(jdir)).replace("$KP_JUNIT", str(jdir))
    root = os.path.realpath(jdir)
    out = []
    for name in sorted(glob.glob(expanded, recursive=True)):
        real = os.path.realpath(name)
        # Never follow a link out of the JUnit folder into the worker's files.
        if (
            real.startswith(root + os.sep)
            and os.path.isfile(real)
            and not os.path.islink(name)
        ):
            out.append(Path(real))
    return out


def _read_output(path: Path) -> dict:
    try:
        if path.stat().st_size > OUTPUT_LIMIT:
            return {"subtype": "output_too_large"}
        raw = json.loads(path.read_text())
    except OSError, ValueError:
        return {"subtype": "no_output"}
    return raw if isinstance(raw, dict) else {"subtype": "bad_output"}


def _int(value: Any) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except TypeError, ValueError:
        return 0.0


def _first(items: list[str]) -> str:
    return (items[0] if items else "")[:500] + (
        f" (+{len(items) - 1} more)" if len(items) > 1 else ""
    )


def _finding(f: dict) -> str:
    return (
        f"[{f['severity']}] {f['file']}:{f['line']} {f['title']}. Fix: {f['fix_hint']}"
    )
