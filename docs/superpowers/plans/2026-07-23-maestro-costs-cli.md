# `maestro costs` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only `maestro costs --db <path>` CLI that prints a database-wide cost summary (TOTAL + by-harness + by-task) over `task_costs`, with honest known/unknown accounting.

**Architecture:** A pure aggregator in `cost_tracker.py` (reusing `effective_cost` as the known/unknown SSOT); a dedicated **read-only** SQLite connection in `database.py` (`mode=ro` URI — never creates/modifies the DB, verifies required columns); a `costs` command in `cli.py` that renders Rich tables and maps error inputs to exit 2.

**Tech Stack:** Python 3.12+, aiosqlite (URI `mode=ro`), Typer + Rich, pytest (pytest-asyncio auto), ruff, pyrefly.

## Global Constraints

- Package manager: `uv` only (`uv run pytest`, `uv add`). Never pip.
- Type hints on all code; `uv run pyrefly check` 0 errors.
- `uv run ruff format .` / `uv run ruff check .` pass; line length 88.
- Async tests: `async def test_...` under pytest-asyncio auto mode.
- **Read-only is the connection lifecycle, not just the query:** never create a
  missing file, never run schema/migrations, never modify the DB. Use SQLite
  `mode=ro` (NOT `immutable=1`). The invariant is **no writes to the database**
  (its data/schema/migrations are untouched; a missing path is never created) —
  **not** "no new files": reading a WAL DB read-only may create/modify/leave
  SQLite `-wal`/`-shm` service files, which is allowed (spec r4 §7/§8).
- Known/unknown cost = `cost_tracker.effective_cost(row)` (single source of
  truth): `reported > priced-estimate > None`. `announce`=known-$0;
  `opencode`-unpriced-unreported=unknown. Never sum an unpriced `estimated=0.0`
  as known.
- **No "by model" and no "by run" grouping** (unprovable read-only — documented
  boundary). Do not touch the write path / dispatcher / event log / REST /
  `get_cost_summary` / `build_summary`.
- Spec: `docs/superpowers/specs/2026-07-23-maestro-costs-cli-design.md`.

## File Structure

- Modify `maestro/cost_tracker.py` — add `CostGroup`, `CostReport`, `summarize_costs`.
- Modify `maestro/database.py` — add `read_all_costs_readonly` + `_ro_uri` + required-column check.
- Modify `maestro/cli.py` — add the `costs` command + `_render_cost_report`.
- Modify `CLAUDE.md` — document `maestro costs` (call it a database-wide summary).
- Tests: `tests/test_cost_summary.py` (aggregator), `tests/test_cost_readonly.py` (ro DB access), `tests/test_cli_costs.py` (CLI).

---

## Task 1: Pure aggregator — `summarize_costs`

**Files:**
- Modify: `maestro/cost_tracker.py`
- Test: `tests/test_cost_summary.py`

**Interfaces:**
- Consumes: `TaskCost` (models), `effective_cost` (same module).
- Produces:
  - `@dataclass(frozen=True) class CostGroup`: `label: str`, `known_cost_usd: float`, `input_tokens: int`, `output_tokens: int`, `tasks: int`, `attempts: int`, `unknown_attempts: int`, `unknown_tasks: int`.
  - `@dataclass(frozen=True) class CostReport`: `total: CostGroup`, `by_harness: list[CostGroup]`, `by_task: list[CostGroup]`.
  - `def summarize_costs(costs: list[TaskCost]) -> CostReport`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cost_summary.py
from datetime import UTC, datetime

from maestro.cost_tracker import CostReport, summarize_costs
from maestro.models import AgentType, TaskCost


def _c(task_id, agent, *, inp=0, out=0, est=0.0, reported=None, attempt=1):
    return TaskCost(
        task_id=task_id, agent_type=agent, input_tokens=inp, output_tokens=out,
        estimated_cost_usd=est, reported_cost_usd=reported, attempt=attempt,
        created_at=datetime.now(UTC),
    )


def test_empty():
    r = summarize_costs([])
    assert isinstance(r, CostReport)
    assert r.total.known_cost_usd == 0.0
    assert r.total.tasks == 0 and r.total.attempts == 0
    assert r.by_harness == [] and r.by_task == []


def test_announce_is_known_zero_not_unknown():
    r = summarize_costs([_c("t1", AgentType.ANNOUNCE, est=0.0)])
    assert r.total.known_cost_usd == 0.0
    assert r.total.unknown_attempts == 0 and r.total.unknown_tasks == 0


