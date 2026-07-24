"""Fixed, versioned Maestro remote supervisor. Stdlib only.

Launched as `python3 maestro_supervisor.py <descriptor.json>`. Daemonizes so it
outlives the launch SSH channel, owns the workload process group, and writes an
atomic status marker the center's monitor polls. NO dynamic argv is ever
interpolated into this source — everything comes from the descriptor.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


SUPERVISOR_VERSION = 1
HANDSHAKE = "MAESTRO-SUPERVISOR-READY"


def _fail(msg: str) -> None:
    sys.stderr.write(f"supervisor: {msg}\n")
    sys.stderr.flush()
    os._exit(2)


def _validate(desc: dict) -> None:
    root = f"{desc['workdir_root'].rstrip('/')}/maestro-exec-{desc['execution_id']}"
    root_real = str(Path(root).resolve(strict=False))
    keys = ("cwd", "env_file", "owner_marker", "pid_file", "status_file", "log_file")
    for key in keys:
        candidate_real = str(Path(str(desc[key])).resolve(strict=False))
        if candidate_real == root_real:
            continue
        common = os.path.commonpath([root_real, candidate_real])
        if common != root_real:
            _fail(f"path {key} escapes {root}")


def _atomic_write(path: str, data: str) -> None:
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    Path(tmp).replace(path)


def _load_env(env_file: str) -> dict:
    env = dict(os.environ)
    if Path(env_file).exists():
        with Path(env_file).open() as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env[key] = value
    return env


def _run_workload(desc: dict, ready_fd: int) -> None:
    # Detached child: no controlling terminal, own session/process group.
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    log_fd = os.open(desc["log_file"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)

    env = _load_env(desc["env_file"])
    try:
        proc = subprocess.Popen(
            desc["argv"],
            cwd=desc["cwd"],
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        # Workload could not exec (e.g. argv[0] not on the remote PATH). Signal
        # the parent through the confirmation pipe so `run()` fails fast instead
        # of hanging on a handshake that will never come. No `.status` is
        # written — the run never started.
        with os.fdopen(ready_fd, "wb") as pipe:
            pipe.write(f"ERR:{exc}\n".encode("utf-8", "replace"))
        os._exit(2)
    _atomic_write(desc["pid_file"], json.dumps({"pid": proc.pid, "pgid": proc.pid}))
    # Popen succeeded and the pid/pgid + owner marker + descriptor + log are all
    # in place: confirm readiness to the parent, then close the pipe so the
    # parent's read returns.
    with os.fdopen(ready_fd, "wb") as pipe:
        pipe.write(b"OK\n")
    exit_code = proc.wait()
    _atomic_write(
        desc["status_file"],
        json.dumps(
            {
                "pid": proc.pid,
                "pgid": proc.pid,
                "exit_code": exit_code,
                "completed_at": time.time(),
            }
        ),
    )
    os._exit(0)


def main() -> None:
    if len(sys.argv) != 2:
        _fail("usage: maestro_supervisor.py <descriptor.json>")
    with Path(sys.argv[1]).open() as fh:
        desc = json.load(fh)
    _validate(desc)
    with Path(desc["owner_marker"]).open("w") as fh:
        fh.write(desc["execution_id"] + "\n")

    # Popen-confirmation handshake: the parent only emits readiness once the
    # daemon child confirms the workload actually started (spec C.4). The pipe
    # fds are non-inheritable (PEP 446), so the exec'd workload never holds the
    # write end — the parent's read cannot deadlock on it.
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid > 0:
        # Parent: block on the child's confirmation, then end the launch SSH
        # command. On OK -> emit the handshake `run()` waits for; on ERR or an
        # empty read (child died before confirming) -> exit non-zero WITHOUT the
        # handshake, so `run()` fails fast instead of hanging.
        os.close(write_fd)
        with os.fdopen(read_fd, "rb") as pipe:
            msg = pipe.read()
        if msg.startswith(b"OK"):
            sys.stdout.write(f"{HANDSHAKE} {desc['execution_id']}\n")
            sys.stdout.flush()
            os._exit(0)
        detail = msg.decode("utf-8", "replace").strip() or "child exited before start"
        sys.stderr.write(f"supervisor: workload did not start: {detail}\n")
        sys.stderr.flush()
        os._exit(2)
    # Child: daemonized supervisor.
    os.close(read_fd)
    _run_workload(desc, write_fd)


if __name__ == "__main__":
    main()
