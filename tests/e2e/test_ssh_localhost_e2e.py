"""Opt-in end-to-end test for `SshBackend` over real `ssh localhost`.

This is the one place the whole SSH execution path runs for real: a git
bundle materializes the worktree remotely, rsync overlays dirty state, a
daemonizing supervisor is launched over SSH, and the REAL guarded remote
cleanup (ownership-checked `rm -rf`) is exercised end-to-end.

Gate: skip unless `MAESTRO_SSH_E2E=1` **and** `ssh -o BatchMode=yes localhost
true` succeeds (mirrors `tests/test_docker_integration.py`'s docker gate).
Without passwordless localhost sshd, this skips cleanly in CI/dev — the
`_GATED` check below short-circuits on the env var, so no `ssh` subprocess
ever runs at import/collection time unless the opt-in var is already set.
"""

import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.anyio

_GATED = os.environ.get("MAESTRO_SSH_E2E") != "1" or (
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "localhost", "true"],
        capture_output=True,
    ).returncode
    != 0
)
skip_reason = "set MAESTRO_SSH_E2E=1 and enable passwordless `ssh localhost`"


def _init_committed_worktree(wt: Path) -> None:
    """Git-init `wt` with one committed file (sync helper; no async-blocking)."""
    wt.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    (wt / "a.txt").write_text("orig")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        cwd=wt,
        check=True,
    )


@pytest.mark.skipif(_GATED, reason=skip_reason)
async def test_localhost_run_collect_and_real_cleanup(tmp_path: Path) -> None:
    """Run, collect, and real-cleanup a workload over real localhost SSH."""
    from maestro.execution.exec_config import SshTransport
    from maestro.execution.models import CollectPolicy, ExecutionRequest
    from maestro.execution.ssh_backend import SshBackend

    wt = tmp_path / "wt"
    _init_committed_worktree(wt)

    workdir_root = tmp_path / "remote"
    workdir_root.mkdir()
    t = SshTransport(type="ssh", host="localhost", workdir_root=str(workdir_root))
    backend = SshBackend("localhost", t, secret_env=[])

    # A workload that mutates the worktree so collect has real changes.
    req = ExecutionRequest(
        run_id="ws",
        argv=["sh", "-c", "echo changed > a.txt; echo new > b.txt"],
        workdir=wt,
        log_path=tmp_path / "ws.log",
        collect=CollectPolicy(mode="whole_worktree"),
        required_tools=[],
        execution_id="e2e1",
        entity_kind="workstream",
        backend_id="localhost",
    )
    handle = await backend.run(req)
    result = await handle.wait()
    assert result.exit_code == 0
    await handle.collect()
    assert (wt / "a.txt").read_text().strip() == "changed"
    assert (wt / "b.txt").read_text().strip() == "new"
    # Real guarded cleanup over localhost SSH: remote tmp actually removed.
    remote_root = workdir_root / "maestro-exec-e2e1"
    assert remote_root.exists()
    await handle.cleanup()
    assert not remote_root.exists()
