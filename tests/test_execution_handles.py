"""Tests for the `execution_handles` table and entity `backend` columns.

Docker Isolation Phase 1: durable execution identity (migrations 7 and 8).
"""

import sqlite3

import pytest

from maestro.database import Database


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
