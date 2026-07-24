import json
from datetime import UTC, datetime

import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.models import ExecutionHandleRef
from maestro.execution.ssh_cli import RunResult, SshCli
from maestro.execution.ssh_launch import remote_layout
from maestro.execution.ssh_recovery import probe_ssh


def _ref(host="gpu"):
    layout = remote_layout("/w", "e1")
    return ExecutionHandleRef(
        backend_id="gpu",
        run_id="api",
        transport_ref=json.dumps(
            {
                "v": 1,
                "transport": "ssh",
                "host": host,
                "port": None,
                "remote_dir": layout.root,
                "status_marker": layout.status,
            }
        ),
        status_marker=layout.status,
        started_at=datetime.now(UTC),
    )


def _ssh(responses):
    async def runner(argv, stdin):
        for needle, r in responses:
            if needle in " ".join(argv):
                return r
        return RunResult(1, "", "")

    return SshCli(
        SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=runner
    )


@pytest.mark.anyio
async def test_marker_absent_but_process_alive_needs_review():
    ssh = _ssh(
        [
            (".status", RunResult(1, "", "no such file")),
            (".pid", RunResult(0, json.dumps({"pid": 9, "pgid": 9}), "")),
            ("kill -0", RunResult(0, "", "")),
        ]
    )
    v = await probe_ssh(ssh, _ref())
    assert v.needs_review


@pytest.mark.anyio
async def test_probe_unreachable_needs_review():
    ssh = _ssh([("", RunResult(255, "", "ssh: connect timeout"))])
    v = await probe_ssh(ssh, _ref())
    assert v.needs_review
