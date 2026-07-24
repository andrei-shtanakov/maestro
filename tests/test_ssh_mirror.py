import sqlite3

from maestro.execution.ssh_mirror import snapshot_locally


def test_snapshot_is_consistent_under_active_writer(tmp_path):
    src = tmp_path / "live.db"
    con = sqlite3.connect(src)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (x)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    con.commit()
    # Keep a writer open (uncommitted) to prove backup() is consistent.
    con.execute("INSERT INTO t VALUES (999)")
    snap = tmp_path / "snap.db"
    snapshot_locally(src, snap)
    rcon = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    (count,) = rcon.execute("SELECT count(*) FROM t").fetchone()
    assert count == 100  # committed rows only; no DatabaseError
