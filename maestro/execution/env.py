"""Environment assembly for local execution.

`inherit_env=True` reproduces the legacy `spawn_env()` /
`{**os.environ, **child_env()}` idiom exactly (Phase-0 zero-change). When
False, only an explicit allowlist reaches the child — the basis for the SSH
secret contract in later phases.
"""

import os

from maestro._vendor.obs import child_env
from maestro.execution.models import ExecutionRequest


def build_local_env(req: ExecutionRequest) -> dict[str, str]:
    """Build the child environment for a local run."""
    if req.inherit_env:
        return {**os.environ, **child_env()}
    allowed = {name: os.environ[name] for name in req.secret_env if name in os.environ}
    return {**allowed, **req.env, **child_env()}
