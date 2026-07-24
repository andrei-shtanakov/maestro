import shlex

import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.ssh_cli import RunResult, SshCli


def _t(**kw):
    return SshTransport(type="ssh", host="gpu", workdir_root="/w", **kw)


def test_guarded_opts_present_and_last():
    base = SshCli(_t(ssh_opts=["-o", "ServerAliveInterval=15"])).ssh_base()
    assert base[0] == "ssh"
    assert base[-1] == "gpu"
    joined = " ".join(base)
    for guard in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "ConnectTimeout=10",
        "PasswordAuthentication=no",
    ):
        assert guard in joined
    # user opt appears BEFORE the guarded block
    assert joined.index("ServerAliveInterval=15") < joined.index("BatchMode=yes")


def test_user_and_port_are_flags_not_host_token():
    base = SshCli(_t(user="alice", port=2222)).ssh_base()
    assert "-l" in base and "alice" in base
    assert "-p" in base and "2222" in base
    assert base[-1] == "gpu"


def test_ssh_opts_cannot_override_guarded_key():
    with pytest.raises(ValueError, match="guarded"):
        SshCli(_t(ssh_opts=["-o", "StrictHostKeyChecking=no"])).validate_ssh_opts()


def test_ssh_opts_cannot_override_guarded_key_compact_form():
    with pytest.raises(ValueError, match="guarded"):
        SshCli(_t(ssh_opts=["-oStrictHostKeyChecking=no"])).validate_ssh_opts()


def test_ssh_opts_cannot_override_guarded_key_case_insensitive():
    with pytest.raises(ValueError, match="guarded"):
        SshCli(_t(ssh_opts=["-o", "stricthostkeychecking=no"])).validate_ssh_opts()


def test_probe_tool_rejects_bad_name():
    with pytest.raises(ValueError, match="tool name"):
        import anyio

        anyio.run(SshCli(_t()).probe_tool, "spec runner; rm -rf /")


@pytest.mark.anyio
async def test_run_uses_injected_runner():
    seen = {}

    async def fake(argv, stdin):
        seen["argv"] = argv
        seen["stdin"] = stdin
        return RunResult(0, "ok", "")

    cli = SshCli(_t(), runner=fake)
    res = await cli.run(["true"], stdin="x")
    assert res.returncode == 0 and seen["stdin"] == "x"
    assert seen["argv"][0] == "ssh" and seen["argv"][-2:] == ["gpu", "true"]


@pytest.mark.anyio
async def test_run_shell_quotes_argv_with_embedded_space():
    seen = {}

    async def fake(argv, stdin):
        seen["argv"] = argv
        return RunResult(0, "", "")

    cli = SshCli(_t(), runner=fake)
    await cli.run(["echo", "a b"])
    remote_command = seen["argv"][-1]
    assert shlex.split(remote_command) == ["echo", "a b"]
