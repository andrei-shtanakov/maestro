"""Tests for SshTaskHandle + monitor: cached poll, byte-offset log tail,
status-marker completion, process-group signals, and ownership-checked
cleanup. All driven by a fake runner — no real sshd required.
"""

import json
from typing import ClassVar

import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.models import ExecutionHandleRef
from maestro.execution.ssh_cli import RunResult, SshCli
from maestro.execution.ssh_handle import CollectSpec, SshTaskHandle
from maestro.execution.ssh_launch import remote_layout


def _ref() -> ExecutionHandleRef:
    from datetime import UTC, datetime

    return ExecutionHandleRef(
        backend_id="gpu", run_id="api", transport_ref="{}", started_at=datetime.now(UTC)
    )


class FakeSsh:
    """Scripts responses by matching a substring in the joined remote argv."""

    def __init__(self, responses):
        self._responses = responses
        self.calls: list[list[str]] = []

    async def __call__(self, argv, stdin):
        self.calls.append(argv)
        for needle, result in self._responses:
            if needle in " ".join(argv):
                return result
        return RunResult(0, "", "")


@pytest.mark.anyio
async def test_poll_is_cached_and_wait_reads_status(tmp_path, monkeypatch):
    status = json.dumps({"pid": 5, "pgid": 5, "exit_code": 0, "completed_at": 1.0})
    fake = FakeSsh([("cat", RunResult(0, status, ""))])
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    layout = remote_layout("/w", "e1")
    h = SshTaskHandle(
        ssh,
        layout,
        _ref(),
        log_path=tmp_path / "log",
        timeout_seconds=None,
        collect_spec=CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        poll_interval=0.01,
    )
    h.start()
    res = await h.wait()
    assert res.exit_code == 0
    assert h.poll() == 0  # cached, no I/O


@pytest.mark.anyio
async def test_terminate_signals_process_group(tmp_path):
    status = json.dumps({"pid": 5, "pgid": 42, "exit_code": 143, "completed_at": 1.0})
    pidf = json.dumps({"pid": 5, "pgid": 42})
    fake = FakeSsh(
        [("cat", RunResult(0, status, "")), (".pid", RunResult(0, pidf, ""))]
    )
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    h = SshTaskHandle(
        ssh,
        remote_layout("/w", "e1"),
        _ref(),
        log_path=tmp_path / "log",
        timeout_seconds=None,
        collect_spec=CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        poll_interval=0.01,
    )
    await h.terminate(0.01)
    kill_calls = [c for c in fake.calls if "kill" in " ".join(c)]
    assert any("-42" in " ".join(c) for c in kill_calls)  # negative pgid = group


@pytest.mark.anyio
async def test_cleanup_refuses_when_owner_marker_mismatch(tmp_path):
    # owner marker cat returns a DIFFERENT execution_id → refuse rm -rf
    fake = FakeSsh([(".maestro-owner", RunResult(0, "OTHER\n", ""))])
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    layout = remote_layout("/w", "e1")
    h = SshTaskHandle(
        ssh,
        layout,
        _ref(),
        log_path=tmp_path / "log",
        timeout_seconds=None,
        collect_spec=CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        poll_interval=0.01,
        expected_owner="e1",
    )
    with pytest.raises(RuntimeError, match="owner"):
        await h.cleanup()
    assert not any("rm -rf" in " ".join(c) for c in fake.calls)


@pytest.mark.anyio
async def test_tail_uses_byte_offset_no_dup(tmp_path):
    # First tail returns "abc", status still absent; second tail returns "" then status.
    seq = {"n": 0}

    class Seq:
        calls: ClassVar[list[list[str]]] = []

        async def __call__(self, argv, stdin):
            self.calls.append(argv)
            j = " ".join(argv)
            if "tail" in j:
                seq["n"] += 1
                return RunResult(0, "abc" if seq["n"] == 1 else "", "")
            if "cat" in j and ".status" in j:
                return RunResult(
                    0,
                    ""
                    if seq["n"] < 2
                    else json.dumps(
                        {"pid": 1, "pgid": 1, "exit_code": 0, "completed_at": 1.0}
                    ),
                    "",
                )
            return RunResult(0, "", "")

    fake = Seq()
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    h = SshTaskHandle(
        ssh,
        remote_layout("/w", "e1"),
        _ref(),
        log_path=tmp_path / "log",
        timeout_seconds=None,
        collect_spec=CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        poll_interval=0.01,
    )
    h.start()
    await h.wait()
    assert (tmp_path / "log").read_text() == "abc"  # written once, no duplication
    # second tail requested a higher offset
    tails = [c for c in fake.calls if "tail" in " ".join(c)]
    assert "+4" in " ".join(tails[1])