def test_opencode_unpriced_unreported_is_unknown():
    # opencode absent from PRICING; estimated 0.0 must NOT be summed as known
    r = summarize_costs([_c("t1", AgentType.OPENCODE, inp=100, out=50, est=0.0)])
    assert r.total.known_cost_usd == 0.0
    assert r.total.unknown_attempts == 1 and r.total.unknown_tasks == 1
    # tokens still counted even though $ is unknown
    assert r.total.input_tokens == 100 and r.total.output_tokens == 50


def test_reported_cost_is_known():
    r = summarize_costs([_c("t1", AgentType.OPENCODE, reported=0.42)])
    assert r.total.known_cost_usd == 0.42
    assert r.total.unknown_attempts == 0


def test_priced_estimate_is_known():
    r = summarize_costs([_c("t1", AgentType.CLAUDE_CODE, est=0.10)])
    assert r.total.known_cost_usd == 0.10
    assert r.total.unknown_attempts == 0


def test_mixed_known_and_unknown_attempts_on_one_task():
    rows = [
        _c("t1", AgentType.CLAUDE_CODE, est=0.20, attempt=1),
        _c("t1", AgentType.OPENCODE, est=0.0, attempt=2),  # unknown
    ]
    r = summarize_costs(rows)
    assert r.total.known_cost_usd == 0.20     # known subtotal preserved
    assert r.total.tasks == 1 and r.total.attempts == 2
    assert r.total.unknown_attempts == 1 and r.total.unknown_tasks == 1


def test_two_tasks_same_harness():
    r = summarize_costs([_c("t1", AgentType.CLAUDE_CODE, est=0.1),
                         _c("t2", AgentType.CLAUDE_CODE, est=0.2)])
    assert r.total.tasks == 2 and r.total.attempts == 2
    assert len(r.by_harness) == 1
    assert r.by_harness[0].label == "claude_code"
    assert r.by_harness[0].known_cost_usd == 0.30 and r.by_harness[0].tasks == 2


def test_retry_with_different_harness_splits_by_group():
    rows = [_c("t1", AgentType.CLAUDE_CODE, est=0.1, attempt=1),
            _c("t1", AgentType.CODEX, est=0.2, attempt=2)]
    r = summarize_costs(rows)
    labels = {g.label: g for g in r.by_harness}
    assert set(labels) == {"claude_code", "codex_cli"}
    assert labels["claude_code"].attempts == 1
    assert labels["codex_cli"].attempts == 1
    # by task: t1 aggregates both attempts
    assert len(r.by_task) == 1 and r.by_task[0].attempts == 2
    assert r.by_task[0].known_cost_usd == 0.30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cost_summary.py -q`
Expected: FAIL — `ImportError` (`summarize_costs`/`CostReport` missing).

- [ ] **Step 3: Implement**

```python
# maestro/cost_tracker.py  (add near the top-level, after effective_cost)
from dataclasses import dataclass


@dataclass(frozen=True)
class CostGroup:
    """Aggregated cost/usage for one grouping key (or the grand total)."""

    label: str
    known_cost_usd: float
    input_tokens: int
    output_tokens: int
    tasks: int
    attempts: int
    unknown_attempts: int
    unknown_tasks: int


@dataclass(frozen=True)
class CostReport:
    total: CostGroup
    by_harness: list[CostGroup]
    by_task: list[CostGroup]


class _Acc:
    """Mutable accumulator; frozen into a CostGroup at the end."""

    def __init__(self) -> None:
        self.known = 0.0
        self.inp = 0
        self.out = 0
        self.attempts = 0
        self.unknown_attempts = 0
        self._task_ids: set[str] = set()
        self._unknown_task_ids: set[str] = set()

    def add(self, cost: TaskCost) -> None:
        self.attempts += 1
        self.inp += cost.input_tokens
        self.out += cost.output_tokens
        self._task_ids.add(cost.task_id)
        eff = effective_cost(cost)
        if eff is None:
            self.unknown_attempts += 1
            self._unknown_task_ids.add(cost.task_id)
        else:
            self.known += eff

    def freeze(self, label: str) -> CostGroup:
        return CostGroup(
            label=label,
            known_cost_usd=self.known,
            input_tokens=self.inp,
            output_tokens=self.out,
            tasks=len(self._task_ids),
            attempts=self.attempts,
            unknown_attempts=self.unknown_attempts,
            unknown_tasks=len(self._unknown_task_ids),
        )


