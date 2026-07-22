from pathlib import Path

from maestro.execution.local import LocalBackend, build_local_env
from maestro.execution.models import CollectPolicy, ExecutionRequest


def _req(tmp_path: Path, argv: list[str], **kw) -> ExecutionRequest:
    return ExecutionRequest(
        run_id="r1",
        argv=argv,
        workdir=tmp_path,
        log_path=tmp_path / "out.log",
        collect=CollectPolicy(mode="none"),
        **kw,
    )


async def test_run_streams_to_log_and_reports_exit_code(tmp_path):
    backend = LocalBackend()
    handle = await backend.run(_req(tmp_path, ["sh", "-c", "echo hello; exit 0"]))
    result = await handle.wait()
    assert result.exit_code == 0
    assert result.timed_out is False
    assert (tmp_path / "out.log").read_text().strip() == "hello"
    assert handle.poll() == 0
    await handle.cleanup()


async def test_run_nonzero_exit(tmp_path):
    backend = LocalBackend()
    handle = await backend.run(_req(tmp_path, ["sh", "-c", "exit 3"]))
    result = await handle.wait()
    assert result.exit_code == 3


async def test_capture_output_populates_tails(tmp_path):
    backend = LocalBackend()
    req = _req(tmp_path, ["sh", "-c", "echo out; echo err 1>&2"], capture_output=True)
    handle = await backend.run(req)
    result = await handle.wait()
    assert result.exit_code == 0
    assert "out" in result.stdout_tail
    assert "err" in result.stderr_tail


async def test_timeout_kills_and_flags(tmp_path):
    backend = LocalBackend()
    handle = await backend.run(_req(tmp_path, ["sleep", "5"], timeout_seconds=1))
    result = await handle.wait()
    assert result.timed_out is True
    assert result.exit_code is None


async def test_poll_is_none_while_running(tmp_path):
    backend = LocalBackend()
    handle = await backend.run(_req(tmp_path, ["sleep", "2"]))
    assert handle.poll() is None
    assert handle.os_pid is not None and handle.os_pid > 0
    await handle.kill()
    await handle.wait()


async def test_can_run_missing_tool(tmp_path):
    backend = LocalBackend()
    req = _req(tmp_path, ["true"], required_tools=["definitely-not-a-real-binary"])
    cap = await backend.can_run(req)
    assert cap.ok is False
    assert "definitely-not-a-real-binary" in cap.missing_tools


def test_build_local_env_inherit(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_MARKER", "xyz")
    req = _req(tmp_path, ["true"], inherit_env=True)
    env = build_local_env(req)
    assert env["MY_MARKER"] == "xyz"  # full inheritance == today's spawn_env()


def test_build_local_env_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_ONE", "s1")
    monkeypatch.setenv("LEAK_ME", "nope")
    req = _req(
        tmp_path,
        ["true"],
        inherit_env=False,
        secret_env=["SECRET_ONE"],
        env={"EXPLICIT": "e"},
    )
    env = build_local_env(req)
    assert env["SECRET_ONE"] == "s1"
    assert env["EXPLICIT"] == "e"
    assert "LEAK_ME" not in env
