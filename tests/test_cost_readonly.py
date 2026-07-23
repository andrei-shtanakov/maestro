import hashlib
import sqlite3
from pathlib import Path

import pytest

from maestro.database import DatabaseError, create_database, read_all_costs_readonly
from maestro.models import AgentType, Task, TaskCost


async def _seed(db_path: Path) -> None:
    db = await create_database(db_path)  # normal (writing) connect for seeding
    await db.create_task(Task(id="t1", title="T", prompt="p", workdir="/tmp"))
    await db.save_task_cost(
        TaskCost(
            task_id="t1",
            agent_type=AgentType.CLAUDE_CODE,
            input_tokens=10,
            output_tokens=5,
            estimated_cost_usd=0.1,
        )
    )
    await db.close()


async def test_missing_db_raises_and_creates_no_file(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(DatabaseError):
        await read_all_costs_readonly(missing)
    assert not missing.exists()  # mode=ro must never create the file


async def test_directory_path_raises(tmp_path):
    with pytest.raises(DatabaseError):
        await read_all_costs_readonly(tmp_path)  # a directory


async def test_non_sqlite_file_raises(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_text("not a database")
    with pytest.raises(DatabaseError):
        await read_all_costs_readonly(junk)


async def test_missing_required_column_raises(tmp_path):
    # a task_costs table lacking reported_cost_usd (pre-migration schema)
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE task_costs (id INTEGER PRIMARY KEY, task_id TEXT, "
        "agent_type TEXT, input_tokens INT, output_tokens INT, "
        "estimated_cost_usd REAL, attempt INT, created_at TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    with pytest.raises(DatabaseError):
        await read_all_costs_readonly(p)


async def test_reads_seeded_db(tmp_path):
    p = tmp_path / "state.db"
    await _seed(p)
    costs = await read_all_costs_readonly(p)
    assert len(costs) == 1 and costs[0].task_id == "t1"


async def test_read_does_not_modify_main_db(tmp_path):
    p = tmp_path / "state.db"
    await _seed(p)  # WAL DB, writer closed -> quiescent

    def _snap() -> tuple[str, int, int]:
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        st = p.stat()
        return digest, st.st_size, st.st_mtime_ns

    before = _snap()
    await read_all_costs_readonly(p)
    after = _snap()
    assert after == before  # main .db byte/size/mtime unchanged
    # NOTE: -wal/-shm may appear/change (SQLite reading a WAL DB) — allowed,
    # not asserted; the invariant is "no writes to the database", not "no
    # new files".
