"""Git on the worker's own clones. The one place the worker runs git.

Hooks are off for every call. Error messages never include the command line,
so nothing here can leak a credential into a run's error text.
"""

import os
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    def __init__(self, args: tuple[str, ...], returncode: int) -> None:
        self.returncode = returncode
        super().__init__(f"git {args[0] if args else ''} exited {returncode}")


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        env={**os.environ, **env} if env else None,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitError(args, proc.returncode)
    return proc.stdout


def show_file(repo: Path, ref: str, path: str) -> str | None:
    try:
        return git(repo, "show", f"{ref}:{path}")
    except GitError:
        return None


def changed_files(repo: Path, a: str, b: str, *, deleted: bool = True) -> list[str]:
    args = ["diff", "--name-only", "--no-renames", a, b]
    if not deleted:
        args.insert(3, "--diff-filter=d")
    return [f for f in git(repo, *args).splitlines() if f]


def is_ancestor(repo: Path, a: str, b: str) -> bool:
    try:
        git(repo, "merge-base", "--is-ancestor", a, b)
    except GitError:
        return False
    return True
