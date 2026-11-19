"""The run container: one per run, kept alive while the worker steps through it.

The worker execs each agent session, git step and check inside it. `reset`
restarts the container before a check, so nothing an agent left running in the
background can rewrite the JUnit report the worker is about to read.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("app.container")

OUTPUT_TAIL = 8_000
TIMED_OUT = (124, 137)  # coreutils `timeout`, and SIGKILL


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str  # the last OUTPUT_TAIL characters
    timed_out: bool = False


class RunContainer(Protocol):
    python: list[str]  # argv prefix that runs the harness's Python

    def exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        stream: bool = False,
    ) -> ExecResult: ...

    def reset(self) -> None: ...


class DockerRunContainer:
    python = ["/app/.venv/bin/python"]

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

    def reset(self) -> None:
        self._c.restart(timeout=5)


@contextmanager
def docker_run_container(
    image: str, *, env: dict[str, str], runs_volume: str, runs_dir: str
) -> Iterator[DockerRunContainer]:
    import docker

    client = docker.from_env()
    container = client.containers.run(
        image,
        ["sleep", "infinity"],
        environment=env,
        volumes={runs_volume: {"bind": runs_dir, "mode": "rw"}},
        detach=True,
        init=True,
    )
    try:
        yield DockerRunContainer(container)
    finally:
        container.remove(force=True)


class LocalRunContainer:
    """Runs everything as a local subprocess. For tests and local debugging only:
    it gives no isolation at all."""

    def __init__(self, python: list[str]) -> None:
        self.python = python

    def exec(self, argv, *, cwd=None, env=None, timeout=None, stream=False):
        import os
        import subprocess

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

    def reset(self) -> None:
        pass


def shell(command: str) -> list[str]:
    return ["sh", "-c", command]
