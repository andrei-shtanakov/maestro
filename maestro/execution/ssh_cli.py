"""Guarded ssh/rsync argv builder over an injectable command runner.

The runner injection makes the whole SSH backend unit-testable with no real
sshd. Maestro's security options are appended AFTER any whitelisted user
`ssh_opts`, so a user option can never disable BatchMode / host-key
verification / connect timeout / password-auth-off.
"""

import asyncio
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from maestro.execution.exec_config import SshTransport


_TOOL_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_GUARDED_KEYS = {
    "BatchMode",
    "StrictHostKeyChecking",
    "ConnectTimeout",
    "PasswordAuthentication",
    "KbdInteractiveAuthentication",
}
_GUARDED_KEYS_LOWER = {key.lower() for key in _GUARDED_KEYS}


@dataclass
class RunResult:
    """Outcome of running a command via a `Runner`."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], str | None], Awaitable[RunResult]]


async def _default_runner(argv: list[str], stdin: str | None) -> RunResult:
    """Run `argv` as a subprocess, feeding `stdin` if given."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin.encode() if stdin is not None else None)
    return RunResult(
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


class SshCli:
    """Build guarded ssh/rsync argv and run it via an injectable runner."""

    def __init__(
        self, transport: SshTransport, *, runner: Runner | None = None
    ) -> None:
        self._t = transport
        self._runner = runner or _default_runner

    @property
    def host(self) -> str:
        """The remote host/alias this client targets."""
        return self._t.host

    @property
    def workdir_root(self) -> str:
        """The remote working-directory root for this transport."""
        return self._t.workdir_root

    def validate_ssh_opts(self) -> None:
        """Reject any user ssh_opt that sets a guarded key.

        Detects both the two-token form (``-o KEY=VALUE``) and the compact
        single-token form (``-oKEY=VALUE``), and compares keys
        case-insensitively since OpenSSH option names are case-insensitive.
        """
        opts = self._t.ssh_opts
        for i, tok in enumerate(opts):
            key: str | None = None
            if tok == "-o" and i + 1 < len(opts):
                key = opts[i + 1].split("=", 1)[0].strip()
            elif tok.startswith("-o") and tok != "-o":
                key = tok[2:].split("=", 1)[0].strip()
            if key is not None and key.lower() in _GUARDED_KEYS_LOWER:
                raise ValueError(f"ssh_opt sets guarded key {key!r}")

    def _guarded_opts(self) -> list[str]:
        return [
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={self._t.connect_timeout_s}",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
        ]

    def _endpoint_flags(self) -> list[str]:
        flags: list[str] = []
        if self._t.user:
            flags += ["-l", self._t.user]
        if self._t.port:
            flags += ["-p", str(self._t.port)]
        return flags

    def ssh_base(self) -> list[str]:
        """Build the base `ssh` argv: guarded opts win, host is last."""
        self.validate_ssh_opts()
        return [
            "ssh",
            *self._t.ssh_opts,
            *self._guarded_opts(),
            *self._endpoint_flags(),
            self._t.host,
        ]

    def _rsync_ssh_string(self) -> str:
        parts = ["ssh", *self._t.ssh_opts, *self._guarded_opts()]
        if self._t.port:
            parts += ["-p", str(self._t.port)]
        return " ".join(parts)

    def rsync_argv(
        self, src: str, dst: str, *, delete: bool, excludes: list[str]
    ) -> list[str]:
        """Build an `rsync` argv that tunnels through the guarded ssh transport."""
        self.validate_ssh_opts()
        argv = ["rsync", "-a"]
        if delete:
            argv.append("--delete")
        for exc in excludes:
            argv += ["--exclude", exc]
        argv += ["-e", self._rsync_ssh_string(), src, dst]
        return argv

    async def run(self, argv: list[str], *, stdin: str | None = None) -> RunResult:
        """Run `argv` on the remote host via `ssh_base() + [shlex.join(argv)]`.

        OpenSSH joins all trailing command arguments with spaces into a
        single string handed to the remote login shell, which re-parses it.
        Shell-quoting `argv` into one token here preserves argument
        boundaries (e.g. embedded spaces) across that re-parse.
        """
        return await self._runner([*self.ssh_base(), shlex.join(argv)], stdin)

    async def rsync(
        self, src: str, dst: str, *, delete: bool, excludes: list[str]
    ) -> RunResult:
        """Run an rsync (over the guarded ssh transport) via the same runner."""
        return await self._runner(
            self.rsync_argv(src, dst, delete=delete, excludes=excludes), None
        )

    async def check(self, argv: list[str]) -> bool:
        """Run `argv` and report whether it exited successfully."""
        return (await self.run(argv)).returncode == 0

    async def probe_tool(self, tool: str) -> bool:
        """Check whether `tool` is available on the remote PATH."""
        if not _TOOL_RE.match(tool):
            raise ValueError(f"invalid tool name {tool!r}")
        return await self.check(["command", "-v", "--", tool])
