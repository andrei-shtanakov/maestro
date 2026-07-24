"""WAL-safe progress mirror: a remote sqlite3.backup() snapshot of the live DB,
transferred as a single file. Sequential rsync of .db/.db-wal/.db-shm is NOT a
valid snapshot protocol (see spec §F); a consistent backup() is.
"""

import logging
import sqlite3
from pathlib import Path

from maestro.execution.ssh_cli import SshCli


logger = logging.getLogger(__name__)


SNAPSHOT_SCRIPT = """\
import os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
tmp = dst + ".tmp"
source = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
target = sqlite3.connect(tmp)
with target:
    source.backup(target)
target.close(); source.close()
os.replace(tmp, dst)
"""


def snapshot_locally(src_db: Path, dst: Path) -> None:
    """Consistent snapshot of a live (WAL) DB into dst (used by tests/localhost).

    Opens `src_db` read-only and uses `sqlite3.Connection.backup()` to produce
    a point-in-time consistent copy, even while another connection holds an
    uncommitted write — the backup only ever sees committed data. The copy is
    written to a temp file first, then atomically renamed into place.
    """
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    source = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    target = sqlite3.connect(str(tmp))
    try:
        with target:
            source.backup(target)
    finally:
        target.close()
        source.close()
    tmp.replace(dst)


async def mirror_once(
    ssh: SshCli, remote_db: str, remote_snapshot: str, local_target: Path
) -> bool:
    """One mirror tick: remote snapshot -> rsync one file -> atomic local replace.

    Runs `SNAPSHOT_SCRIPT` on the remote host (via `ssh.run`) to produce a
    consistent `remote_snapshot` from the live `remote_db`, then pulls that
    single file down with `ssh.rsync` and atomically replaces `local_target`.
    Returns True on success; on any transient failure (nonzero exit from the
    remote snapshot step or the rsync step) it logs and returns False rather
    than raising, so callers can retry on the next tick.
    """
    res = await ssh.run(
        ["python3", "-", remote_db, remote_snapshot], stdin=SNAPSHOT_SCRIPT
    )
    if res.returncode != 0:
        logger.warning(
            "ssh_mirror: remote snapshot failed (rc=%s): %s",
            res.returncode,
            res.stderr,
        )
        return False
    tmp = local_target.with_suffix(local_target.suffix + ".tmp")
    pulled = await ssh.rsync(
        f"{ssh.host}:{remote_snapshot}",  # host embedded per rsync convention
        str(tmp),
        delete=False,
        excludes=[],
    )
    if pulled.returncode != 0:
        logger.warning(
            "ssh_mirror: rsync pull failed (rc=%s): %s",
            pulled.returncode,
            pulled.stderr,
        )
        return False
    if not tmp.exists():
        logger.warning("ssh_mirror: rsync reported success but %s is missing", tmp)
        return False
    tmp.replace(local_target)
    return True
