import asyncio
from pathlib import Path

import pytest

from maestro.execution.local import LocalBackend, build_local_env
from maestro.execution.models import CollectPolicy, ExecutionRequest


def _open_fd_count() -> int | None:
    """Return the current process's open fd count, or None if unsupported."""
    for fd_dir in ("/dev/fd", "/proc/self/fd"):
        path = Path(fd_dir)
        if path.is_dir():
            return len(list(path.iterdir()))
    return None


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


async def test_run_completion_does_not_leak_parent_log_fd(tmp_path):
    """Regression test for the Mode 1 fd leak (final-review blocking bug).

    The scheduler's normal completion path only does:
        handle = await backend.run(req)
        ... poll handle.poll() until it is not None ...
    It never calls wait()/terminate()/kill()/cleanup(). Before the fix,
    LocalBackend.run() kept the parent's copy of the log fd open on the
    non-capture path, and only _close_log() (invoked from those other
    methods) ever closed it — so every completed task leaked one fd.
    """
    if _open_fd_count() is None:
        pytest.skip("no /dev/fd or /proc/self/fd on this platform")

    backend = LocalBackend()
    before = _open_fd_count()
    assert before is not None
    num_tasks = 15
    for i in range(num_tasks):
        req = _req(tmp_path, ["sh", "-c", f"echo task-{i}; exit 0"])
        handle = await backend.run(req)
        # Poll (scheduler style) until the process has exited, without ever
        # calling wait()/cleanup()/kill()/terminate().
        for _ in range(200):
            if handle.poll() is not None:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("subprocess did not exit in time")
    after = _open_fd_count()
    assert after is not None

    # Allow a tiny slack for loop/watcher fds; pre-fix this leaked ~15.
    assert after <= before + 1, f"fd leak detected: before={before} after={after}"


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
