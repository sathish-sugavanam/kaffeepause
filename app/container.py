"""The run container: one per run, kept alive while the worker steps through it.

The worker shares no folder with it. Files go in with `put`/`put_tree` and
come out with `get`, through the container API, so nothing the agent does
inside (symlinks, swapped folders, background writers) can make the worker
read or write its own files by mistake. `get` returns regular files only.

`reset` restarts the container before a check, so nothing an agent left
running in the background can rewrite the JUnit report the worker reads.
"""

import io
import logging
import os
import shutil
import subprocess
import tarfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

import docker
import docker.errors

log = logging.getLogger("app.container")

OUTPUT_TAIL = 8_000
TIMED_OUT = (124, 137)  # coreutils `timeout`, and SIGKILL
GET_LIMIT = 64 * 1024 * 1024  # bytes, across all files of one `get`
WORKDIR = "/workspace"


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str  # the last OUTPUT_TAIL characters
    timed_out: bool = False


class RunContainer(Protocol):
    python: list[str]  # argv prefix that runs the harness's Python
    workdir: str  # where the run lives inside the container

    def exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stream: bool = False,
    ) -> ExecResult: ...

    def put(self, directory: str, files: dict[str, bytes]) -> None:
        """Creates `directory` (empty, or with these files) inside the container."""

    def put_tree(self, source: Path, directory: str) -> None:
        """Copies a local directory into the container at `directory`."""

    def get(self, directory: str) -> dict[str, bytes]:
        """Regular files under `directory`, by relative path. {} if it is missing."""

    def reset(self) -> None: ...


class LimitExceeded(RuntimeError):
    pass


class DockerRunContainer:
    python = ["/app/.venv/bin/python"]
    workdir = WORKDIR

    def __init__(self, container) -> None:  # a docker-py Container
        self._c = container

    def exec(self, argv, *, cwd=None, env=None, timeout=None, stream=False):
        if timeout:
            argv = ["timeout", "-s", "KILL", str(int(timeout)), *argv]
        api = self._c.client.api
        exec_id = api.exec_create(
            self._c.id,
            argv,
            workdir=cwd,
            environment=env or {},
            stdout=True,
            stderr=True,
        )["Id"]
        tail = ""
        for chunk in api.exec_start(exec_id, stream=True):
            text = chunk.decode(errors="replace")
            if stream:
                for line in text.splitlines():
                    log.info("[run] %s", line)
            tail = (tail + text)[-OUTPUT_TAIL:]
        code = api.exec_inspect(exec_id)["ExitCode"]
        for _ in range(50):  # the exit code can lag the end of the stream a little
            if code is not None:
                break
            time.sleep(0.1)
            code = api.exec_inspect(exec_id)["ExitCode"]
        code = -1 if code is None else code
        return ExecResult(code, tail, timed_out=bool(timeout) and code in TIMED_OUT)

    def put(self, directory, files):
        rel = _relative(directory)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for depth in range(1, len(rel.parts) + 1):
                info = tarfile.TarInfo(str(PurePosixPath(*rel.parts[:depth])))
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            for name, data in files.items():
                info = tarfile.TarInfo(str(rel / name))
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
        self._c.put_archive(WORKDIR, buf.getvalue())

    def put_tree(self, source, directory):
        rel = _relative(directory)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(source, arcname=str(rel))
        self._c.put_archive(WORKDIR, buf.getvalue())

    def get(self, directory):
        try:
            stream, _stat = self._c.get_archive(directory)
        except docker.errors.NotFound:
            return {}
        buf = io.BytesIO()
        for chunk in stream:
            buf.write(chunk)
            if buf.tell() > GET_LIMIT:
                raise LimitExceeded(f"{directory} is over {GET_LIMIT} bytes")
        buf.seek(0)
        out: dict[str, bytes] = {}
        with tarfile.open(fileobj=buf, mode="r") as tar:
            for member in tar:
                if not member.isfile():  # skips symlinks, devices, dirs
                    continue
                parts = PurePosixPath(member.name).parts[1:]  # drop the top folder
                if parts and ".." not in parts:
                    out[str(PurePosixPath(*parts))] = tar.extractfile(member).read()
        return out

    def reset(self) -> None:
        self._c.restart(timeout=5)


def _relative(directory: str) -> PurePosixPath:
    rel = PurePosixPath(directory).relative_to(WORKDIR)
    if ".." in rel.parts or not rel.parts:
        raise ValueError(f"{directory} is not a folder inside {WORKDIR}")
    return rel


@contextmanager
def docker_run_container(
    image: str, *, env: dict[str, str]
) -> Iterator[DockerRunContainer]:
    client = docker.from_env()
    container = client.containers.run(
        image,
        ["sleep", "infinity"],
        environment=env,
        working_dir=WORKDIR,
        detach=True,
        init=True,
    )
    try:
        yield DockerRunContainer(container)
    finally:
        container.remove(force=True)


class LocalRunContainer:
    """Runs everything as a local subprocess under `root`. For tests and local
    debugging only: it gives no isolation at all."""

    def __init__(self, root: Path, python: list[str]) -> None:
        self.root = root
        self.workdir = str(root)
        self.python = python

    def exec(self, argv, *, cwd=None, env=None, timeout=None, stream=False):
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                env={**os.environ, **(env or {})},
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            if isinstance(out, bytes):
                out = out.decode(errors="replace")
            return ExecResult(137, out[-OUTPUT_TAIL:], timed_out=True)
        return ExecResult(proc.returncode, (proc.stdout + proc.stderr)[-OUTPUT_TAIL:])

    def put(self, directory, files):
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (folder / name).write_bytes(data)

    def put_tree(self, source, directory):
        shutil.copytree(source, directory, symlinks=True)

    def get(self, directory):
        root = Path(directory)
        out: dict[str, bytes] = {}
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                path = Path(dirpath) / name
                if path.is_file() and not path.is_symlink():
                    out[str(path.relative_to(root))] = path.read_bytes()
        return out

    def reset(self) -> None:
        pass


def shell(command: str) -> list[str]:
    return ["sh", "-c", command]
