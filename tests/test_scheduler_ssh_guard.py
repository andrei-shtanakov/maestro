"""Scheduler must resolve backends in Mode-1 (`scheduler`) mode.

SSH-transport backends are Mode-2 (orchestrator) only until Phase 2b, so the
scheduler's `BackendResolver` must be constructed with `mode="scheduler"` and
fail fast when asked to resolve an ssh-transport backend.
"""

from pathlib import Path

import pytest

from maestro.dag import DAG
from maestro.database import create_database
from maestro.execution.exec_config import BackendSpec, ExecutionConfig, SshTransport
from maestro.execution.resolver import ExecutionConfigError
from maestro.scheduler import Scheduler


@pytest.mark.anyio
async def test_scheduler_rejects_ssh_backend(tmp_path: Path) -> None:
    """Scheduler's resolver rejects ssh backends with a Mode-2 error."""
    cfg = ExecutionConfig(
        backends={
            "gpu": BackendSpec(
                transport=SshTransport(type="ssh", host="gpu", workdir_root="/w"),
                isolation={"type": "bare"},
            )
        }
    )
    db = await create_database(tmp_path / "s.db")
    try:
        scheduler = Scheduler(db=db, dag=DAG([]), spawners={}, execution=cfg)
        with pytest.raises(ExecutionConfigError, match="Mode-2"):
            scheduler._backends.resolve("gpu")
    finally:
        await db.close()
