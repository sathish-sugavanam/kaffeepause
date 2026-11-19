"""Runs the security scanners in the run container before the security review.

The reports stay in the container, where the security agent reads them. The
worker only records whether each scanner ran.
"""

import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.container import RunContainer, shell

TIMEOUT_SECONDS = 10 * 60
MANIFESTS = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "requirements.txt", "pyproject.toml", "uv.lock", "poetry.lock", "Pipfile.lock",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock",
    "pom.xml", "build.gradle", "build.gradle.kts", "gradle.lockfile",
    "composer.json", "composer.lock", "mix.lock", "pubspec.lock",
}  # fmt: skip


@dataclass(frozen=True)
class Scan:
    name: str
    report: str | None  # path inside the container, when the scanner ran
    note: str = ""  # why it didn't run, or what went wrong

    @property
    def summary(self) -> str:
        return f"report at {self.report}" if self.report else self.note


def run_scans(
    container: RunContainer,
    *,
    repo: str,
    out_dir: str,
    base: str,
    changed: list[str],
    present: list[str],
) -> list[Scan]:
    """`changed`: every path the change touches; `present`: those still existing."""
    container.put(out_dir, {})
    commands = {
        "gitleaks": "gitleaks git --no-banner --exit-code 0 --report-format json "
        f"--report-path {shlex.quote(out_dir + '/gitleaks.json')} "
        f"--log-opts={shlex.quote(f'{base}..HEAD')} .",
    }
    if present:
        # `--` so a changed file named like a flag stays a file.
        files = " ".join(shlex.quote(f) for f in present)
        commands["semgrep"] = (
            "semgrep scan --config p/default --metrics off --quiet --json "
            f"--output {shlex.quote(out_dir + '/semgrep.json')} -- {files}"
        )
    if any(PurePosixPath(f).name in MANIFESTS for f in changed):
        commands["osv-scanner"] = (
            "osv-scanner scan source -r --format json . > "
            + shlex.quote(out_dir + "/osv-scanner.json")
        )

    results: dict[str, tuple[int, bool, str]] = {}
    for name, command in commands.items():
        res = container.exec(shell(command), cwd=repo, timeout=TIMEOUT_SECONDS)
        results[name] = (res.exit_code, res.timed_out, res.output[-300:])
    reports = {name for name, data in container.get(out_dir).items() if data}

    scans = []
    for name, (code, timed_out, tail) in results.items():
        if code == 127:
            scans.append(Scan(name, None, "not installed in the run container"))
        elif timed_out:
            scans.append(Scan(name, None, "timed out"))
        elif f"{name}.json" in reports:
            scans.append(Scan(name, f"{out_dir}/{name}.json"))
        else:
            scans.append(Scan(name, None, f"no report (exit {code}): {tail}"))
    if "semgrep" not in commands:
        scans.append(Scan("semgrep", None, "skipped: no changed files"))
    return sorted(scans, key=lambda s: s.name)
