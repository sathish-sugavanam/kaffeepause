"""The container file boundary: what the worker accepts back from a run container."""

import io
import sys
import tarfile
import textwrap

import pytest

from app.agents.hooks import Scope
from app.agents.step import StepInput
from app.container import DockerRunContainer, LocalRunContainer
from app.pipeline import container_agent_runner


class FakeDockerContainer:
    """Stands in for a docker-py Container: put_archive/get_archive only."""

    def __init__(self, archive: bytes = b"") -> None:
        self.archive = archive
        self.put: list[tuple[str, bytes]] = []

    def put_archive(self, path, data):
        self.put.append((path, data))

    def get_archive(self, path):
        return iter([self.archive]), {}


def tar(*entries: tarfile.TarInfo, data: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for info in entries:
            payload = (data or {}).get(info.name)
            if payload is not None:
                info.size = len(payload)
            t.addfile(info, io.BytesIO(payload) if payload is not None else None)
    return buf.getvalue()


def entry(name, kind=tarfile.REGTYPE, link=""):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, link
    return info


def test_get_returns_regular_files_only():
    archive = tar(
        entry("03-coder", tarfile.DIRTYPE),
        entry("03-coder/output.json"),
        entry("03-coder/leak", tarfile.SYMTYPE, "/etc/passwd"),
        entry("03-coder/hard", tarfile.LNKTYPE, "03-coder/output.json"),
        entry("03-coder/../escape.txt"),
        data={"03-coder/output.json": b"{}", "03-coder/../escape.txt": b"x"},
    )
    files = DockerRunContainer(FakeDockerContainer(archive)).get(
        "/workspace/steps/03-coder"
    )
    assert files == {"output.json": b"{}"}


def test_put_writes_inside_the_workspace_only():
    fake = FakeDockerContainer()
    DockerRunContainer(fake).put("/workspace/steps/01-tester", {"input.json": b"{}"})
    ((path, data),) = fake.put
    names = tarfile.open(fileobj=io.BytesIO(data)).getnames()
    assert path == "/workspace"
    assert names == ["steps", "steps/01-tester", "steps/01-tester/input.json"]


def test_put_refuses_paths_outside_the_workspace():
    for bad in ("/etc", "/workspace", "/workspace/../etc"):
        with pytest.raises(ValueError):
            DockerRunContainer(FakeDockerContainer()).put(bad, {})


def test_local_get_skips_symlinks(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "real.json").write_text("{}")
    (tmp_path / "out" / "link.json").symlink_to(tmp_path / "secret")
    (tmp_path / "secret").write_text("worker secret")
    container = LocalRunContainer(tmp_path, [sys.executable])
    assert container.get(str(tmp_path / "out")) == {"real.json": b"{}"}


def step() -> StepInput:
    return StepInput("coder", "m", "p", (), 1, 1.0, Scope("/r", None, (), None))


def test_container_runner_round_trips_through_the_container(tmp_path):
    fake_session = tmp_path / "fake_session.py"
    fake_session.write_text(
        textwrap.dedent("""
        import json, pathlib, sys
        d = pathlib.Path(sys.argv[-1])
        assert json.loads((d / "input.json").read_text())["role"] == "coder"
        (d / "output.json").write_text(json.dumps(
            {"subtype": "success", "verdict": {"ok": 1}, "turns": 2, "cost_usd": 0.25}))
    """)
    )
    container = LocalRunContainer(tmp_path / "ws", [sys.executable, str(fake_session)])
    out = container_agent_runner(container)(step())
    assert (out.subtype, out.verdict, out.turns, out.cost_usd) == (
        "success",
        {"ok": 1},
        2,
        0.25,
    )


def test_container_runner_survives_a_session_that_wrote_nothing(tmp_path):
    container = LocalRunContainer(tmp_path / "ws", [sys.executable, "-c", "pass"])
    out = container_agent_runner(container)(step())
    assert (out.subtype, out.verdict) == ("no_output", None)
