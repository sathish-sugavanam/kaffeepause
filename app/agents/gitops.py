"""Git steps that run inside the run container, never in the worker.

    python -m app.agents.gitops commit <repo> <base> <message> <bundle>
    python -m app.agents.gitops reset <repo> <sha>

`commit` commits whatever the agent left in the working tree and writes the
branch as a bundle (`<base>..HEAD`). With no commits on top of `<base>` it
writes no bundle. The worker reads only the bundle, never this working copy.
`reset` moves the branch back to a commit the worker names.
"""

import subprocess
import sys
from pathlib import Path

AUTHOR = [
    "-c",
    "user.name=Kaffeepause Agent",
    "-c",
    "user.email=agent@kaffeepause.invalid",
]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *AUTHOR, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def commit(repo: Path, base: str, message: str, bundle: Path) -> None:
    git(repo, "add", "-A")
    if git(repo, "status", "--porcelain").strip():
        git(repo, "commit", "--quiet", "--no-verify", "-m", message)
    bundle.unlink(missing_ok=True)
    if git(repo, "rev-list", "--count", f"{base}..HEAD").strip() != "0":
        git(repo, "bundle", "create", "--quiet", str(bundle), f"{base}..HEAD")


def reset(repo: Path, sha: str) -> None:
    git(repo, "reset", "--quiet", "--hard", sha)
    git(repo, "clean", "-fdq")


def main(argv: list[str]) -> int:
    match argv:
        case ["commit", repo, base, message, bundle]:
            commit(Path(repo), base, message, Path(bundle))
        case ["reset", repo, sha]:
            reset(Path(repo), sha)
        case _:
            print(__doc__, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
