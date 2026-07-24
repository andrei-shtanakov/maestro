"""Tests for migration 9: `collected` execution-handle state + remote columns.

SSH backend Phase 2a (B3): execution_handles gains a durable `collected`
state (SSH-collected-but-not-yet-cleaned) plus persisted remote coordinates
(`remote_host`, `remote_dir`, `status_marker`, `collected_at`).
"""

import pytest

from maestro.database import Database
from maestro.models import Workstream, WorkstreamStatus


def _seed_workstream(wid="api", status=WorkstreamStatus.READY):
    return Workstream(
        id=wid,
        title=wid,
        description="d",
        branch=f"feature/{wid}",
        status=status,
    )


@pytest.mark.anyio
async def test_collected_state_and_remote_columns(tmp_path):
    """`collected` is a valid state and the remote columns round-trip."""
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    try:
        await db.create_workstream(_seed_workstream("api"))
        await db.start_execution(
            entity_kind="workstream",
            entity_id="api",
            expected_status=WorkstreamStatus.READY.value,
            running_status=WorkstreamStatus.RUNNING.value,
            execution_id="e1",
            backend_id="ssh",
            transport_ref='{"v":1,"transport":"ssh"}',
            attempt=1,
            remote_host="gpu",
            remote_dir="/var/tmp/maestro/maestro-exec-e1.ab",
            status_marker="/var/tmp/maestro/maestro-exec-e1.ab/e1.status",
        )
        await db.mark_execution_state(
            "e1", "terminal", allowed_from=["prepared", "running"]
        )
        await db.mark_execution_state("e1", "collected", allowed_from=["terminal"])
        rows = await db.get_open_execution_handles()
        row = next(r for r in rows if r["execution_id"] == "e1")
        assert row["state"] == "collected"
        assert row["remote_host"] == "gpu"
        assert row["remote_dir"].endswith("maestro-exec-e1.ab")
        assert row["status_marker"].endswith("e1.status")
        assert row["collected_at"] is not None
    finally:
        await db.close()


@pytest.mark.anyio
async def test_migration_9_registered_and_idempotent(tmp_path):
    """Migration 9 is journaled on a fresh DB and re-connecting is a no-op."""
    db_path = str(tmp_path / "m.db")
    db = Database(db_path)
    await db.connect()
    await db.close()

    db2 = Database(db_path)
    await db2.connect()
    assert db2._connection is not None
    cur = await db2._connection.execute(
        "SELECT version FROM schema_migrations WHERE version = 9"
    )
    assert await cur.fetchone() is not None
    await db2.close()


@pytest.mark.anyio
async def test_indexes_survive_migration(tmp_path):
    """The table-rebuild in migration 9 re-creates both indexes."""
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    assert db._connection is not None
    cur = await db._connection.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name IN ('ix_exec_state_backend', 'ix_exec_entity')"
    )
    names = {row[0] for row in await cur.fetchall()}
    assert names == {"ix_exec_state_backend", "ix_exec_entity"}
    await db.close()
