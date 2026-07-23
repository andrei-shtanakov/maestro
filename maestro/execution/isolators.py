"""Isolators: compose with LocalBackend to rewrite argv/env/mounts.

`prepare` is deterministic w.r.t. its arguments (no os.environ/child_env reads);
`materialize` performs the filesystem side effects right before spawn.
"""

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
        """Materialize plan into run (not implemented yet; Task 11)."""
        raise NotImplementedError("Task 11")

    def transport_ref(self, prepared: PreparedRun, pid: int) -> str:
        """Compute transport reference (not implemented yet; Task 11)."""
        raise NotImplementedError("Task 11")

    def wrap(
        self,
        local: LocalTaskHandle,
        prepared: PreparedRun,
        ref: ExecutionHandleRef,
    ) -> TaskHandle:
        """Wrap local handle (not implemented yet; Task 11)."""
        raise NotImplementedError("Task 11")
