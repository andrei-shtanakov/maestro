import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.execution.ssh_backend import SshBackend
from maestro.execution.ssh_cli import RunResult


class Recorder:
    def __init__(self):
        self.argvs: list[list[str]] = []

    async def __call__(self, argv, stdin):
        self.argvs.append(argv)
        j = " ".join(argv)
        if "maestro_supervisor.py" in j:
            return RunResult(0, "MAESTRO-SUPERVISOR-READY e\n", "")
        return RunResult(0, "", "")


def _req(tmp_path) -> ExecutionRequest:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "f.txt").write_text("x")
    return ExecutionRequest(
        run_id="api",
        argv=["spec-runner", "run", "--all"],
        workdir=wt,
        log_path=tmp_path / "api.log",
        collect=CollectPolicy(mode="whole_worktree"),
        required_tools=["spec-runner"],
        execution_id="e",
        entity_kind="workstream",
        backend_id="gpu",
    )


@pytest.mark.anyio
async def test_run_awaits_handshake_before_returning(tmp_path, monkeypatch):
    rec = Recorder()
    t = SshTransport(type="ssh", host="gpu", workdir_root="/var/tmp/m")
    backend = SshBackend("gpu", t, secret_env=[], runner=rec)
    # Avoid real git/rsync: monkeypatch the transfer helpers to no-ops that
    # record. (The plan's implementation must route git-bundle/rsync through
    # small injectable seams — see Step 3.)
    monkeypatch.setattr(backend, "_materialize_remote", _fake_async)
    handle = await backend.run(_req(tmp_path))
    joined = [" ".join(a) for a in rec.argvs]
    assert any("maestro_supervisor.py" in j for j in joined)
    assert handle.os_pid is None  # remote


async def _fake_async(*a, **k):
    return None
