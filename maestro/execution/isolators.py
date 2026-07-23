"""Isolators: compose with LocalBackend to rewrite argv/env/mounts.

`prepare` is deterministic w.r.t. its arguments (no os.environ/child_env reads);
`materialize` performs the filesystem side effects right before spawn.
"""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from maestro.execution.backend import TaskHandle
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
