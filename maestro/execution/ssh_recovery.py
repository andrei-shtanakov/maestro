"""Fail-closed SSH recovery classification (peer of docker_recovery).

A remote terminal marker is NOT a completed Maestro finalization: a crash after
the marker but before collect leaves unapplied changes in the remote worktree.
So probe_ssh routes anything uncertain — or terminal-but-not-collected — to
NEEDS_REVIEW and never deletes; only a caller holding a `collected` handle GCs.
"""

import contextlib
import json

from maestro.execution.docker_recovery import RecoveryVerdict
from maestro.execution.models import ExecutionHandleRef
from maestro.execution.ssh_cli import SshCli


def _decode(ref: ExecutionHandleRef) -> dict:
    """Decode the opaque, versioned SSH `transport_ref` JSON payload."""
    return json.loads(ref.transport_ref)


async def probe_ssh(ssh: SshCli, ref: ExecutionHandleRef) -> RecoveryVerdict:
    """Classify whether a persisted SSH run is safe to silently reclaim.

    Fails closed: a present terminal marker (collect may be unconfirmed), an
    alive process group, an unknown pgid, or any probe error all result in
    `needs_review=True`. Only a caller that already knows the handle is
    `collected` (via the DB row, not this probe) should GC.

    Args:
        ssh: Guarded SSH client for the target host.
        ref: Persisted execution handle reference to probe.

    Returns:
        RecoveryVerdict describing whether review is required and why.
    """
    try:
        info = _decode(ref)
        status_marker = info["status_marker"]
        st = await ssh.run(["cat", status_marker])
        if st.returncode == 0 and st.stdout.strip():
            # Terminal marker exists. finalize may not have collected → review,
            # remote tmp preserved. (Caller distinguishes `collected` via the DB
            # row and only then GCs.)
            return RecoveryVerdict(True, "terminal marker present; collect unconfirmed")
        # No marker: is the workload still alive?
        pgid = await _read_pgid(ssh, info)
        if pgid is None:
            return RecoveryVerdict(True, "no marker and pgid unknown (fail-closed)")
        if await ssh.check(["kill", "-0", f"-{pgid}"]):
            return RecoveryVerdict(True, "no marker but process group alive")
        return RecoveryVerdict(True, "no marker; process group not confirmed dead")
    except Exception as e:
        return RecoveryVerdict(True, f"probe failed: {e}")


async def _read_pgid(ssh: SshCli, info: dict) -> int | None:
    """Read the process group id from the `.pid` file beside `status_marker`."""
    status = info["status_marker"]
    pid_file = status[: -len(".status")] + ".pid"
    res = await ssh.run(["cat", pid_file])
    if res.returncode == 0 and res.stdout.strip():
        with contextlib.suppress(Exception):
            return int(json.loads(res.stdout)["pgid"])
    return None


async def gc_ssh_terminal(ssh: SshCli, ref: ExecutionHandleRef) -> str:
    """Guarded remote `rm -rf` for a handle already known `collected`.

    Ownership-checked: only removes `remote_dir` when the `.maestro-owner`
    marker is readable there, so a stale or repurposed path is left alone.

    Args:
        ssh: Guarded SSH client for the target host.
        ref: Persisted execution handle reference, already confirmed
            `collected` by the caller (this function does not re-check).

    Returns:
        `"removed"` or `"no owner marker; skipped"`.
    """
    info = _decode(ref)
    remote_dir = info["remote_dir"]
    owner = f"{remote_dir}/.maestro-owner"
    res = await ssh.run(["cat", owner])
    if res.returncode != 0:
        return "no owner marker; skipped"
    await ssh.run(["rm", "-rf", remote_dir])
    return "removed"
