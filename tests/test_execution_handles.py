"""Tests for the `execution_handles` table and entity `backend` columns.

Docker Isolation Phase 1: durable execution identity (migrations 7 and 8).
"""

import sqlite3

import pytest

from maestro.database import ConcurrentModificationError, Database
from maestro.models import Task, TaskStatus


@pytest.mark.anyio
async def test_execution_handles_table_exists(tmp_path):
    """A freshly initialized database has the execution_handles table and
    nullable `backend` columns on both `tasks` and `workstreams`."""
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    assert db._connection is not None
    cur = await db._connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='execution_handles'"
    )
    assert await cur.fetchone() is not None
    cur = await db._connection.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "backend" in cols
    cur = await db._connection.execute("PRAGMA table_info(workstreams)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "backend" in cols
    await db.close()


@pytest.mark.anyio
async def test_execution_handles_indexes_exist(tmp_path):
    """Both indexes from the migration are present on a fresh database."""
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


@pytest.mark.anyio
async def test_execution_handles_migration_idempotent(tmp_path):
    """Re-running initialize (and thus the migration list) on an
    already-migrated database is a no-op, not an error."""
    db_path = str(tmp_path / "m.db")
    db = Database(db_path)
    await db.connect()
    await db.close()

    # Re-open and re-initialize the same on-disk database.
    db2 = Database(db_path)
    await db2.connect()
    assert db2._connection is not None
    cur = await db2._connection.execute(
        "SELECT version FROM schema_migrations WHERE version IN (7, 8)"
    )
    versions = {row[0] for row in await cur.fetchall()}
    assert versions == {7, 8}
    await db2.close()


@pytest.mark.anyio
async def test_execution_handles_check_constraints(tmp_path):
    """The CHECK constraints on entity_kind/state reject invalid values."""
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    assert db._connection is not None
    with pytest.raises(sqlite3.IntegrityError):
        await db._connection.execute(
            "INSERT INTO execution_handles "
            "(execution_id, entity_kind, entity_id, attempt, backend_id, "
            "transport_ref, state, created_at) "
            "VALUES ('e1', 'not-a-kind', 'task-1', 1, 'local', 'ref', "
            "'prepared', '2026-07-23T00:00:00+00:00')"
        )
    await db.close()


async def _seed_task(db, task_id="t1", status=TaskStatus.READY):
    task = Task(id=task_id, title="T", prompt="p", workdir="/tmp", status=status)
    await db.create_task(task)
    return task


@pytest.mark.anyio
async def test_start_execution_atomic_cas_and_insert(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    await _seed_task(db)
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="running",
        execution_id="e1",
        backend_id="docker",
        transport_ref="docker:maestro-e1",
        attempt=1,
    )
    got = await db.get_task("t1")
    assert got.status is TaskStatus.RUNNING
    rows = await db.get_open_execution_handles()
    assert any(r["execution_id"] == "e1" and r["state"] == "prepared" for r in rows)
    await db.close()


@pytest.mark.anyio
async def test_start_execution_cas_mismatch_raises_and_writes_nothing(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    await _seed_task(db, status=TaskStatus.DONE)  # not READY
    with pytest.raises(ConcurrentModificationError):
        await db.start_execution(
            entity_kind="task",
            entity_id="t1",
            expected_status="ready",
            running_status="running",
            execution_id="e1",
            backend_id="docker",
            transport_ref="docker:maestro-e1",
            attempt=1,
        )
    assert await db.get_open_execution_handles() == []  # no orphan row
    await db.close()


@pytest.mark.anyio
async def test_mark_execution_state_is_monotonic(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    await _seed_task(db)
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="running",
        execution_id="e1",
        backend_id="docker",
        transport_ref="docker:maestro-e1",
        attempt=1,
    )
    await db.mark_execution_state(
        "e1", "terminal", allowed_from=["prepared", "running"]
    )
    # cleaned -> running must be impossible
    await db.mark_execution_state("e1", "cleaned", allowed_from=["terminal"])
    await db.mark_execution_state("e1", "running", allowed_from=["prepared"])  # no-op
    rows = {r["execution_id"]: r for r in await db.get_open_execution_handles()}
    assert "e1" not in rows  # 'cleaned' is filtered out of the open set
    await db.close()


@pytest.mark.anyio
async def test_start_execution_rolls_back_cas_when_insert_fails(tmp_path):
    """If the INSERT after a successful CAS fails, the whole transaction
    (including the CAS) must roll back — no half-applied state."""
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    await _seed_task(db)
    assert db._connection is not None

    # Pre-seed a row with the execution_id we're about to reuse, so the
    # INSERT in start_execution collides on the PRIMARY KEY and raises.
    await db._connection.execute(
        "INSERT INTO execution_handles "
        "(execution_id, entity_kind, entity_id, attempt, backend_id, "
        "transport_ref, state, created_at, finished_at) "
        "VALUES ('e1', 'task', 'other-entity', 1, 'local', 'ref', "
        "'cleaned', '2026-07-23T00:00:00+00:00', NULL)"
    )
    await db._connection.commit()

    with pytest.raises(sqlite3.IntegrityError):
        await db.start_execution(
            entity_kind="task",
            entity_id="t1",
            expected_status="ready",
            running_status="running",
            execution_id="e1",
            backend_id="docker",
            transport_ref="docker:maestro-e1",
            attempt=1,
        )

    # The CAS must have been rolled back along with the failed insert.
    got = await db.get_task("t1")
    assert got.status is TaskStatus.READY

    # Only the pre-existing row survives — no partial/duplicate row.
    cur = await db._connection.execute(
        "SELECT entity_id, state FROM execution_handles WHERE execution_id = 'e1'"
    )
    rows = await cur.fetchall()
    assert [dict(r) for r in rows] == [
        {"entity_id": "other-entity", "state": "cleaned"}
    ]
    await db.close()