def summarize_costs(costs: list[TaskCost]) -> CostReport:
    """Database-wide cost summary: TOTAL + per-harness + per-task.

    Known/unknown per row is decided by `effective_cost` (SSOT). `known_cost_usd`
    is a known subtotal; unknown attempts/tasks are reported alongside, never
    folded into the dollar figure. Tokens are summed over all supplied rows.
    """
    total = _Acc()
    by_harness: dict[str, _Acc] = {}
    by_task: dict[str, _Acc] = {}
    for cost in costs:
        total.add(cost)
        by_harness.setdefault(cost.agent_type.value, _Acc()).add(cost)
        by_task.setdefault(cost.task_id, _Acc()).add(cost)
    return CostReport(
        total=total.freeze("TOTAL"),
        by_harness=[acc.freeze(k) for k, acc in sorted(by_harness.items())],
        by_task=[acc.freeze(k) for k, acc in sorted(by_task.items())],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cost_summary.py -q`
Expected: PASS (all 8 tests).

- [ ] **Step 5: Format, lint, type-check**

Run: `uv run ruff format maestro/cost_tracker.py tests/test_cost_summary.py && uv run ruff check maestro/cost_tracker.py tests/test_cost_summary.py && uv run pyrefly check`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/cost_tracker.py tests/test_cost_summary.py
git commit -m "feat(costs): pure summarize_costs aggregator (known/unknown via effective_cost)"
```

---

## Task 2: Read-only DB access

**Files:**
- Modify: `maestro/database.py`
- Test: `tests/test_cost_readonly.py`

**Interfaces:**
- Consumes: `aiosqlite`, `_row_to_task_cost`, `TaskCost`, `DatabaseError`.
- Produces: `async def read_all_costs_readonly(db_path: str | Path) -> list[TaskCost]` — opens `db_path` **read-only** (mode=ro), verifies the `task_costs` schema, returns all cost rows. Raises `DatabaseError` on a missing / non-SQLite / schema-incompatible DB, **without creating or modifying any file**.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cost_readonly.py
import sqlite3
from pathlib import Path

import pytest

from maestro.database import DatabaseError, create_database, read_all_costs_readonly
from maestro.models import AgentType, TaskCost, Task, TaskStatus


async def _seed(db_path: Path) -> None:
    db = await create_database(db_path)  # normal (writing) connect for seeding
    await db.create_task(Task(id="t1", title="T", prompt="p"))
    await db.save_task_cost(
        TaskCost(task_id="t1", agent_type=AgentType.CLAUDE_CODE,
                 input_tokens=10, output_tokens=5, estimated_cost_usd=0.1)
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
    conn.commit(); conn.close()
    with pytest.raises(DatabaseError):
        await read_all_costs_readonly(p)


async def test_reads_seeded_db(tmp_path):
    p = tmp_path / "state.db"
    await _seed(p)
    costs = await read_all_costs_readonly(p)
    assert len(costs) == 1 and costs[0].task_id == "t1"


async def test_read_does_not_modify_files(tmp_path):
    p = tmp_path / "state.db"
    await _seed(p)
    before = {f.name: f.stat().st_size for f in tmp_path.iterdir()}
    before_mtime = p.stat().st_mtime_ns
    await read_all_costs_readonly(p)
    after = {f.name: f.stat().st_size for f in tmp_path.iterdir()}
    assert set(after) == set(before)      # no NEW files created
    assert p.stat().st_mtime_ns == before_mtime  # DB not modified
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cost_readonly.py -q`
Expected: FAIL — `ImportError` (`read_all_costs_readonly` missing).

- [ ] **Step 3: Implement**

```python
# maestro/database.py  (add module-level; imports: sqlite3, from urllib.request import pathname2url)
_REQUIRED_TASK_COST_COLUMNS = frozenset(
    {
        "task_id", "agent_type", "input_tokens", "output_tokens",
        "estimated_cost_usd", "reported_cost_usd", "attempt", "created_at",
    }
)


def _ro_uri(db_path: str | Path) -> str:
    """SQLite read-only URI for an absolute path (percent-quoted)."""
    abspath = Path(db_path).resolve()
    return f"file:{pathname2url(str(abspath))}?mode=ro"


async def read_all_costs_readonly(db_path: str | Path) -> list[TaskCost]:
    """Open ``db_path`` READ-ONLY and return all TaskCost rows.

    mode=ro never creates a missing file, runs no schema/migrations, and does not
    modify the DB (it may read pre-existing -wal/-shm). Raises DatabaseError for
    a missing / non-SQLite / schema-incompatible DB.
    """
    try:
        conn = await aiosqlite.connect(_ro_uri(db_path), uri=True)
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        raise DatabaseError(f"cannot open database read-only: {exc}") from exc
    try:
        conn.row_factory = aiosqlite.Row
        try:
            cursor = await conn.execute("PRAGMA table_info(task_costs)")
            columns = {row["name"] for row in await cursor.fetchall()}
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            raise DatabaseError(f"not a valid database: {exc}") from exc
        if not _REQUIRED_TASK_COST_COLUMNS <= columns:
            raise DatabaseError(
                "database has no compatible 'task_costs' table "
                "(missing table or required columns)"
            )
        cursor = await conn.execute("SELECT * FROM task_costs ORDER BY created_at")
        rows = await cursor.fetchall()
        return [_row_to_task_cost(row) for row in rows]
    finally:
        await conn.close()
```

> Verify `pathname2url` / `sqlite3` imports exist at the top of `database.py`;
> add them if missing. `PRAGMA table_info` on a missing table returns an empty
> set (→ not a superset → DatabaseError), so "no task_costs" and "missing
> column" both map to the same clean error. A non-SQLite file trips the PRAGMA
> query (file is not a database) → DatabaseError.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cost_readonly.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Format, lint, type-check, full suite**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q`
Expected: clean; full suite green (no existing DB behavior changed).

- [ ] **Step 6: Commit**

```bash
git add maestro/database.py tests/test_cost_readonly.py
git commit -m "feat(costs): read-only DB access (mode=ro, required-column check, no file creation)"
```

---

## Task 3: `maestro costs` CLI

**Files:**
- Modify: `maestro/cli.py`
- Modify: `CLAUDE.md`
- Test: `tests/test_cli_costs.py`

**Interfaces:**
- Consumes: `read_all_costs_readonly` (Task 2), `summarize_costs`/`CostReport` (Task 1), `DEFAULT_DB_PATH`, `DatabaseError`, Rich `Table`/`console`.
- Produces: `costs` Typer command; exit `0` (incl. empty valid DB), `2` (invalid/incompatible input).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_costs.py
from pathlib import Path

from typer.testing import CliRunner

from maestro.cli import app
from maestro.database import create_database
from maestro.models import AgentType, Task, TaskCost

runner = CliRunner()


async def _seed(db_path: Path, rows: list[TaskCost]) -> None:
    db = await create_database(db_path)
    await db.create_task(Task(id="t1", title="T", prompt="p"))
    for r in rows:
        await db.save_task_cost(r)
    await db.close()


def test_costs_missing_db_does_not_create_file(tmp_path):
    missing = tmp_path / "missing.db"
    result = runner.invoke(app, ["costs", "--db", str(missing)])
    assert result.exit_code == 2
    assert not missing.exists()


def test_costs_empty_db_exit_0(tmp_path, anyio_backend):
    import anyio
    p = tmp_path / "empty.db"
    anyio.run(_seed, p, [])
    result = runner.invoke(app, ["costs", "--db", str(p)])
    assert result.exit_code == 0
    assert "No cost records" in result.stdout


def test_costs_mixed_known_unknown_renders(tmp_path):
    import anyio
    p = tmp_path / "state.db"
    rows = [
        TaskCost(task_id="t1", agent_type=AgentType.CLAUDE_CODE,
                 estimated_cost_usd=0.20, attempt=1),
        TaskCost(task_id="t1", agent_type=AgentType.OPENCODE,
                 estimated_cost_usd=0.0, attempt=2),  # unknown
    ]
    anyio.run(_seed, p, rows)
    result = runner.invoke(app, ["costs", "--db", str(p)])
    assert result.exit_code == 0
    out = result.stdout
    assert "0.20" in out           # known subtotal shown
    assert "unknown" in out.lower()  # unknown attempts surfaced
    # documented boundary: no by-model / by-run TABLE (check titles, not a bare
    # substring — a task label could legitimately contain "run"/"model")
    assert "by model" not in out.lower()
    assert "by run" not in out.lower()
```

> `anyio_backend` is the repo's pytest-anyio fixture (or use `asyncio.run` in the
> helper as `tests/test_cli.py` does with `_setup_db_with_pending_task`; mirror
> whichever the sync CLI tests already use). The point: the assertions are the
> contract — exit codes, "No cost records", mixed-known rendering, and the
> absence of model/run tables.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_costs.py -q`
Expected: FAIL — no `costs` command.

- [ ] **Step 3: Implement**

```python
# maestro/cli.py  (add the command + a renderer)
@app.command("costs")
def costs_command(
    db: Annotated[
        Path | None, typer.Option("--db", "-d", help="Path to SQLite database file")
    ] = None,
) -> None:
    """Database-wide cost summary (read-only) over recorded task costs.

    NOTE: this aggregates the whole database, which may span several runs
    (one DB survives --resume); it is a database-wide summary, not a run total.
    Costs of unpriced harnesses with no self-reported cost are shown as
    UNKNOWN, never as $0.

    Examples:
        maestro costs --db run/maestro.db
    """
    from maestro.cost_tracker import summarize_costs
    from maestro.database import DatabaseError, read_all_costs_readonly

    db_path = db or DEFAULT_DB_PATH

    async def _run() -> int:
        try:
            costs = await read_all_costs_readonly(db_path)
        except DatabaseError as exc:
            err_console.print(f"[red]{exc}[/red]")
            return 2
        if not costs:
            console.print("[dim]No cost records.[/dim]")
            return 0
        _render_cost_report(summarize_costs(costs))
        return 0

    raise typer.Exit(asyncio.run(_run()))


def _render_cost_report(report: "CostReport") -> None:
    from maestro.cost_tracker import CostGroup

    def _row(g: CostGroup) -> tuple[str, ...]:
        return (
            g.label,
            f"${g.known_cost_usd:.4f}",
            f"{g.input_tokens}/{g.output_tokens}",
            str(g.tasks),
            str(g.attempts),
            str(g.unknown_attempts),
            str(g.unknown_tasks),
        )

    def _table(title: str, first_col: str, groups: list[CostGroup]) -> Table:
        t = Table(title=title, show_header=True, header_style="bold")
        t.add_column(first_col)
        for col in ("Known $", "Tokens in/out", "Tasks", "Attempts",
                    "Unknown attempts", "Unknown tasks"):
            t.add_column(col, justify="right")
        for g in groups:
            t.add_row(*_row(g))
        return t

    console.print(_table("Cost — database-wide TOTAL", "Scope", [report.total]))
    console.print(_table("By harness", "Harness", report.by_harness))
    console.print(_table("By task", "Task", report.by_task))
```

Add `from maestro.cost_tracker import CostReport` under `TYPE_CHECKING` for the
annotation if pyrefly needs it (or import locally). Ensure `Table`, `console`,
`err_console` are already imported (they are).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli_costs.py -q`
Expected: PASS.

- [ ] **Step 5: Update CLAUDE.md**

Add under the Mode-2 / log-utilities CLI section:

```
uv run maestro costs --db maestro.db   # database-wide cost summary (read-only; TOTAL / by-harness / by-task; unpriced = UNKNOWN, not $0)
```

- [ ] **Step 6: Format, lint, type-check, full suite**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q`
Expected: clean; full suite green.

- [ ] **Step 7: Commit**

```bash
git add maestro/cli.py CLAUDE.md tests/test_cli_costs.py
git commit -m "feat(costs): maestro costs CLI (read-only database-wide summary)"
```

---

## Task 4: Verification & PR

- [ ] **Step 1: Full green gate**

Run: `uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check`
Expected: all pass, 0 pyrefly errors.

- [ ] **Step 2: Manual smoke**

Against a real dogfood DB (or a seeded one):
Run: `uv run maestro costs --db <path>` → tables render; `uv run maestro costs --db /tmp/none.db` → exit 2, no file created (`ls /tmp/none.db` absent).

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin feat/costs-cli
gh pr create --base master --title "feat: maestro costs read-only summary (idea #25)" \
  --body "<summary; link spec; note it is a database-wide summary (not run total); the by-run/by-model + COALESCE-unification follow-ups are out of scope>"
```

- [ ] **Step 4: Read Copilot review**

Address valid inline comments with follow-up commits; reply with rationale to
invalid ones. Do not merge (the user merges).

---

## Self-Review (plan vs spec)

- §2 cost rule (effective_cost SSOT), aggregator fields (known_cost_usd +
  unknown_attempt/task counts, tokens over rows) → Task 1.
- §3 groupings = TOTAL + by-harness + by-task; no by-model → Task 1 (no model
  dimension) + Task 3 (no model/run table, asserted).
- §4 CLI mixed-known columns + "No cost records" + exit codes → Task 3.
- §7 read-only connection contract (mode=ro, no file creation, required-column
  check, not immutable=1) → Task 2.
- §8 input/exit matrix + filesystem-immutability invariant → Task 2 tests
  (missing/dir/non-sqlite/missing-column, no-file-created, no-modify) + Task 3
  (`test_costs_missing_db_does_not_create_file`).
- §5 non-goals (no write path, no REST, no get_cost_summary/build_summary touch)
  → nothing in the plan modifies them; COALESCE unification is an out-of-scope
  follow-up noted in the PR body.
- §6 tests → distributed across Tasks 1–3.
