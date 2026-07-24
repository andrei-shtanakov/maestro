"""Pure builders for the SSH launch: remote path layout, JSON descriptor,
rsync exclude sets, opaque transport_ref. No I/O — trivially unit-testable.
"""

import json
from dataclasses import dataclass


RSYNC_EXCLUDES_OUT = [".git", ".maestro", "*.log"]
RSYNC_EXCLUDES_COLLECT = [
    ".git",
    ".maestro",
    "*.log",
    "env",
    ".maestro-owner",
    "*.status",
    "*.pid",
    "repo/.git",
]


@dataclass(frozen=True)
class RemoteLayout:
    """Absolute remote paths for a single SSH-backed execution."""

    root: str
    repo: str
    env_file: str
    descriptor: str
    supervisor: str
    owner_marker: str
    pid: str
    status: str
    log: str


def remote_layout(workdir_root: str, execution_id: str) -> RemoteLayout:
    """Build the fixed remote directory layout for one execution.

    Rooted at `<workdir_root>/maestro-exec-<execution_id>`.
    """
    root = f"{workdir_root.rstrip('/')}/maestro-exec-{execution_id}"
    return RemoteLayout(
        root=root,
        repo=f"{root}/repo",
        env_file=f"{root}/env",
        descriptor=f"{root}/descriptor.json",
        supervisor=f"{root}/maestro_supervisor.py",
        owner_marker=f"{root}/.maestro-owner",
        pid=f"{root}/{execution_id}.pid",
        status=f"{root}/{execution_id}.status",
        log=f"{root}/{execution_id}.log",
    )


def build_descriptor(
    execution_id: str,
    layout: RemoteLayout,
    argv: list[str],
    workdir_root: str,
) -> dict:
    """Build the JSON-serializable launch descriptor for the remote supervisor."""
    return {
        "v": 1,
        "execution_id": execution_id,
        "cwd": layout.repo,
        "argv": list(argv),
        "env_file": layout.env_file,
        "workdir_root": workdir_root,
        "owner_marker": layout.owner_marker,
        "pid_file": layout.pid,
        "status_file": layout.status,
        "log_file": layout.log,
    }


def encode_transport_ref(
    host: str, port: int | None, remote_dir: str, status_marker: str
) -> str:
    """Encode an opaque, versioned transport_ref string for an SSH execution."""
    return json.dumps(
        {
            "v": 1,
            "transport": "ssh",
            "host": host,
            "port": port,
            "remote_dir": remote_dir,
            "status_marker": status_marker,
        }
    )


def decode_transport_ref(s: str) -> dict:
    """Decode an opaque `transport_ref` string produced by `encode_transport_ref`.

    Pure inverse of `encode_transport_ref` — keeps callers (e.g. the
    orchestrator, persisting the minted handle coordinates after
    `SshBackend.run()`) decoupled from the JSON shape.
    """
    return json.loads(s)
