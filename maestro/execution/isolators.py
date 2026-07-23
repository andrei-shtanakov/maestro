"""Isolators: compose with LocalBackend to rewrite argv/env/mounts.

`prepare` is deterministic w.r.t. its arguments (no os.environ/child_env reads);
`materialize` performs the filesystem side effects right before spawn.
"""

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

from maestro.execution.backend import TaskHandle
from maestro.execution.docker_cli import DockerCli
from maestro.execution.exec_config import DockerConfig
from maestro.execution.local import LocalTaskHandle
from maestro.execution.models import (
    ExecutionHandleRef,
    ExecutionRequest,
    PreparedRun,
    PreparedRunPlan,
)


@runtime_checkable
class Isolator(Protocol):
    """Rewrites a request's argv/env and wraps the resulting handle."""

    id: str

    def prepare(
        self,
        req: ExecutionRequest,
        *,
        trace_env: Mapping[str, str],
        host_env: Mapping[str, str],
    ) -> PreparedRunPlan: ...

    def materialize(self, plan: PreparedRunPlan) -> PreparedRun: ...

    def transport_ref(self, prepared: PreparedRun, pid: int) -> str: ...

    def wrap(
        self,
        local: LocalTaskHandle,
        prepared: PreparedRun,
        ref: ExecutionHandleRef,
    ) -> TaskHandle: ...


class BareIsolator:
    """Identity isolator: reproduces build_local_env exactly (env.py:15-20)."""

    id = "bare"

    def prepare(
        self,
        req: ExecutionRequest,
        *,
        trace_env: Mapping[str, str],
        host_env: Mapping[str, str],
    ) -> PreparedRunPlan:
        if req.inherit_env:
            env = {**host_env, **trace_env}
        else:
            allowed = {k: host_env[k] for k in req.secret_env if k in host_env}
            env = {**allowed, **req.env, **trace_env}
        return PreparedRunPlan(argv=list(req.argv), env=env)

    def materialize(self, plan: PreparedRunPlan) -> PreparedRun:
        return PreparedRun(plan=plan)

    def transport_ref(self, prepared: PreparedRun, pid: int) -> str:  # noqa: ARG002
        return f"local_pid:{pid}"

    def wrap(
        self,
        local: LocalTaskHandle,
        prepared: PreparedRun,  # noqa: ARG002
        ref: ExecutionHandleRef,  # noqa: ARG002
    ) -> TaskHandle:
        return local


class DockerIsolator:
    """Runs argv inside a local Docker container with a bind-mounted workspace."""

    id = "docker"

    def __init__(self, cfg: DockerConfig, docker: DockerCli | None = None) -> None:
        """Initialize a Docker isolator with the given config."""
        self._cfg = cfg
        self._docker = docker or DockerCli()

    def prepare(
        self,
        req: ExecutionRequest,
        *,
        trace_env: Mapping[str, str],
        host_env: Mapping[str, str],
    ) -> PreparedRunPlan:
        """Build the docker run argv and execution plan (pure, no I/O).

        Raises ValueError if req.execution_id is None.
        """
        if req.execution_id is None:
            raise ValueError("DockerIsolator requires req.execution_id")
        name = f"maestro-{req.execution_id}"
        base = Path(host_env.get("TMPDIR", "/tmp"))
        tmp_dir = base / f"maestro-exec-{req.execution_id}"
        cidfile = tmp_dir / "cid"
        env_file = tmp_dir / "env"

        # secret NAMES that actually exist on the host (values read in materialize)
        secret_keys = [k for k in self._cfg.secret_env if k in host_env]
        labels = {
            "maestro.execution_id": req.execution_id,
            "maestro.entity_kind": req.entity_kind or "task",
            "maestro.entity_id": req.run_id,
            "maestro.attempt": str(req.attempt),
            "maestro.backend_id": "docker",
        }
        argv: list[str] = [
            "docker",
            "run",
            "--name",
            name,
            "--cidfile",
            str(cidfile),
            "-v",
            f"{req.workdir}:/work",
            "-w",
            "/work",
            "--network",
            self._cfg.network,
        ]
        if self._cfg.user:
            argv += ["--user", self._cfg.user]
        if self._cfg.memory:
            argv += ["--memory", self._cfg.memory]
        if self._cfg.cpus:
            argv += ["--cpus", self._cfg.cpus]
        if secret_keys:
            argv += ["--env-file", str(env_file)]
        # non-secret env: explicit req.env + trace env, inlined (values not secret)
        for key, value in {**req.env, **dict(trace_env)}.items():
            argv += ["-e", f"{key}={value}"]
        for key, value in labels.items():
            argv += ["--label", f"{key}={value}"]
        argv.append(self._cfg.image)
        argv += list(req.argv)

        return PreparedRunPlan(
            argv=argv,
            env=dict(
                host_env
            ),  # env for docker CLI subprocess (needs PATH/DOCKER_HOST/HOME); container env set via -e / --env-file in argv
            container_name=name,
            labels=labels,
            env_file_keys=secret_keys,
            cidfile_path=cidfile,
            tmp_dir=tmp_dir,
        )

    def materialize(self, plan: PreparedRunPlan) -> PreparedRun:
        """Create the tmp dir (0700) and, if secrets are planned, the env-file (0600).

        Secret VALUES are read from `os.environ` here (not in `prepare`), and each
        value is validated to reject embedded `\\n`, `\\r`, or NUL — a raw value
        containing one of those could corrupt the `KEY=value` env-file format or
        smuggle extra lines into it. On any failure, partial artifacts (the tmp
        dir and/or env-file) are removed before the exception propagates, since
        the caller (`LocalBackend.run`) invokes `materialize` outside its own
        spawn try/except and cannot clean up on our behalf.

        Raises ValueError if a secret value contains a forbidden control char.
        """
        assert plan.tmp_dir is not None
        env_file: Path | None = None
        try:
            plan.tmp_dir.mkdir(parents=True, exist_ok=True)
            plan.tmp_dir.chmod(0o700)
            if plan.env_file_keys:
                env_file = plan.tmp_dir / "env"
                lines = []
                for key in plan.env_file_keys:
                    value = os.environ.get(key, "")
                    if any(c in value for c in ("\n", "\r", "\x00")):
                        raise ValueError(
                            f"secret {key} value has a forbidden control char"
                        )
                    lines.append(f"{key}={value}")
                # 0600 from creation: O_CREAT|O_WRONLY with mode 0o600.
                fd = os.open(
                    str(env_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
                )
                with os.fdopen(fd, "w") as fh:
                    fh.write("\n".join(lines) + "\n")
                env_file.chmod(0o600)  # defensive: umask could have narrowed
        except Exception:
            shutil.rmtree(plan.tmp_dir, ignore_errors=True)
            if env_file is not None:
                env_file.unlink(missing_ok=True)
            raise
        cleanup: list[Path] = [plan.tmp_dir]
        if env_file is not None:
            cleanup.append(env_file)
        return PreparedRun(plan=plan, env_file=env_file, cleanup_paths=cleanup)

    def transport_ref(self, prepared: PreparedRun, pid: int) -> str:  # noqa: ARG002
        """Return the docker transport reference for this run's container."""
        return f"docker:{prepared.plan.container_name}"

    def wrap(
        self,
        local: LocalTaskHandle,
        prepared: PreparedRun,
        ref: ExecutionHandleRef,
    ) -> TaskHandle:
        """Wrap local handle (not implemented yet; Task 12)."""
        raise NotImplementedError("Task 12")
