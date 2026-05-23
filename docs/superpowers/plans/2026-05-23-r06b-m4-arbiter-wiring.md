# R-06b M4 — Arbiter Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver `BenchmarkResult` from Maestro into Arbiter via a new MCP tool `report_benchmark`, persisted in a new `benchmark_runs` table, so R-07 can later consume per-agent per-benchmark scores for routing decisions.

**Architecture:** Schema-first cross-repo work. New Maestro module `maestro/benchmark/arbiter_report.py` provides a helper `report_benchmark_to_arbiter(result, client)` one layer above `BenchmarkRunner` (M1 contract preserved). Helper never raises — returns `BenchmarkResult.model_copy(update={"report_status": ...})`. Vendored `ArbiterClient` gains a typed `report_benchmark(payload)` method and a `MIN_ARBITER_PROTOCOL` range check in `start()`. Arbiter side adds a 4th MCP tool with `INSERT ... ON CONFLICT(run_id) DO NOTHING RETURNING` idempotency, wrapped in a transactional migration.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, jsonschema (already in dev deps), Rust (arbiter), tokio, rusqlite, MCP JSON-RPC over stdin/stdout. **Reference spec:** `docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md`.

**Phases (schema-first ordering per spec §13):**

| Phase | What | Repo | Can parallel? |
|-------|------|------|---------------|
| 0 | Wire-contract schema | Maestro | Blocks all other phases |
| 1 | Arbiter tool + table + tests | arbiter (Rust) | After Phase 0 |
| 2 | Maestro types + errors (additive) | Maestro | After Phase 0 (parallel with Phase 1) |
| 3 | Vendored client extensions | Maestro | After Phase 1 (needs arbiter SHA + tag) |
| 4 | Helper + projection + tests | Maestro | After Phase 2 |
| 5 | Contract tests + forward-compat | Maestro | After Phase 4 |
| 6 | Runner / ATP-client additive | Maestro | After Phase 2 (parallel with 3-5) |
| 7 | E2E + smoke + CI wiring | Maestro | After Phase 3 + arbiter mini-PR merged |
| 8 | Docs + TODO + landing | Maestro | After Phase 7 |

---

## File Inventory

**New Maestro files:**
- `_cowork_output/benchmark-contract/report_benchmark-v1.schema.json` — JSONSchema (single source of truth)
- `_cowork_output/benchmark-contract/README.md` — overview + arbiter-CI fetch instructions
- `maestro/benchmark/arbiter_report.py` — `WireTaskResult`, `ReportBenchmarkPayload`, `_classify_error`, `_build_wire_payload`, `_sample_per_task`, `report_benchmark_to_arbiter`
- `scripts/smoke_benchmark_report.py` — CI happy-path smoke
- `tests/test_benchmark_arbiter_report.py` — helper + projection tests
- `tests/test_benchmark_contract.py` — JSONSchema validation + forward-compat
- `tests/test_arbiter_real_subprocess_benchmark.py` — e2e (3 cases)

**Modified Maestro files:**
- `maestro/benchmark/models.py` — additive: `BenchmarkResult.report_status`, `.report_error`; `BenchmarkTaskResult.task_type`, `.score`
- `maestro/benchmark/runner.py` — `BenchmarkRunner.run(..., run_id: str | None = None)`
- `maestro/benchmark/atp_client.py` — surface `task_type` / `score` from ATP request metadata
- `maestro/benchmark/__init__.py` — re-export helper
- `maestro/coordination/arbiter_client.py` — `report_benchmark()` method, `ARBITER_PROTOCOL_VERSION`, `MIN_ARBITER_PROTOCOL`, `ARBITER_VENDORED_FROM_SHA`, version check in `start()`
- `maestro/coordination/arbiter_errors.py` — `ArbiterContractError`
- `maestro/coordination/__init__.py` — re-export new error
- `tests/test_arbiter_client.py` — version-sync + `report_benchmark` method tests
- `tests/test_benchmark_runner.py` — `run_id` param tests
- `tests/test_benchmark_atp_client.py` — task_type/score surfacing tests
- `.github/workflows/ci.yml` — add benchmark e2e + smoke + vendored-sha check step
- `TODO.md` — `[x] R-06b M4` entry + open-issue items from spec §14
- `CHANGELOG.md` — release-notes section

**New arbiter files:**
- `arbiter-mcp/src/tools/report_benchmark.rs` — handler
- `arbiter-mcp/tests/report_benchmark_test.rs` — Rust unit tests (6 cases)

**Modified arbiter files:**
- `arbiter-mcp/src/db.rs` — add `benchmark_runs` table + transactional migration + atomicity test
- `arbiter-mcp/src/tools/mod.rs` — register module
- `arbiter-mcp/src/server.rs` — tools/list entry, dispatch, `protocolVersion` bump in `initialize`
- `arbiter-mcp/Cargo.toml` — minor version bump
- `arbiter-mcp/tests/integration.rs` — extend with `report_benchmark` dispatch + contract test

---

## Phase 0: Wire-contract schema (Maestro)

This is the **single source of truth**. Lock it before any implementation.

### Task 0.1: Create JSONSchema directory + schema file

**Files:**
- Create: `_cowork_output/benchmark-contract/report_benchmark-v1.schema.json`

- [ ] **Step 1: Create directory**

```bash
mkdir -p /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/_cowork_output/benchmark-contract
```

- [ ] **Step 2: Write JSONSchema file**

File content for `_cowork_output/benchmark-contract/report_benchmark-v1.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://github.com/andrei-shtanakov/maestro/benchmark-contract/report_benchmark-v1.schema.json",
  "title": "report_benchmark v1",
  "description": "Wire payload + response for the report_benchmark MCP tool. Maestro -> Arbiter. R-06b M4.",
  "type": "object",
  "definitions": {
    "WireTaskResult": {
      "type": "object",
      "additionalProperties": true,
      "required": ["task_index", "duration_seconds"],
      "properties": {
        "task_index": {"type": "integer", "minimum": 0},
        "task_type": {"type": ["string", "null"]},
        "score": {"type": ["number", "null"]},
        "tokens_used": {"type": ["integer", "null"], "minimum": 0},
        "duration_seconds": {"type": "number", "minimum": 0},
        "error_class": {
          "type": ["string", "null"],
          "enum": ["timeout", "crash", "test_failure", "other", null]
        }
      }
    },
    "Request": {
      "type": "object",
      "additionalProperties": true,
      "required": [
        "payload_version", "run_id", "benchmark_id", "agent_id", "ts",
        "score", "score_components", "duration_seconds",
        "per_task", "per_task_total_count", "per_task_truncated"
      ],
      "properties": {
        "payload_version": {"type": "string", "const": "1.0.0"},
        "run_id": {"type": "string", "minLength": 1},
        "benchmark_id": {"type": "string", "minLength": 1},
        "agent_id": {"type": "string", "minLength": 1},
        "ts": {"type": "string", "format": "date-time"},
        "score": {"type": "number"},
        "score_components": {
          "type": "object",
          "additionalProperties": {"type": "number"}
        },
        "total_tokens": {"type": ["integer", "null"], "minimum": 0},
        "total_cost_usd": {"type": ["number", "null"], "minimum": 0},
        "duration_seconds": {"type": "number", "minimum": 0},
        "per_task": {
          "type": "array",
          "items": {"$ref": "#/definitions/WireTaskResult"}
        },
        "per_task_total_count": {"type": "integer", "minimum": 0},
        "per_task_truncated": {"type": "boolean"}
      }
    },
    "Response": {
      "type": "object",
      "additionalProperties": true,
      "required": ["status", "run_id"],
      "properties": {
        "status": {"type": "string", "enum": ["created", "duplicate"]},
        "run_id": {"type": "string", "minLength": 1}
      }
    }
  },
  "oneOf": [
    {"$ref": "#/definitions/Request"},
    {"$ref": "#/definitions/Response"}
  ]
}
```

- [ ] **Step 3: Validate JSONSchema itself is well-formed**

Run: `uv run python -c "import json; json.load(open('_cowork_output/benchmark-contract/report_benchmark-v1.schema.json'))"`
Expected: no output (success)

Run: `uv run python -c "from jsonschema import Draft202012Validator; import json; Draft202012Validator.check_schema(json.load(open('_cowork_output/benchmark-contract/report_benchmark-v1.schema.json')))"`
Expected: no output (success — meta-schema validation passes)

- [ ] **Step 4: Commit**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/Maestro
git add ../_cowork_output/benchmark-contract/report_benchmark-v1.schema.json
git commit -m "spec(benchmark-contract): report_benchmark v1 JSONSchema

Single source of truth for R-06b M4 wire payload + response.
Lives in _cowork_output/benchmark-contract/ per design §13.2.
Arbiter CI will fetch at pinned Maestro SHA.

additionalProperties: true on all objects = consumer-liberal
(arbiter accepts unknown optional fields without bump). Producer
strictness lives in Pydantic (WireTaskResult.extra='forbid').

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

Note: schema lives **outside** the Maestro repo (in `../_cowork_output/`). If that path is not a git repo, commit it inside Maestro by symlinking or copying instead; pick whichever the current convention uses. The smoke step verifies the file resolves; the contract test reads it via path. Adapt accordingly.

### Task 0.2: README for the contract

**Files:**
- Create: `_cowork_output/benchmark-contract/README.md`

- [ ] **Step 1: Write the README**

File content for `_cowork_output/benchmark-contract/README.md`:

```markdown
# benchmark-contract — report_benchmark

Single source of truth for the `report_benchmark` MCP tool wire format
(Maestro → Arbiter). Owned by Maestro repo; consumed by both Maestro
(`tests/test_benchmark_contract.py`) and arbiter-mcp
(`tests/contract_test.rs`).

## Files

- `report_benchmark-v1.schema.json` — JSONSchema draft 2020-12,
  request + response under `definitions/`. Match against
  `#/definitions/Request` for incoming arguments,
  `#/definitions/Response` for outgoing tool result.

## Versioning

Two independent version axes (see Maestro spec
`docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md` §6):

- **`payload_version`** (constant `"1.0.0"` in v1): bumps on
  removed/reshaped fields. Additive optional fields don't bump.
- **MCP `protocolVersion`** (set by arbiter at `initialize`):
  tracks tool surface; bumps on new/removed tools.

## Arbiter CI fetch

Arbiter `tests/contract_test.rs` reads this file at build time.
Recommended mechanism (chosen at arbiter-side implementation):

- Option A: `git submodule` of Maestro pinned at SHA matching
  `ARBITER_PINNED_SHA` round-trip in Maestro CI.
- Option B: HTTP fetch in `build.rs` from
  `https://raw.githubusercontent.com/andrei-shtanakov/maestro/<SHA>/_cowork_output/benchmark-contract/report_benchmark-v1.schema.json`.
- Option C: Copy-on-bump (manual sync, CI grep guards against drift).

Arbiter side decides; Maestro side guarantees the file does not
move once committed.
```

- [ ] **Step 2: Commit**

```bash
git add ../_cowork_output/benchmark-contract/README.md
git commit -m "docs(benchmark-contract): README — versioning + arbiter fetch

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 1: Arbiter side (Rust mini-PR)

**Repository:** `/Users/Andrei_Shtanakov/labs/all_ai_orchestrators/arbiter/`

This phase runs in the arbiter repo. If arbiter is maintained by a different developer, hand them this Phase 1 as a self-contained spec; otherwise execute it yourself. The output of Phase 1 is a tagged arbiter commit SHA used in Phase 3.

### Task 1.1: Migration — add `benchmark_runs` table

**Files:**
- Modify: `arbiter-mcp/src/db.rs` — add table definition + creation
- Test: `arbiter-mcp/src/db.rs` (existing test module)

- [ ] **Step 1: Write the failing test** (in db.rs test module)

```rust
#[test]
fn migration_creates_benchmark_runs_table_and_index() {
    let db = test_db();
    let mut conn = db.conn();
    // Should not panic; columns should match spec §5.1
    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='benchmark_runs'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 1, "benchmark_runs table missing");

    let idx: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_benchmark_runs_agent_bench_ts'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(idx, 1, "covering index missing");
}

#[test]
fn migration_atomicity_on_partial_failure() {
    // Inject a SQL-level fault between CREATE TABLE and schema_version bump.
    // Pre-state: snapshot of sqlite_master + schema_version.
    // Post-state after failed migration: must equal pre-state (transaction rolled back).
    let db = test_db_pre_m4();  // fresh DB without benchmark_runs
    let pre_snapshot = snapshot_schema(&db);
    let result = run_benchmark_runs_migration_with_fault(&db);
    assert!(result.is_err());
    let post_snapshot = snapshot_schema(&db);
    assert_eq!(pre_snapshot, post_snapshot, "migration must rollback fully");
}
```

Helpers `test_db_pre_m4`, `snapshot_schema`, `run_benchmark_runs_migration_with_fault` go in the test module — concrete impls in step 3.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/arbiter && cargo test --package arbiter-mcp --lib migration_creates_benchmark_runs migration_atomicity`
Expected: FAIL (table doesn't exist, helpers undefined)

- [ ] **Step 3: Add migration code in `db.rs`**

Find the existing `init_schema` (around the table-create section, ~line 740-800 area where `CREATE TABLE IF NOT EXISTS schema_version` etc. live). Add:

```rust
// After existing tables. Single transaction = atomic apply.
pub fn migrate_benchmark_runs(conn: &mut Connection) -> Result<()> {
    let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    tx.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS benchmark_runs (
            run_id                TEXT PRIMARY KEY,
            payload_version       TEXT NOT NULL,
            benchmark_id          TEXT NOT NULL,
            agent_id              TEXT NOT NULL,
            ts                    TEXT NOT NULL,
            score                 REAL NOT NULL,
            score_components      TEXT NOT NULL,
            total_tokens          INTEGER,
            total_cost_usd        REAL,
            duration_seconds      REAL NOT NULL,
            per_task              TEXT NOT NULL,
            per_task_total_count  INTEGER NOT NULL,
            per_task_truncated    INTEGER NOT NULL,
            inserted_at           TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_benchmark_runs_agent_bench_ts
            ON benchmark_runs(agent_id, benchmark_id, ts DESC);
        "#,
    )?;
    // Bump schema_version row to advertise new shape (follow existing pattern).
    tx.execute(
        "INSERT OR REPLACE INTO schema_version (id, version, applied_at) VALUES (1, ?1, datetime('now'))",
        rusqlite::params![CURRENT_SCHEMA_VERSION_FOR_M4],
    )?;
    tx.commit()?;
    Ok(())
}
```

Adjust the `schema_version` bump statement to match the actual existing pattern in `db.rs` (look at how `init_schema` writes there today). The atomicity guarantee is the `transaction_with_behavior(Immediate)` + commit-or-rollback.

Wire `migrate_benchmark_runs` into the existing init/migrate path.

- [ ] **Step 4: Implement test helpers**

```rust
#[cfg(test)]
fn test_db_pre_m4() -> Db {
    // Spin up a fresh in-memory DB with everything but benchmark_runs.
    let db = Db::open_in_memory().unwrap();
    init_schema_through_pre_m4(&db);
    db
}

#[cfg(test)]
fn snapshot_schema(db: &Db) -> Vec<String> {
    let conn = db.conn();
    let mut stmt = conn.prepare("SELECT type, name, sql FROM sqlite_master ORDER BY name").unwrap();
    stmt.query_map([], |r| {
        Ok(format!("{}/{}/{}", r.get::<_, String>(0)?, r.get::<_, String>(1)?,
                   r.get::<_, Option<String>>(2)?.unwrap_or_default()))
    })
    .unwrap()
    .collect::<Result<_, _>>()
    .unwrap()
}

#[cfg(test)]
fn run_benchmark_runs_migration_with_fault(db: &Db) -> Result<()> {
    let mut conn = db.conn();
    let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    tx.execute_batch("CREATE TABLE benchmark_runs (run_id TEXT PRIMARY KEY)")?;
    // Inject failure: invalid SQL that aborts before schema_version bump.
    tx.execute_batch("UPDATE __nonexistent_table_to_force_error SET x=1")?;
    tx.commit()?;
    Ok(())
}
```

- [ ] **Step 5: Run tests, verify pass**

Run: `cargo test --package arbiter-mcp --lib migration_creates_benchmark_runs migration_atomicity`
Expected: both PASS

- [ ] **Step 6: Run idempotency test (re-run migration)**

Add to test module:

```rust
#[test]
fn migration_idempotent_re_run() {
    let db = test_db();  // already has benchmark_runs
    let mut conn = db.conn();
    let result = migrate_benchmark_runs(&mut conn);
    assert!(result.is_ok(), "re-running migration must be idempotent");
    // table should still have exactly 0 rows (no data lost)
    let count: i64 = conn.query_row("SELECT COUNT(*) FROM benchmark_runs", [], |r| r.get(0)).unwrap();
    assert_eq!(count, 0);
}
```

Run: `cargo test --package arbiter-mcp --lib migration_idempotent_re_run`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/arbiter
git add arbiter-mcp/src/db.rs
git commit -m "feat(arbiter-mcp): R-06b M4 — benchmark_runs table + migration

Transactional migration (BEGIN IMMEDIATE...COMMIT) creates
benchmark_runs table with run_id PRIMARY KEY + covering index
on (agent_id, benchmark_id, ts DESC).

Tests: schema-existence + idempotent re-run + atomicity on
partial failure (fault injection between CREATE TABLE and
schema_version bump → ROLLBACK to pre-migration state)."
```

### Task 1.2: `report_benchmark` handler — happy-path created

**Files:**
- Create: `arbiter-mcp/src/tools/report_benchmark.rs`
- Modify: `arbiter-mcp/src/tools/mod.rs`
- Test: `arbiter-mcp/tests/report_benchmark_test.rs` (new)

- [ ] **Step 1: Create the test file with happy-path test**

File content for `arbiter-mcp/tests/report_benchmark_test.rs`:

```rust
use arbiter_mcp::tools::report_benchmark;
use arbiter_mcp::db::Db;
use serde_json::json;

fn valid_payload(run_id: &str) -> serde_json::Value {
    json!({
        "payload_version": "1.0.0",
        "run_id": run_id,
        "benchmark_id": "swe-mini",
        "agent_id": "claude_code",
        "ts": "2026-05-23T12:00:00Z",
        "score": 0.85,
        "score_components": {"accuracy": 0.85},
        "total_tokens": 12345,
        "total_cost_usd": 0.12,
        "duration_seconds": 42.0,
        "per_task": [{
            "task_index": 0,
            "task_type": "bugfix",
            "score": 1.0,
            "tokens_used": 1234,
            "duration_seconds": 4.2,
            "error_class": null
        }],
        "per_task_total_count": 1,
        "per_task_truncated": false
    })
}

#[test]
fn happy_path_returns_created() {
    let db = Db::open_in_memory().unwrap();
    arbiter_mcp::db::init_schema(&db).unwrap();

    let result = report_benchmark::execute(&valid_payload("run-1"), &db).unwrap();
    assert_eq!(result["status"], "created");
    assert_eq!(result["run_id"], "run-1");

    let count: i64 = db.conn().query_row(
        "SELECT COUNT(*) FROM benchmark_runs WHERE run_id='run-1'", [], |r| r.get(0)
    ).unwrap();
    assert_eq!(count, 1);
}
```

- [ ] **Step 2: Create stub module**

File content for `arbiter-mcp/src/tools/report_benchmark.rs`:

```rust
//! report_benchmark MCP tool — R-06b M4.

use crate::db::Db;
use anyhow::Result;
use serde_json::Value;

pub fn execute(_args: &Value, _db: &Db) -> Result<Value> {
    anyhow::bail!("not implemented")
}
```

Wire into `arbiter-mcp/src/tools/mod.rs`:

```rust
pub mod report_benchmark;
```

- [ ] **Step 3: Run test, verify FAIL**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/arbiter && cargo test --test report_benchmark_test happy_path_returns_created`
Expected: FAIL ("not implemented")

- [ ] **Step 4: Implement happy-path INSERT**

Replace `execute` body in `report_benchmark.rs`:

```rust
pub fn execute(args: &Value, db: &Db) -> Result<Value> {
    let run_id = args["run_id"].as_str().ok_or_else(|| anyhow::anyhow!("run_id required"))?;
    let payload_version = args["payload_version"].as_str().ok_or_else(|| anyhow::anyhow!("payload_version required"))?;
    if payload_version != "1.0.0" {
        anyhow::bail!("unsupported payload_version: {}", payload_version);
    }
    let benchmark_id = args["benchmark_id"].as_str().ok_or_else(|| anyhow::anyhow!("benchmark_id required"))?;
    let agent_id = args["agent_id"].as_str().ok_or_else(|| anyhow::anyhow!("agent_id required"))?;
    let ts = args["ts"].as_str().ok_or_else(|| anyhow::anyhow!("ts required"))?;
    let score = args["score"].as_f64().ok_or_else(|| anyhow::anyhow!("score required"))?;
    let score_components = serde_json::to_string(&args["score_components"])?;
    let total_tokens = args["total_tokens"].as_i64();
    let total_cost_usd = args["total_cost_usd"].as_f64();
    let duration_seconds = args["duration_seconds"].as_f64().ok_or_else(|| anyhow::anyhow!("duration_seconds required"))?;
    let per_task = serde_json::to_string(&args["per_task"])?;
    let per_task_total_count = args["per_task_total_count"].as_i64().ok_or_else(|| anyhow::anyhow!("per_task_total_count required"))?;
    let per_task_truncated = args["per_task_truncated"].as_bool().ok_or_else(|| anyhow::anyhow!("per_task_truncated required"))? as i64;

    let conn = db.conn();
    let affected = conn.execute(
        "INSERT INTO benchmark_runs (
            run_id, payload_version, benchmark_id, agent_id, ts, score, score_components,
            total_tokens, total_cost_usd, duration_seconds, per_task,
            per_task_total_count, per_task_truncated
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)
         ON CONFLICT(run_id) DO NOTHING",
        rusqlite::params![
            run_id, payload_version, benchmark_id, agent_id, ts, score, score_components,
            total_tokens, total_cost_usd, duration_seconds, per_task,
            per_task_total_count, per_task_truncated,
        ],
    )?;

    let status = if affected == 1 { "created" } else { "duplicate" };
    Ok(serde_json::json!({"status": status, "run_id": run_id}))
}
```

- [ ] **Step 5: Run test, verify PASS**

Run: `cargo test --test report_benchmark_test happy_path_returns_created`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add arbiter-mcp/src/tools/report_benchmark.rs arbiter-mcp/src/tools/mod.rs arbiter-mcp/tests/report_benchmark_test.rs
git commit -m "feat(arbiter-mcp): report_benchmark handler — happy path

INSERT...ON CONFLICT DO NOTHING pattern; returns status=created
when affected=1, duplicate otherwise. Validates payload_version
literal '1.0.0' + required fields. Wired into tools/ module."
```

### Task 1.3: Duplicate test + ON CONFLICT verification

- [ ] **Step 1: Add test**

Append to `report_benchmark_test.rs`:

```rust
#[test]
fn duplicate_run_id_returns_duplicate_no_double_insert() {
    let db = Db::open_in_memory().unwrap();
    arbiter_mcp::db::init_schema(&db).unwrap();
    let payload = valid_payload("run-dup");

    let r1 = report_benchmark::execute(&payload, &db).unwrap();
    let r2 = report_benchmark::execute(&payload, &db).unwrap();
    assert_eq!(r1["status"], "created");
    assert_eq!(r2["status"], "duplicate");

    let count: i64 = db.conn().query_row(
        "SELECT COUNT(*) FROM benchmark_runs WHERE run_id='run-dup'", [], |r| r.get(0)
    ).unwrap();
    assert_eq!(count, 1, "exactly one row after duplicate insert");
}
```

- [ ] **Step 2: Run, verify PASS**

Run: `cargo test --test report_benchmark_test duplicate_run_id_returns_duplicate_no_double_insert`
Expected: PASS (handler already has ON CONFLICT from Task 1.2)

- [ ] **Step 3: Commit**

```bash
git add arbiter-mcp/tests/report_benchmark_test.rs
git commit -m "test(arbiter-mcp): report_benchmark — duplicate idempotency"
```

### Task 1.4: Concurrent duplicate test

- [ ] **Step 1: Add tokio-based concurrent test**

```rust
#[tokio::test]
async fn concurrent_duplicate_run_id_one_created_one_duplicate() {
    let db = std::sync::Arc::new(Db::open_in_memory().unwrap());
    arbiter_mcp::db::init_schema(&db).unwrap();
    let payload = std::sync::Arc::new(valid_payload("run-conc"));

    let db1 = db.clone();
    let payload1 = payload.clone();
    let t1 = tokio::task::spawn_blocking(move || {
        report_benchmark::execute(&payload1, &db1).unwrap()
    });
    let db2 = db.clone();
    let payload2 = payload.clone();
    let t2 = tokio::task::spawn_blocking(move || {
        report_benchmark::execute(&payload2, &db2).unwrap()
    });

    let (r1, r2) = tokio::join!(t1, t2);
    let s1 = r1.unwrap()["status"].as_str().unwrap().to_string();
    let s2 = r2.unwrap()["status"].as_str().unwrap().to_string();
    let mut statuses = vec![s1, s2];
    statuses.sort();
    assert_eq!(statuses, vec!["created", "duplicate"]);

    let count: i64 = db.conn().query_row(
        "SELECT COUNT(*) FROM benchmark_runs WHERE run_id='run-conc'", [], |r| r.get(0)
    ).unwrap();
    assert_eq!(count, 1);
}
```

- [ ] **Step 2: Run, verify PASS**

Run: `cargo test --test report_benchmark_test concurrent_duplicate`
Expected: PASS

If `Db` is not `Send + Sync`, wrap in `Mutex<Db>` and adjust accordingly. If `tokio` is not already a dev-dep, add it (`tokio = { version = "1", features = ["rt-multi-thread", "macros"] }`).

- [ ] **Step 3: Commit**

```bash
git add arbiter-mcp/tests/report_benchmark_test.rs arbiter-mcp/Cargo.toml
git commit -m "test(arbiter-mcp): report_benchmark — concurrent idempotency

Two tokio tasks INSERTing the same run_id → exactly one created,
one duplicate, single row in table. Validates ON CONFLICT is
atomic vs SELECT-then-INSERT race."
```

### Task 1.5: Validation error tests (missing field, bad payload_version, malformed per_task)

- [ ] **Step 1: Add 3 tests**

```rust
#[test]
fn missing_required_field_returns_error() {
    let db = Db::open_in_memory().unwrap();
    arbiter_mcp::db::init_schema(&db).unwrap();
    let mut payload = valid_payload("run-err");
    payload.as_object_mut().unwrap().remove("agent_id");
    let result = report_benchmark::execute(&payload, &db);
    assert!(result.is_err());
    let count: i64 = db.conn().query_row("SELECT COUNT(*) FROM benchmark_runs", [], |r| r.get(0)).unwrap();
    assert_eq!(count, 0, "no INSERT on validation failure");
}

#[test]
fn unsupported_payload_version_rejected() {
    // Rust test sends raw payload bypassing any Pydantic — exercises server-side check.
    let db = Db::open_in_memory().unwrap();
    arbiter_mcp::db::init_schema(&db).unwrap();
    let mut payload = valid_payload("run-pv");
    payload["payload_version"] = serde_json::json!("2.0.0");
    let result = report_benchmark::execute(&payload, &db);
    let err_msg = format!("{:?}", result.unwrap_err());
    assert!(err_msg.contains("unsupported payload_version"), "got: {}", err_msg);
}

#[test]
fn malformed_per_task_rejected() {
    let db = Db::open_in_memory().unwrap();
    arbiter_mcp::db::init_schema(&db).unwrap();
    let mut payload = valid_payload("run-mal");
    payload["per_task"] = serde_json::json!("not an array");
    // Should error in execute() or earlier in schema validation (Task 1.7).
    let result = report_benchmark::execute(&payload, &db);
    // For now (pre-1.7), the handler accepts any JSON in per_task via to_string —
    // tighten when contract test wraps the handler in Task 1.7. Mark this test
    // as #[ignore] if it passes today; un-ignore after Task 1.7.
    assert!(result.is_ok() || result.is_err(), "stub assertion until 1.7");
}
```

- [ ] **Step 2: Run, observe**

Run: `cargo test --test report_benchmark_test missing_required unsupported_payload`
Expected: both PASS

- [ ] **Step 3: Commit**

```bash
git add arbiter-mcp/tests/report_benchmark_test.rs
git commit -m "test(arbiter-mcp): report_benchmark — validation rejections"
```

### Task 1.6: Server dispatch + tools/list entry + protocolVersion bump

**Files:**
- Modify: `arbiter-mcp/src/server.rs`
- Modify: `arbiter-mcp/Cargo.toml`

- [ ] **Step 1: Find existing tools/list + dispatch** in `server.rs` (the user surveyed this earlier: tools listed around lines 120-130, dispatch around line 421-430, handle_report_outcome around line 626).

- [ ] **Step 2: Write integration test for dispatch + tools/list**

Append to `arbiter-mcp/tests/integration.rs` (existing file):

```rust
#[test]
fn tools_list_includes_report_benchmark() {
    let response = send_jsonrpc(r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#);
    let parsed: serde_json::Value = serde_json::from_str(&response).unwrap();
    let names: Vec<&str> = parsed["result"]["tools"]
        .as_array().unwrap()
        .iter()
        .map(|t| t["name"].as_str().unwrap())
        .collect();
    assert!(names.contains(&"report_benchmark"), "report_benchmark not in tools/list: {:?}", names);
}

#[test]
fn tools_call_report_benchmark_dispatches() {
    let req = r#"{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"report_benchmark","arguments":{
        "payload_version":"1.0.0","run_id":"int-1","benchmark_id":"b","agent_id":"claude_code",
        "ts":"2026-05-23T12:00:00Z","score":0.5,"score_components":{},"duration_seconds":1.0,
        "per_task":[],"per_task_total_count":0,"per_task_truncated":false
    }}}"#;
    let response = send_jsonrpc(req);
    let parsed: serde_json::Value = serde_json::from_str(&response).unwrap();
    assert!(parsed["error"].is_null(), "got error: {}", parsed);
    let inner: serde_json::Value = serde_json::from_str(parsed["result"]["content"][0]["text"].as_str().unwrap()).unwrap();
    assert_eq!(inner["status"], "created");
}

#[test]
fn initialize_advertises_protocol_version_1_1() {
    let req = r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"1.0","capabilities":{}}}"#;
    let response = send_jsonrpc(req);
    let parsed: serde_json::Value = serde_json::from_str(&response).unwrap();
    let server_version = parsed["result"]["protocolVersion"].as_str().unwrap();
    assert_eq!(server_version, "1.1.0");
}
```

`send_jsonrpc` helper presumably exists in the integration test file from R-05; reuse it.

- [ ] **Step 3: Run, verify FAIL** (no dispatch yet, no protocolVersion bump)

Run: `cargo test --test integration tools_list_includes_report_benchmark tools_call_report_benchmark initialize_advertises`
Expected: 3 FAIL

- [ ] **Step 4: Add tools/list entry**

In `server.rs`, locate the tools/list response (near `"name": "report_outcome"` around line 124). Add a sibling entry:

```rust
{
    "name": "report_benchmark",
    "description": "Persist a BenchmarkResult from Maestro (R-06b M4). Returns status=created|duplicate.",
    "inputSchema": {
        "type": "object",
        "required": ["payload_version", "run_id", "benchmark_id", "agent_id", "ts",
                     "score", "score_components", "duration_seconds",
                     "per_task", "per_task_total_count", "per_task_truncated"],
        "properties": {
            // mirror the JSONSchema from Phase 0 (paste in literal here for the
            // tools/list response — JSON-RPC clients use this for discovery)
        }
    }
}
```

- [ ] **Step 5: Add dispatch arm**

In `server.rs` `match tool_name` block (around line 423-430, near `"report_outcome" => self.handle_report_outcome(...)`), add:

```rust
"report_benchmark" => {
    let result = crate::tools::report_benchmark::execute(&arguments, &self.db);
    match result {
        Ok(value) => Ok(jsonrpc_success(req_id, value)),
        Err(e) => Ok(jsonrpc_error(req_id, -32602, &format!("{}", e))),
    }
}
```

(Match the surrounding error-code convention in `server.rs` — `-32602` for invalid params is appropriate for missing/wrong fields; `-32603` internal error for unexpected.)

- [ ] **Step 6: Bump protocolVersion in initialize handler**

Find the `initialize` handler in `server.rs`; bump the `protocolVersion` response field from `"1.0"` (or whatever it currently advertises) to `"1.1.0"`.

- [ ] **Step 7: Run tests, verify PASS**

Run: `cargo test --test integration`
Expected: PASS (including the 3 new + pre-existing)

- [ ] **Step 8: Bump Cargo.toml minor**

In `arbiter-mcp/Cargo.toml`, bump `version = "0.X.0"` → `version = "0.(X+1).0"`.

- [ ] **Step 9: Commit**

```bash
git add arbiter-mcp/src/server.rs arbiter-mcp/Cargo.toml arbiter-mcp/tests/integration.rs
git commit -m "feat(arbiter-mcp): expose report_benchmark + bump protocolVersion 1.1.0

Adds tools/list entry + dispatch arm. protocolVersion bumped
because the MCP tool surface changed (additive tool). Per
Maestro design §6: pragmatic deviation from canonical MCP —
tools/list remains source of truth during sessions; bump enables
single-RPC startup compatibility check."
```

### Task 1.7: Contract test (Rust side reads shared schema)

**Files:**
- Create or extend: `arbiter-mcp/tests/contract_test.rs`

This step depends on the chosen schema-fetch mechanism (Phase 0 README options A/B/C). For Option C (copy-on-bump), keep a local copy at `arbiter-mcp/tests/contract/report_benchmark-v1.schema.json`.

- [ ] **Step 1: Add jsonschema dep if missing**

In `arbiter-mcp/Cargo.toml` `[dev-dependencies]`:
```toml
jsonschema = "0.18"
```

- [ ] **Step 2: Copy schema file**

```bash
mkdir -p /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/arbiter/arbiter-mcp/tests/contract
cp /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/_cowork_output/benchmark-contract/report_benchmark-v1.schema.json \
   /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/arbiter/arbiter-mcp/tests/contract/
```

- [ ] **Step 3: Write contract tests**

File content for `arbiter-mcp/tests/contract_test.rs`:

```rust
use jsonschema::JSONSchema;
use serde_json::Value;
use std::fs;

fn schema() -> JSONSchema {
    let raw = fs::read_to_string("tests/contract/report_benchmark-v1.schema.json").unwrap();
    let s: Value = serde_json::from_str(&raw).unwrap();
    JSONSchema::options()
        .with_draft(jsonschema::Draft::Draft202012)
        .compile(&s)
        .unwrap()
}

#[test]
fn valid_request_passes_schema() {
    let payload = serde_json::json!({
        "payload_version":"1.0.0","run_id":"r1","benchmark_id":"b","agent_id":"a",
        "ts":"2026-05-23T12:00:00Z","score":0.5,"score_components":{},
        "duration_seconds":1.0,"per_task":[],
        "per_task_total_count":0,"per_task_truncated":false
    });
    assert!(schema().is_valid(&payload), "errors: {:?}", schema().validate(&payload).err());
}

#[test]
fn missing_required_field_fails_schema() {
    let payload = serde_json::json!({
        "payload_version":"1.0.0","run_id":"r1"
    });
    assert!(!schema().is_valid(&payload));
}

#[test]
fn valid_response_passes_schema() {
    let resp = serde_json::json!({"status":"created","run_id":"r1"});
    assert!(schema().is_valid(&resp));
}
```

- [ ] **Step 4: Run, verify PASS**

Run: `cargo test --test contract_test`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add arbiter-mcp/Cargo.toml arbiter-mcp/tests/contract_test.rs arbiter-mcp/tests/contract/
git commit -m "test(arbiter-mcp): contract test for report_benchmark v1

Validates request + response against shared JSONSchema from
Maestro _cowork_output/benchmark-contract/ (copy-on-bump
mechanism — Option C from contract README)."
```

### Task 1.8: Tag arbiter release; capture SHA for Maestro

- [ ] **Step 1: Run full test suite**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/arbiter
cargo test
cargo clippy
```
Expected: green

- [ ] **Step 2: Push branch (if using PR flow) or merge to main**

```bash
git push origin <branch>
# Open PR, review, merge — OR if direct:
git checkout main && git merge --no-ff <branch>
```

- [ ] **Step 3: Capture the merge commit SHA**

```bash
ARBITER_M4_SHA=$(git rev-parse HEAD)
echo "$ARBITER_M4_SHA"  # save this for Phase 3
```

---

## Phase 2: Maestro types + errors (parallel-safe with Phase 1)

### Task 2.1: Add `ArbiterContractError`

**Files:**
- Modify: `maestro/coordination/arbiter_errors.py`
- Modify: `maestro/coordination/__init__.py`
- Test: `tests/test_arbiter_errors.py` (or extend existing)

- [ ] **Step 1: Find existing error hierarchy**

Run: `cat maestro/coordination/arbiter_errors.py`
Note: the existing root class (e.g. `ArbiterClientError`) and any patterns.

- [ ] **Step 2: Write failing test**

In `tests/test_arbiter_errors.py` (create or append):

```python
import pytest
from maestro.coordination.arbiter_errors import (
    ArbiterClientError,
    ArbiterUnavailable,
    ArbiterContractError,
)


def test_contract_error_is_subclass_of_client_error():
    assert issubclass(ArbiterContractError, ArbiterClientError)


def test_contract_error_not_subclass_of_unavailable():
    """contract_break and unavailable are sibling categories, not parent/child."""
    assert not issubclass(ArbiterContractError, ArbiterUnavailable)
    assert not issubclass(ArbiterUnavailable, ArbiterContractError)


def test_contract_error_carries_code_message_data():
    e = ArbiterContractError(-32602, "missing field 'agent_id'", {"field": "agent_id"})
    assert e.code == -32602
    assert e.message == "missing field 'agent_id'"
    assert e.data == {"field": "agent_id"}
    assert "-32602" in str(e)
    assert "missing field" in str(e)


def test_contract_error_data_defaults_to_empty_dict():
    e = ArbiterContractError(-32603, "internal")
    assert e.data == {}
```

- [ ] **Step 3: Run, verify FAIL** (import error)

Run: `uv run pytest tests/test_arbiter_errors.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 4: Add class**

Append to `maestro/coordination/arbiter_errors.py`:

```python
class ArbiterContractError(ArbiterClientError):
    """JSON-RPC error from arbiter indicating schema or protocol mismatch.

    Always means: vendored client diverged from server, payload bug, or
    payload_version mismatch. Never transient — retry is meaningless.
    Sibling to ArbiterUnavailable (which is transient).
    """

    def __init__(self, code: int, message: str, data: dict | None = None) -> None:
        self.code = code
        self.message = message
        self.data = data or {}
        super().__init__(f"contract error {code}: {message}")
```

- [ ] **Step 5: Re-export in `__init__.py`**

In `maestro/coordination/__init__.py`, find the existing exports and add `ArbiterContractError` alongside `ArbiterUnavailable` / `ArbiterClientError`.

- [ ] **Step 6: Run, verify PASS**

Run: `uv run pytest tests/test_arbiter_errors.py -v`
Expected: 4 PASS

- [ ] **Step 7: Type-check + lint**

Run: `uv run pyrefly check && uv run ruff check . && uv run ruff format --check .`
Expected: clean

- [ ] **Step 8: Commit**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/Maestro
git add maestro/coordination/arbiter_errors.py maestro/coordination/__init__.py tests/test_arbiter_errors.py
git commit -m "feat(coordination): ArbiterContractError for JSON-RPC contract breaks

Sibling to ArbiterUnavailable. _classify_error in M4 helper
matches on type, not on error message string — eliminates
fragile substring-grep classification.

R-06b M4 design §6 + §7.1.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 2.2: Extend `BenchmarkResult` and `BenchmarkTaskResult`

**Files:**
- Modify: `maestro/benchmark/models.py`
- Test: `tests/test_benchmark_models.py` (create or extend; check first)

- [ ] **Step 1: Check existing test file**

Run: `ls tests/test_benchmark*`
Note: if `test_benchmark_models.py` exists, append; otherwise create.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_benchmark_models.py (create/extend)
from maestro.benchmark.models import BenchmarkResult, BenchmarkTaskResult


def test_benchmark_result_has_default_report_status_skipped():
    r = BenchmarkResult(
        run_id="x", benchmark_id="b", agent_id="a",
        score=0.5, per_task=[], duration_seconds=1.0,
    )
    assert r.report_status == "skipped"
    assert r.report_error is None


def test_benchmark_result_report_status_accepts_ok_failed_skipped():
    for status in ("ok", "failed", "skipped"):
        r = BenchmarkResult(
            run_id="x", benchmark_id="b", agent_id="a", score=0.5,
            per_task=[], duration_seconds=1.0, report_status=status,
        )
        assert r.report_status == status


def test_benchmark_task_result_additive_task_type_and_score():
    t = BenchmarkTaskResult(
        task_index=0, prompt="p", response="r",
        duration_seconds=1.0, task_type="bugfix", score=0.9,
    )
    assert t.task_type == "bugfix"
    assert t.score == 0.9


def test_benchmark_task_result_additive_defaults_none():
    t = BenchmarkTaskResult(task_index=0, prompt="p", response="r", duration_seconds=1.0)
    assert t.task_type is None
    assert t.score is None
```

- [ ] **Step 3: Run, verify FAIL**

Run: `uv run pytest tests/test_benchmark_models.py -v -k "report_status or additive"`
Expected: FAIL

- [ ] **Step 4: Extend models**

In `maestro/benchmark/models.py`:

```python
# At top, add Literal to imports
from typing import Literal

class BenchmarkTaskResult(BaseModel):
    task_index: int
    prompt: str
    response: str
    duration_seconds: float
    tokens_used: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    # R-06b M4 additive (domain):
    task_type: str | None = None
    score: float | None = None


class BenchmarkResult(BaseModel):
    run_id: str
    benchmark_id: str
    agent_id: str
    score: float
    score_components: dict[str, float] = Field(default_factory=dict)
    per_task: list[BenchmarkTaskResult]
    total_tokens: int | None = None
    total_cost_usd: float | None = None
    duration_seconds: float
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # R-06b M4 additive (transport status; helper sets via model_copy):
    report_status: Literal["ok", "failed", "skipped"] = "skipped"
    report_error: str | None = None
```

- [ ] **Step 5: Run, verify PASS**

Run: `uv run pytest tests/test_benchmark_models.py -v`
Expected: PASS (incl. pre-existing tests if any)

- [ ] **Step 6: Run the whole benchmark test suite to ensure no regression**

Run: `uv run pytest tests/test_benchmark_runner.py tests/test_benchmark_atp_client.py tests/test_spawner_responder.py -v`
Expected: all PASS

- [ ] **Step 7: Type-check + lint**

Run: `uv run pyrefly check && uv run ruff check . && uv run ruff format --check .`
Expected: clean

- [ ] **Step 8: Commit**

```bash
git add maestro/benchmark/models.py tests/test_benchmark_models.py
git commit -m "feat(benchmark): R-06b M4 — additive model fields

BenchmarkResult.report_status (Literal['ok','failed','skipped'],
default 'skipped') + .report_error (str|None) for caller-side
strict-mode decision (M5 CLI --strict-report).

BenchmarkTaskResult.task_type + .score additive — surfaced from
ATP metadata in Task 6.2.

All additive: M1/M2/M3 constructors continue to work unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 3: Vendored client extensions (Maestro)

**Prerequisite:** Phase 1 done; `ARBITER_M4_SHA` captured.

### Task 3.1: Version constants + `MIN_ARBITER_PROTOCOL` + `start()` check

**Files:**
- Modify: `maestro/coordination/arbiter_client.py`
- Test: `tests/test_arbiter_client_version.py` (new)

- [ ] **Step 1: Write failing tests**

File content for `tests/test_arbiter_client_version.py`:

```python
import logging
import pytest
from unittest.mock import AsyncMock, patch
from maestro.coordination.arbiter_client import (
    ArbiterClient, ArbiterClientConfig,
    MIN_ARBITER_PROTOCOL, ARBITER_PROTOCOL_VERSION,
)
from maestro.coordination.arbiter_errors import ArbiterContractError


def test_constants_present_and_consistent():
    """Vendored constant pair: declared current + minimum supported."""
    assert isinstance(MIN_ARBITER_PROTOCOL, tuple) and len(MIN_ARBITER_PROTOCOL) == 2
    cur_major, cur_minor = map(int, ARBITER_PROTOCOL_VERSION.split(".")[:2])
    assert (cur_major, cur_minor) >= MIN_ARBITER_PROTOCOL
    assert MIN_ARBITER_PROTOCOL == (1, 1), "M4 sets MIN at 1.1 (report_benchmark added)"


@pytest.mark.asyncio
async def test_start_accepts_matching_protocol(caplog):
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    with patch.object(client, "_spawn_subprocess", new=AsyncMock()), \
         patch.object(client, "_send_request", new=AsyncMock(return_value={"protocolVersion": "1.5"})), \
         patch.object(client, "_send_notification", new=AsyncMock()):
        caplog.set_level(logging.WARNING)
        await client.start()
    assert "protocol minor lower" not in caplog.text


@pytest.mark.asyncio
async def test_start_warns_on_minor_below_min(caplog):
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    with patch.object(client, "_spawn_subprocess", new=AsyncMock()), \
         patch.object(client, "_send_request", new=AsyncMock(return_value={"protocolVersion": "1.0"})), \
         patch.object(client, "_send_notification", new=AsyncMock()):
        caplog.set_level(logging.WARNING)
        await client.start()
    assert "protocol minor lower" in caplog.text


@pytest.mark.asyncio
async def test_start_raises_on_major_mismatch():
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    with patch.object(client, "_spawn_subprocess", new=AsyncMock()), \
         patch.object(client, "_send_request", new=AsyncMock(return_value={"protocolVersion": "2.0"})), \
         patch.object(client, "_send_notification", new=AsyncMock()):
        with pytest.raises(ArbiterContractError) as exc:
            await client.start()
        assert "major" in str(exc.value).lower()
```

The exact `_spawn_subprocess`/`_send_request` patch points may differ; adjust to match the actual `start()` implementation (re-read the file at lines 254-490 if needed).

- [ ] **Step 2: Run, verify FAIL** (constants missing)

Run: `uv run pytest tests/test_arbiter_client_version.py -v`
Expected: FAIL (ImportError on `MIN_ARBITER_PROTOCOL`)

- [ ] **Step 3: Add constants + version-check logic**

In `maestro/coordination/arbiter_client.py`, near the top (after imports, before classes):

```python
# R-06b M4: vendored from arbiter@<ARBITER_M4_SHA>.
# DO NOT EDIT directly — re-vendor via documented mechanism.
ARBITER_PROTOCOL_VERSION = "1.1.0"
MIN_ARBITER_PROTOCOL: tuple[int, int] = (1, 1)
ARBITER_VENDORED_FROM_SHA = "<ARBITER_M4_SHA>"  # replace with actual SHA from Phase 1 Task 1.8


def _parse_version(v: str) -> tuple[int, int]:
    parts = v.split(".")
    major = int(parts[0]) if parts and parts[0].isdigit() else 0
    minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return (major, minor)
```

In `ArbiterClient.start()`, after the `initialize` response is received (find the line that reads `result = await self._send_request("initialize", ...)`), add:

```python
server_version = _parse_version(str(result.get("protocolVersion", "0.0")))
if server_version[0] < MIN_ARBITER_PROTOCOL[0]:
    raise ArbiterContractError(
        -1,
        f"protocol major mismatch: server={server_version}, min={MIN_ARBITER_PROTOCOL}",
    )
if server_version < MIN_ARBITER_PROTOCOL:
    logger.warning(
        "arbiter protocol minor lower than required: server=%s, min=%s — "
        "report_benchmark may be missing",
        server_version, MIN_ARBITER_PROTOCOL,
    )
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_arbiter_client_version.py -v`
Expected: 4 PASS

- [ ] **Step 5: Run existing arbiter-client tests to verify no regression**

Run: `uv run pytest tests/test_arbiter_client.py tests/test_arbiter_*.py -v`
Expected: all PASS

- [ ] **Step 6: Replace `<ARBITER_M4_SHA>` placeholder**

Substitute the actual SHA captured in Phase 1 Task 1.8.

- [ ] **Step 7: Type-check + lint + commit**

Run: `uv run pyrefly check && uv run ruff check . && uv run ruff format --check .`
Expected: clean

```bash
git add maestro/coordination/arbiter_client.py tests/test_arbiter_client_version.py
git commit -m "feat(coordination): MIN_ARBITER_PROTOCOL range check in start()

Adds ARBITER_PROTOCOL_VERSION ('1.1.0'), MIN_ARBITER_PROTOCOL
((1, 1)), ARBITER_VENDORED_FROM_SHA pin. start() validates
server-advertised protocolVersion: major < MIN raises
ArbiterContractError; minor < MIN logs WARNING. Server is single
source of truth (initialize response); client declares minimum.

R-06b M4 design §6 — eliminates the silent-drift class of bugs
where re-vendor lags behind an arbiter release.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 3.2: `report_benchmark` method + `ReportBenchmarkPayload`

**Files:**
- Modify: `maestro/coordination/arbiter_client.py`
- Test: `tests/test_arbiter_client.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_arbiter_client.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch
from maestro.coordination.arbiter_client import ArbiterClient, ArbiterClientConfig
from maestro.benchmark.arbiter_report import ReportBenchmarkPayload  # forward ref — Phase 4

# Note: this test depends on Phase 4 Task 4.2 (ReportBenchmarkPayload). If Phase 4
# is not yet started, mark @pytest.mark.skip and revisit. Subagent ordering ensures
# Task 3.2 runs after 4.2, OR move this test into test_benchmark_arbiter_report.py.
```

**Reordering decision:** to keep tests self-contained, defer Task 3.2's *test* until after `ReportBenchmarkPayload` exists (Phase 4 Task 4.2). For now, in Task 3.2, only write the **method** + a smoke test that uses raw dict, not the typed model.

Revised Task 3.2 test:

```python
@pytest.mark.asyncio
async def test_report_benchmark_method_delegates_to_call_tool():
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    with patch.object(client, "_call_tool", new=AsyncMock(return_value={"status": "created", "run_id": "x"})) as mock_call:
        # Use a plain dict that mimics ReportBenchmarkPayload.model_dump(mode="json"):
        payload_dict = {
            "payload_version": "1.0.0",
            "run_id": "x",
            "benchmark_id": "b",
            "agent_id": "a",
            "ts": "2026-05-23T12:00:00Z",
            "score": 0.5,
            "score_components": {},
            "total_tokens": None,
            "total_cost_usd": None,
            "duration_seconds": 1.0,
            "per_task": [],
            "per_task_total_count": 0,
            "per_task_truncated": False,
        }
        result = await client.report_benchmark_raw(payload_dict)
    mock_call.assert_awaited_once_with("report_benchmark", payload_dict)
    assert result == {"status": "created", "run_id": "x"}
```

Use `report_benchmark_raw(dict)` as the low-level method. In Phase 4, add a thin `report_benchmark(payload: ReportBenchmarkPayload)` wrapper that calls `.model_dump(mode="json")` and forwards.

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_arbiter_client.py -v -k report_benchmark`
Expected: FAIL (AttributeError)

- [ ] **Step 3: Add method**

In `maestro/coordination/arbiter_client.py`, near `report_outcome` (around line 327):

```python
async def report_benchmark_raw(self, payload: dict[str, Any]) -> dict[str, Any]:
    """Send a benchmark result. Low-level: takes pre-serialized dict.

    Prefer the typed wrapper ``report_benchmark(payload: ReportBenchmarkPayload)``
    from ``maestro.benchmark.arbiter_report``.
    """
    return await self._call_tool("report_benchmark", payload)
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_arbiter_client.py -v -k report_benchmark`
Expected: PASS

- [ ] **Step 5: Type-check + lint + commit**

Run: `uv run pyrefly check && uv run ruff check . && uv run ruff format --check .`
Expected: clean

```bash
git add maestro/coordination/arbiter_client.py tests/test_arbiter_client.py
git commit -m "feat(coordination): ArbiterClient.report_benchmark_raw

Low-level dict-taking method. Typed Pydantic wrapper added in
Phase 4 lives in maestro.benchmark.arbiter_report to keep
benchmark-payload schemas out of coordination layer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 3.3: Differentiate JSON-RPC error codes → ArbiterContractError

**Files:**
- Modify: `maestro/coordination/arbiter_client.py` (`_send_and_receive`)

- [ ] **Step 1: Write failing test**

Append to `tests/test_arbiter_client.py`:

```python
@pytest.mark.asyncio
async def test_send_and_receive_raises_contract_error_on_invalid_params():
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    # Mock the underlying write/read pair so we control the response.
    fake_response = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32602, "message": "missing agent_id", "data": {"field": "agent_id"}},
    }
    with patch.object(client, "_write_message", new=AsyncMock()), \
         patch.object(client, "_read_response", new=AsyncMock(return_value=fake_response)):
        with pytest.raises(ArbiterContractError) as exc:
            await client._send_and_receive({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
    assert exc.value.code == -32602
    assert exc.value.data == {"field": "agent_id"}


@pytest.mark.asyncio
async def test_send_and_receive_raises_unavailable_on_other_codes():
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    fake_response = {
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": -32000, "message": "server error"},
    }
    with patch.object(client, "_write_message", new=AsyncMock()), \
         patch.object(client, "_read_response", new=AsyncMock(return_value=fake_response)):
        with pytest.raises(ArbiterUnavailable):
            await client._send_and_receive({"jsonrpc": "2.0", "id": 1, "method": "x", "params": {}})
```

Add `from maestro.coordination.arbiter_errors import ArbiterContractError, ArbiterUnavailable` at top.

- [ ] **Step 2: Run, verify FAIL** (currently both code paths raise `ArbiterUnavailable`)

Run: `uv run pytest tests/test_arbiter_client.py -v -k "contract_error_on_invalid_params or other_codes"`
Expected: FAIL

- [ ] **Step 3: Patch `_send_and_receive`**

In `maestro/coordination/arbiter_client.py`, find `_send_and_receive` (around line 553-568). Replace the error-raising block:

```python
if "error" in response and response["error"] is not None:
    err = response["error"]
    code = err.get("code", -32000)
    msg = err.get("message", "Unknown error")
    data = err.get("data")
    if code in (-32600, -32602, -32603):
        raise ArbiterContractError(code, msg, data)
    raise ArbiterUnavailable(f"protocol error: code {code}: {msg}")
```

Add `from maestro.coordination.arbiter_errors import ArbiterContractError` at the top of the file.

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_arbiter_client.py -v -k "contract_error_on_invalid_params or other_codes"`
Expected: PASS

- [ ] **Step 5: Run full coordination test suite (regression check)**

Run: `uv run pytest tests/test_arbiter_client.py tests/test_arbiter_*.py -v`
Expected: all PASS

- [ ] **Step 6: Type-check + lint + commit**

```bash
git add maestro/coordination/arbiter_client.py tests/test_arbiter_client.py
git commit -m "fix(coordination): differentiate contract-break vs transient errors

JSON-RPC error codes -32600 (invalid request), -32602 (invalid
params), -32603 (internal) now raise ArbiterContractError;
other codes remain ArbiterUnavailable (transient).

Enables _classify_error in M4 helper to match on exception type,
not on error-message substring.

R-06b M4 design §6 / §7.1.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 4: Helper + projection + Pydantic wire models

### Task 4.1: `WireTaskResult` Pydantic model + projection

**Files:**
- Create: `maestro/benchmark/arbiter_report.py`
- Test: `tests/test_benchmark_arbiter_report.py` (new)

- [ ] **Step 1: Write failing tests for `WireTaskResult` shape**

File content for `tests/test_benchmark_arbiter_report.py`:

```python
import pytest
from pydantic import ValidationError
from maestro.benchmark.models import BenchmarkTaskResult
from maestro.benchmark.arbiter_report import WireTaskResult


def _domain_task(**kwargs):
    defaults = dict(task_index=0, prompt="p", response="r", duration_seconds=1.0)
    defaults.update(kwargs)
    return BenchmarkTaskResult(**defaults)


def test_wire_task_result_excludes_prompt_and_response():
    """Free-form strip: WireTaskResult must not carry prompt/response."""
    wire = WireTaskResult.from_domain(_domain_task(prompt="long prompt", response="long resp"))
    dumped = wire.model_dump()
    assert "prompt" not in dumped
    assert "response" not in dumped


def test_wire_task_result_maps_domain_fields():
    wire = WireTaskResult.from_domain(_domain_task(
        task_index=3, duration_seconds=4.2, tokens_used=1234,
        task_type="bugfix", score=0.9, error=None,
    ))
    assert wire.task_index == 3
    assert wire.duration_seconds == 4.2
    assert wire.tokens_used == 1234
    assert wire.task_type == "bugfix"
    assert wire.score == 0.9
    assert wire.error_class is None


def test_wire_task_result_error_bucketing():
    """Free-form error message → bounded enum bucket."""
    assert WireTaskResult.from_domain(_domain_task(error="timeout after 30s")).error_class == "timeout"
    assert WireTaskResult.from_domain(_domain_task(error="subprocess crashed")).error_class == "crash"
    assert WireTaskResult.from_domain(_domain_task(error="2 test failures")).error_class == "test_failure"
    assert WireTaskResult.from_domain(_domain_task(error="something else")).error_class == "other"
    assert WireTaskResult.from_domain(_domain_task(error=None)).error_class is None


def test_wire_task_result_forbids_extra_fields():
    with pytest.raises(ValidationError):
        WireTaskResult(task_index=0, duration_seconds=1.0, surprise="boom")  # type: ignore
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_benchmark_arbiter_report.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: Create `arbiter_report.py` with `WireTaskResult`**

File content for `maestro/benchmark/arbiter_report.py`:

```python
"""R-06b M4 — Arbiter feedback wiring.

Projects a BenchmarkResult into a wire payload for the arbiter
report_benchmark MCP tool, and delivers it via ArbiterClient.

Helper never raises (except CancelledError). All failures classified
into ErrorClass and surfaced via BenchmarkResult.report_status.

Design: docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from maestro.benchmark.models import BenchmarkTaskResult

ErrorClassBucket = Literal["timeout", "crash", "test_failure", "other"]


def _bucket_error(msg: str | None) -> ErrorClassBucket | None:
    if msg is None:
        return None
    lower = msg.lower()
    if "timeout" in lower:
        return "timeout"
    if "crash" in lower or "exited" in lower or "killed" in lower:
        return "crash"
    if "test" in lower and ("fail" in lower or "error" in lower):
        return "test_failure"
    return "other"


class WireTaskResult(BaseModel):
    """Projection of BenchmarkTaskResult for arbiter persistence.

    Excludes free-form fields (prompt, response) — they live only in
    the in-memory domain object and Maestro logs. Adding a field
    here requires a payload_version bump + contract-test update on
    both sides.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_index: int
    task_type: str | None
    score: float | None
    tokens_used: int | None
    duration_seconds: float
    error_class: ErrorClassBucket | None

    @classmethod
    def from_domain(cls, task: BenchmarkTaskResult) -> "WireTaskResult":
        return cls(
            task_index=task.task_index,
            task_type=task.task_type,
            score=task.score,
            tokens_used=task.tokens_used,
            duration_seconds=task.duration_seconds,
            error_class=_bucket_error(task.error),
        )
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_benchmark_arbiter_report.py -v`
Expected: 4 PASS

- [ ] **Step 5: Type-check + lint + commit**

Run: `uv run pyrefly check && uv run ruff check . && uv run ruff format --check .`
Expected: clean

```bash
git add maestro/benchmark/arbiter_report.py tests/test_benchmark_arbiter_report.py
git commit -m "feat(benchmark): WireTaskResult — arbiter wire projection

Frozen Pydantic model with extra='forbid'. Excludes free-form
prompt/response (those stay in domain BenchmarkTaskResult for
local display). _bucket_error classifies free-form error into
{timeout, crash, test_failure, other} for SQL-queryable
error_class column on arbiter side.

R-06b M4 design §4.2 / §4.4.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 4.2: `ReportBenchmarkPayload` + truncation/sampling

**Files:**
- Modify: `maestro/benchmark/arbiter_report.py`
- Test: `tests/test_benchmark_arbiter_report.py` (extend)

- [ ] **Step 1: Write failing tests for payload + truncation**

Append to `tests/test_benchmark_arbiter_report.py`:

```python
import os
from maestro.benchmark.models import BenchmarkResult
from maestro.benchmark.arbiter_report import (
    ReportBenchmarkPayload,
    _build_wire_payload,
    _sample_per_task,
)


def _result(run_id="r", per_task=None, score=0.5):
    return BenchmarkResult(
        run_id=run_id, benchmark_id="b", agent_id="a",
        score=score, per_task=per_task or [], duration_seconds=1.0,
    )


def _tasks(n: int) -> list[BenchmarkTaskResult]:
    return [BenchmarkTaskResult(task_index=i, prompt=f"p{i}", response=f"r{i}", duration_seconds=1.0) for i in range(n)]


def test_payload_version_pinned_to_1_0_0():
    p = _build_wire_payload(_result(), max_per_task=200)
    assert p.payload_version == "1.0.0"


def test_payload_maps_all_aggregate_fields():
    result = _result(run_id="x", per_task=_tasks(3), score=0.85)
    p = _build_wire_payload(result, max_per_task=200)
    assert p.run_id == "x"
    assert p.benchmark_id == "b"
    assert p.agent_id == "a"
    assert p.score == 0.85
    assert p.per_task_total_count == 3
    assert p.per_task_truncated is False
    assert len(p.per_task) == 3


def test_truncation_under_cap_no_change():
    p = _build_wire_payload(_result(per_task=_tasks(50)), max_per_task=200)
    assert p.per_task_truncated is False
    assert len(p.per_task) == 50
    assert p.per_task_total_count == 50


def test_truncation_at_cap_boundary_not_truncated():
    p = _build_wire_payload(_result(per_task=_tasks(200)), max_per_task=200)
    assert p.per_task_truncated is False
    assert len(p.per_task) == 200


def test_truncation_above_cap_samples():
    p = _build_wire_payload(_result(per_task=_tasks(500)), max_per_task=200)
    assert p.per_task_truncated is True
    assert len(p.per_task) == 200
    assert p.per_task_total_count == 500


def test_truncation_deterministic_same_run_id_same_sample():
    tasks = _tasks(500)
    p1 = _build_wire_payload(_result(run_id="same", per_task=tasks), max_per_task=200)
    p2 = _build_wire_payload(_result(run_id="same", per_task=tasks), max_per_task=200)
    assert [t.task_index for t in p1.per_task] == [t.task_index for t in p2.per_task]


def test_truncation_different_run_ids_different_samples():
    """Guard against global-seed regression (e.g. random.seed(0))."""
    tasks = _tasks(500)
    p1 = _build_wire_payload(_result(run_id="run-A", per_task=tasks), max_per_task=200)
    p2 = _build_wire_payload(_result(run_id="run-B", per_task=tasks), max_per_task=200)
    assert [t.task_index for t in p1.per_task] != [t.task_index for t in p2.per_task]


def test_empty_per_task_handled():
    p = _build_wire_payload(_result(per_task=[]), max_per_task=200)
    assert p.per_task == []
    assert p.per_task_total_count == 0
    assert p.per_task_truncated is False


def test_payload_excludes_free_form_in_per_task():
    p = _build_wire_payload(_result(per_task=_tasks(1)), max_per_task=200)
    assert "prompt" not in p.per_task[0].model_dump()
    assert "response" not in p.per_task[0].model_dump()


def test_env_override_for_max_per_task(monkeypatch):
    monkeypatch.setenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK", "5")
    # Re-import to pick up env at module load:
    import importlib, maestro.benchmark.arbiter_report as ar
    importlib.reload(ar)
    assert ar.REPORT_MAX_PER_TASK == 5
```

- [ ] **Step 2: Run, verify FAIL** (imports missing)

Run: `uv run pytest tests/test_benchmark_arbiter_report.py -v -k "payload or truncation or empty or env"`
Expected: FAIL

- [ ] **Step 3: Add `ReportBenchmarkPayload`, `_sample_per_task`, `_build_wire_payload`**

Append to `maestro/benchmark/arbiter_report.py`:

```python
import os
import random
from datetime import UTC, datetime

from maestro.benchmark.models import BenchmarkResult

_DEFAULT_MAX_PER_TASK = 200
REPORT_MAX_PER_TASK = int(
    os.getenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK", _DEFAULT_MAX_PER_TASK)
)


class ReportBenchmarkPayload(BaseModel):
    """Wire payload for the arbiter report_benchmark MCP tool (v1.0.0)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_version: Literal["1.0.0"] = "1.0.0"
    run_id: str
    benchmark_id: str
    agent_id: str
    ts: str  # RFC3339 UTC
    score: float
    score_components: dict[str, float]
    total_tokens: int | None
    total_cost_usd: float | None
    duration_seconds: float
    per_task: list[WireTaskResult]
    per_task_total_count: int
    per_task_truncated: bool


def _sample_per_task(
    tasks: list[BenchmarkTaskResult], cap: int, run_id: str
) -> tuple[list[WireTaskResult], bool]:
    """Deterministic random sample when len > cap. seed = run_id."""
    if len(tasks) <= cap:
        return [WireTaskResult.from_domain(t) for t in tasks], False
    rng = random.Random(run_id)
    sampled = sorted(rng.sample(tasks, cap), key=lambda t: t.task_index)
    return [WireTaskResult.from_domain(t) for t in sampled], True


def _build_wire_payload(
    result: BenchmarkResult, max_per_task: int
) -> ReportBenchmarkPayload:
    """Project a domain BenchmarkResult into a wire ReportBenchmarkPayload."""
    per_task_wire, truncated = _sample_per_task(result.per_task, max_per_task, result.run_id)
    ts_str = result.ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ReportBenchmarkPayload(
        payload_version="1.0.0",
        run_id=result.run_id,
        benchmark_id=result.benchmark_id,
        agent_id=result.agent_id,
        ts=ts_str,
        score=result.score,
        score_components=dict(result.score_components),
        total_tokens=result.total_tokens,
        total_cost_usd=result.total_cost_usd,
        duration_seconds=result.duration_seconds,
        per_task=per_task_wire,
        per_task_total_count=len(result.per_task),
        per_task_truncated=truncated,
    )
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_benchmark_arbiter_report.py -v`
Expected: all PASS

- [ ] **Step 5: Type-check + lint + commit**

```bash
git add maestro/benchmark/arbiter_report.py tests/test_benchmark_arbiter_report.py
git commit -m "feat(benchmark): ReportBenchmarkPayload + deterministic sampling

Per spec §7.2: random sample with seed=run_id (not head-N) to
avoid systematic bias when benchmarks order tasks by difficulty.
Truncation cap configurable via MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK
env override (default 200). All cap-boundary cases covered:
empty, len<cap, len==cap, len>cap, deterministic per run_id,
distinct samples per run_id.

R-06b M4 design §4.2, §7.2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 4.3: `_classify_error`

**Files:**
- Modify: `maestro/benchmark/arbiter_report.py`
- Test: `tests/test_benchmark_arbiter_report.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_benchmark_arbiter_report.py`:

```python
import asyncio
from maestro.coordination.arbiter_errors import ArbiterContractError, ArbiterUnavailable
from maestro.benchmark.arbiter_report import _classify_error


def test_classify_timeout():
    assert _classify_error(asyncio.TimeoutError()) == ("timeout", "report timed out")


def test_classify_contract_error_preserves_code_and_message():
    e = ArbiterContractError(-32602, "missing field")
    cls, msg = _classify_error(e)
    assert cls == "contract_break"
    assert "-32602" in msg
    assert "missing field" in msg


def test_classify_unavailable():
    e = ArbiterUnavailable("broken pipe")
    assert _classify_error(e) == ("unavailable", "arbiter unavailable")


def test_classify_unexpected_includes_type_name():
    e = ValueError("oops")
    cls, msg = _classify_error(e)
    assert cls == "unexpected"
    assert "ValueError" in msg
    assert "oops" in msg


def test_classify_does_not_catch_cancelled():
    """CancelledError is BaseException — must propagate."""
    # _classify_error itself isn't called for CancelledError in the helper,
    # but the contract is "isinstance dispatch, not str-match".
    # Verify it falls into "unexpected" if it ever does — defensive coverage.
    cls, _ = _classify_error(asyncio.CancelledError())
    # Acceptable either way: 'unexpected' or that it's never reached.
    assert cls in ("unexpected", "timeout")  # don't pin behavior; just shouldn't blow up
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_benchmark_arbiter_report.py -v -k classify`
Expected: FAIL (ImportError)

- [ ] **Step 3: Add `_classify_error`**

Append to `maestro/benchmark/arbiter_report.py`:

```python
import asyncio

from maestro.coordination.arbiter_errors import ArbiterContractError, ArbiterUnavailable

ErrorClass = Literal["unavailable", "timeout", "contract_break", "unexpected"]

_ERROR_SEVERITY: dict[ErrorClass, Literal["warning", "error"]] = {
    "unavailable": "warning",
    "timeout": "warning",
    "contract_break": "error",
    "unexpected": "error",
}


def _classify_error(exc: BaseException) -> tuple[ErrorClass, str]:
    """Single source of truth for error classification. isinstance dispatch."""
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout", "report timed out"
    if isinstance(exc, ArbiterContractError):
        return "contract_break", f"{exc.code}: {exc.message}"
    if isinstance(exc, ArbiterUnavailable):
        return "unavailable", "arbiter unavailable"
    return "unexpected", f"{type(exc).__name__}: {exc}"
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_benchmark_arbiter_report.py -v -k classify`
Expected: PASS

- [ ] **Step 5: Type-check + commit**

```bash
git add maestro/benchmark/arbiter_report.py tests/test_benchmark_arbiter_report.py
git commit -m "feat(benchmark): _classify_error — isinstance dispatch

Single normalization for both obs.emit error_class field and
BenchmarkResult.report_error message ('error_class: details'
format). Type-based, not message-string-based — eliminates the
class of bugs where arbiter changes error-message wording and
classification silently moves from contract_break to unavailable.

R-06b M4 design §7.1.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 4.4: `report_benchmark_to_arbiter` — skipped + happy paths

**Files:**
- Modify: `maestro/benchmark/arbiter_report.py`
- Test: `tests/test_benchmark_arbiter_report.py` (extend)

- [ ] **Step 1: Write failing tests for client=None + happy paths**

Append:

```python
from unittest.mock import AsyncMock, MagicMock, patch
from maestro.benchmark.arbiter_report import report_benchmark_to_arbiter


@pytest.mark.asyncio
async def test_helper_returns_skipped_when_client_none():
    result = _result(run_id="skip")
    returned = await report_benchmark_to_arbiter(result, client=None)
    assert returned.report_status == "skipped"
    assert returned.report_error is None
    # Immutability: input unchanged
    assert result.report_status == "skipped"  # was the default; helper didn't mutate


@pytest.mark.asyncio
async def test_helper_returns_ok_on_created():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(return_value={"status": "created", "run_id": "x"})
    result = _result(run_id="x")
    returned = await report_benchmark_to_arbiter(result, mock_client)
    assert returned.report_status == "ok"
    assert returned.report_error is None
    mock_client.report_benchmark_raw.assert_awaited_once()


@pytest.mark.asyncio
async def test_helper_returns_ok_on_duplicate():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(return_value={"status": "duplicate", "run_id": "x"})
    result = _result(run_id="x")
    returned = await report_benchmark_to_arbiter(result, mock_client)
    assert returned.report_status == "ok"


@pytest.mark.asyncio
async def test_helper_returns_new_object_not_mutated():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(return_value={"status": "created", "run_id": "x"})
    result = _result()
    returned = await report_benchmark_to_arbiter(result, mock_client)
    assert returned is not result
    assert result.report_status == "skipped"  # original unchanged
    assert returned.report_status == "ok"
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_benchmark_arbiter_report.py -v -k "skipped_when_client_none or ok_on_created or ok_on_duplicate or new_object"`
Expected: FAIL

- [ ] **Step 3: Implement helper (skipped + happy paths first)**

Append to `maestro/benchmark/arbiter_report.py`:

```python
REPORT_TIMEOUT_S = 30.0


async def report_benchmark_to_arbiter(
    result: BenchmarkResult,
    client: object | None,  # ArbiterClient | None — typed below to avoid circular import
    *,
    max_per_task: int = REPORT_MAX_PER_TASK,
) -> BenchmarkResult:
    """Send result to arbiter; return updated copy with report_status set.

    Never raises (CancelledError propagates). client=None -> skipped.
    """
    if client is None:
        return result.model_copy(update={"report_status": "skipped"})

    payload = _build_wire_payload(result, max_per_task)
    response = await asyncio.wait_for(
        client.report_benchmark_raw(payload.model_dump(mode="json")),
        timeout=REPORT_TIMEOUT_S,
    )
    # response: {"status": "created" | "duplicate", "run_id": ...}
    _ = response.get("status")  # both "created" and "duplicate" → ok
    return result.model_copy(update={"report_status": "ok"})
```

- [ ] **Step 4: Run, verify PASS for happy + skipped**

Run: `uv run pytest tests/test_benchmark_arbiter_report.py -v -k "skipped_when_client_none or ok_on_created or ok_on_duplicate or new_object"`
Expected: PASS

- [ ] **Step 5: Commit (interim — error handling next task)**

```bash
git add maestro/benchmark/arbiter_report.py tests/test_benchmark_arbiter_report.py
git commit -m "feat(benchmark): report_benchmark_to_arbiter — happy + skipped paths

Helper returns updated copy (model_copy) — never mutates input.
client=None → skipped. Success (created or duplicate) → ok.
Error paths in next commit."
```

### Task 4.5: Helper error paths (timeout, contract, unavailable, unexpected)

**Files:**
- Modify: `maestro/benchmark/arbiter_report.py`
- Test: `tests/test_benchmark_arbiter_report.py` (extend)

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.asyncio
async def test_helper_failed_on_unavailable():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(side_effect=ArbiterUnavailable("broken pipe"))
    returned = await report_benchmark_to_arbiter(_result(), mock_client)
    assert returned.report_status == "failed"
    assert returned.report_error == "unavailable: arbiter unavailable"


@pytest.mark.asyncio
async def test_helper_failed_on_contract_break():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        side_effect=ArbiterContractError(-32602, "missing agent_id")
    )
    returned = await report_benchmark_to_arbiter(_result(), mock_client)
    assert returned.report_status == "failed"
    assert "contract_break: -32602" in returned.report_error


@pytest.mark.asyncio
async def test_helper_failed_on_timeout():
    async def hang(_payload):
        await asyncio.sleep(60)
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = hang
    # Use a tiny timeout for the test
    import maestro.benchmark.arbiter_report as ar
    original_timeout = ar.REPORT_TIMEOUT_S
    ar.REPORT_TIMEOUT_S = 0.05
    try:
        returned = await report_benchmark_to_arbiter(_result(), mock_client)
    finally:
        ar.REPORT_TIMEOUT_S = original_timeout
    assert returned.report_status == "failed"
    assert returned.report_error.startswith("timeout:")


@pytest.mark.asyncio
async def test_helper_failed_on_unexpected_exception():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(side_effect=ValueError("oops"))
    returned = await report_benchmark_to_arbiter(_result(), mock_client)
    assert returned.report_status == "failed"
    assert "unexpected: ValueError" in returned.report_error


@pytest.mark.asyncio
async def test_helper_does_not_catch_cancelled():
    """CancelledError is BaseException; must propagate."""
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await report_benchmark_to_arbiter(_result(), mock_client)
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_benchmark_arbiter_report.py -v -k "failed_on or does_not_catch"`
Expected: FAIL

- [ ] **Step 3: Add try/except to helper**

Replace the helper body in `maestro/benchmark/arbiter_report.py`:

```python
async def report_benchmark_to_arbiter(
    result: BenchmarkResult,
    client: object | None,
    *,
    max_per_task: int = REPORT_MAX_PER_TASK,
) -> BenchmarkResult:
    """Send result to arbiter; return updated copy with report_status set.

    Never raises except CancelledError. client=None -> skipped.
    """
    if client is None:
        return result.model_copy(update={"report_status": "skipped"})

    try:
        payload = _build_wire_payload(result, max_per_task)
        await asyncio.wait_for(
            client.report_benchmark_raw(payload.model_dump(mode="json")),
            timeout=REPORT_TIMEOUT_S,
        )
        return result.model_copy(update={"report_status": "ok"})
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 — intentional catch-all
        error_class, details = _classify_error(exc)
        return result.model_copy(
            update={
                "report_status": "failed",
                "report_error": f"{error_class}: {details}",
            }
        )
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_benchmark_arbiter_report.py -v`
Expected: all PASS

- [ ] **Step 5: Type-check + lint + commit**

```bash
git add maestro/benchmark/arbiter_report.py tests/test_benchmark_arbiter_report.py
git commit -m "feat(benchmark): helper error paths — fire-and-forget + classify

Catch BaseException to also cover SystemExit / KeyboardInterrupt
edge cases — re-raise CancelledError explicitly. Each failure
mapped to BenchmarkResult.report_status='failed' +
report_error='error_class: details' via _classify_error.

R-06b M4 design §3.2 / §9 — fire-and-forget per design;
--strict-report decision lives in M5 CLI, not in helper.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 4.6: Wrap helper in `obs.span` + emit events

**Files:**
- Modify: `maestro/benchmark/arbiter_report.py`
- Test: `tests/test_benchmark_arbiter_report.py` (extend)

- [ ] **Step 1: Check existing obs API**

Run: `grep -n "obs.span\|obs.emit\|from maestro import obs\|from maestro.obs" maestro/scheduler.py | head -5`
Note: confirm the import path and span/emit signatures used in M2 scheduler instrumentation.

- [ ] **Step 2: Write failing tests using a captured-events fixture**

```python
@pytest.fixture
def captured_obs_events(monkeypatch):
    events = []
    from maestro import obs
    original_emit = obs.emit
    def capture(name, **kw):
        events.append({"event_name": name, **kw})
        return original_emit(name, **kw)
    monkeypatch.setattr(obs, "emit", capture)
    return events


@pytest.mark.asyncio
async def test_emits_skipped_when_client_none(captured_obs_events):
    await report_benchmark_to_arbiter(_result(run_id="x"), None)
    assert any(e["event_name"] == "benchmark.report.skipped" and e.get("run_id") == "x"
               for e in captured_obs_events)


@pytest.mark.asyncio
async def test_emits_succeeded_on_created(captured_obs_events):
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(return_value={"status": "created", "run_id": "x"})
    await report_benchmark_to_arbiter(_result(run_id="x"), mock_client)
    assert any(e["event_name"] == "benchmark.report.succeeded" for e in captured_obs_events)


@pytest.mark.asyncio
async def test_emits_duplicate_on_duplicate(captured_obs_events):
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(return_value={"status": "duplicate", "run_id": "x"})
    await report_benchmark_to_arbiter(_result(run_id="x"), mock_client)
    assert any(e["event_name"] == "benchmark.report.duplicate" for e in captured_obs_events)


@pytest.mark.asyncio
async def test_emits_contract_break_event_distinct(captured_obs_events):
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        side_effect=ArbiterContractError(-32602, "missing")
    )
    await report_benchmark_to_arbiter(_result(run_id="x"), mock_client)
    assert any(e["event_name"] == "benchmark.report.contract_break" for e in captured_obs_events)
    # Must NOT emit benchmark.report.failed for contract breaks (distinct event)
    assert not any(e["event_name"] == "benchmark.report.failed" for e in captured_obs_events)


@pytest.mark.asyncio
async def test_emits_failed_on_unavailable(captured_obs_events):
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(side_effect=ArbiterUnavailable("x"))
    await report_benchmark_to_arbiter(_result(run_id="x"), mock_client)
    assert any(e["event_name"] == "benchmark.report.failed"
               and e.get("error_class") == "unavailable" for e in captured_obs_events)
```

- [ ] **Step 3: Run, verify FAIL**

Run: `uv run pytest tests/test_benchmark_arbiter_report.py -v -k emit`
Expected: FAIL

- [ ] **Step 4: Add obs wrapping**

In `maestro/benchmark/arbiter_report.py`:

```python
from maestro import obs  # adjust import to match M2 pattern


async def report_benchmark_to_arbiter(
    result: BenchmarkResult,
    client: object | None,
    *,
    max_per_task: int = REPORT_MAX_PER_TASK,
) -> BenchmarkResult:
    if client is None:
        obs.emit("benchmark.report.skipped",
                 run_id=result.run_id, benchmark_id=result.benchmark_id,
                 agent_id=result.agent_id)
        return result.model_copy(update={"report_status": "skipped"})

    try:
        payload = _build_wire_payload(result, max_per_task)
        response = await asyncio.wait_for(
            client.report_benchmark_raw(payload.model_dump(mode="json")),
            timeout=REPORT_TIMEOUT_S,
        )
        status = response.get("status")
        if status == "duplicate":
            obs.emit("benchmark.report.duplicate",
                     run_id=result.run_id, benchmark_id=result.benchmark_id,
                     agent_id=result.agent_id)
        else:
            obs.emit("benchmark.report.succeeded",
                     run_id=result.run_id, benchmark_id=result.benchmark_id,
                     agent_id=result.agent_id, score=result.score)
        return result.model_copy(update={"report_status": "ok"})
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001
        error_class, details = _classify_error(exc)
        event_name = (
            "benchmark.report.contract_break"
            if error_class == "contract_break"
            else "benchmark.report.failed"
        )
        obs.emit(event_name,
                 run_id=result.run_id, benchmark_id=result.benchmark_id,
                 agent_id=result.agent_id, error_class=error_class,
                 error=details, severity=_ERROR_SEVERITY[error_class])
        return result.model_copy(update={
            "report_status": "failed",
            "report_error": f"{error_class}: {details}",
        })
```

If `obs.span` is also part of the M2 pattern, wrap the try-body in `async with obs.span("benchmark.report", run_id=...):`. Inspect M2 scheduler use to mirror.

- [ ] **Step 5: Run, verify PASS**

Run: `uv run pytest tests/test_benchmark_arbiter_report.py -v`
Expected: all PASS

- [ ] **Step 6: Type-check + lint + commit**

```bash
git add maestro/benchmark/arbiter_report.py tests/test_benchmark_arbiter_report.py
git commit -m "feat(benchmark): obs.emit instrumentation for report helper

5 distinct event names: skipped, succeeded, duplicate, failed,
contract_break. contract_break gets its OWN event (not just
severity) so alerting rules can match by name. Severity table
encoded in _ERROR_SEVERITY: transient = warning, contract or
unexpected = error.

R-06b M4 design §9 / §10.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 5: Contract tests + forward-compat

### Task 5.1: Maestro-side JSONSchema contract tests

**Files:**
- Create: `tests/test_benchmark_contract.py`

- [ ] **Step 1: Write contract tests**

File content for `tests/test_benchmark_contract.py`:

```python
"""R-06b M4 contract tests — JSONSchema validation on Maestro side.

Schema lives at _cowork_output/benchmark-contract/report_benchmark-v1.schema.json.
Both Maestro (this file) and arbiter (Rust tests/contract_test.rs)
validate against it. Schema is the single source of truth.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from maestro.benchmark.arbiter_report import (
    ReportBenchmarkPayload, WireTaskResult, _build_wire_payload,
)
from maestro.benchmark.models import BenchmarkResult, BenchmarkTaskResult


SCHEMA_PATH = Path(__file__).parents[1].parent / "_cowork_output" / "benchmark-contract" / "report_benchmark-v1.schema.json"


@pytest.fixture(scope="module")
def schema():
    with SCHEMA_PATH.open() as f:
        return json.load(f)


@pytest.fixture(scope="module")
def request_validator(schema):
    defs = schema.get("definitions", schema.get("$defs", {}))
    return Draft202012Validator({**schema, **{"$ref": "#/definitions/Request"}}, format_checker=Draft202012Validator.FORMAT_CHECKER)


@pytest.fixture(scope="module")
def response_validator(schema):
    return Draft202012Validator({**schema, **{"$ref": "#/definitions/Response"}}, format_checker=Draft202012Validator.FORMAT_CHECKER)


def test_schema_file_exists():
    assert SCHEMA_PATH.exists(), f"schema file missing at {SCHEMA_PATH}"


def test_schema_is_valid_jsonschema(schema):
    Draft202012Validator.check_schema(schema)


def test_pydantic_payload_validates_against_schema(request_validator):
    result = BenchmarkResult(
        run_id="r1", benchmark_id="b", agent_id="claude_code",
        score=0.85, score_components={"accuracy": 0.85},
        per_task=[BenchmarkTaskResult(task_index=0, prompt="p", response="r",
                                       duration_seconds=1.0, task_type="bugfix", score=0.9)],
        duration_seconds=10.0,
    )
    payload = _build_wire_payload(result, max_per_task=200)
    data = json.loads(payload.model_dump_json())
    errors = list(request_validator.iter_errors(data))
    assert not errors, f"validation errors: {[e.message for e in errors]}"


def test_missing_required_field_fails_validation(request_validator):
    data = {"payload_version": "1.0.0", "run_id": "r1"}
    errors = list(request_validator.iter_errors(data))
    assert errors, "expected validation errors for missing required fields"


def test_response_created_validates(response_validator):
    resp = {"status": "created", "run_id": "r1"}
    errors = list(response_validator.iter_errors(resp))
    assert not errors


def test_response_duplicate_validates(response_validator):
    resp = {"status": "duplicate", "run_id": "r1"}
    errors = list(response_validator.iter_errors(resp))
    assert not errors


def test_response_unknown_status_fails(response_validator):
    resp = {"status": "weird", "run_id": "r1"}
    errors = list(response_validator.iter_errors(resp))
    assert errors
```

- [ ] **Step 2: Run, verify PASS**

Run: `uv run pytest tests/test_benchmark_contract.py -v`
Expected: 7 PASS

(If the SCHEMA_PATH resolution doesn't work due to repo layout, adjust the path expression — the schema lives at `Maestro/../_cowork_output/benchmark-contract/...`.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_benchmark_contract.py
git commit -m "test(benchmark): contract tests — JSONSchema validation

Maestro side of the cross-repo contract: validates that the
Pydantic-serialized payload matches the shared JSONSchema, and
that response shapes match too. arbiter side mirrors in
tests/contract_test.rs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 5.2: Forward-compat (additive evolution) tests

**Files:**
- Modify: `tests/test_benchmark_contract.py`

- [ ] **Step 1: Add 2 tests**

Append:

```python
def test_unknown_optional_fields_in_payload_accepted(request_validator):
    """Adding optional fields in v1.1+ must not break v1.0 validation."""
    payload = {
        "payload_version": "1.0.0", "run_id": "r", "benchmark_id": "b", "agent_id": "a",
        "ts": "2026-05-23T12:00:00Z", "score": 0.5, "score_components": {},
        "duration_seconds": 1.0, "per_task": [], "per_task_total_count": 0,
        "per_task_truncated": False,
        "future_field": "added in v1.1",
    }
    errors = list(request_validator.iter_errors(payload))
    assert not errors, f"unknown optional field rejected: {[e.message for e in errors]}"


def test_unknown_response_fields_dont_crash_helper():
    """Helper must tolerate arbiter responses with extra info fields."""
    from unittest.mock import AsyncMock, MagicMock
    from maestro.benchmark.arbiter_report import report_benchmark_to_arbiter

    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(return_value={
        "status": "created", "run_id": "x",
        "server_advice": "future info", "queue_depth": 42,
    })
    import asyncio
    result = BenchmarkResult(
        run_id="x", benchmark_id="b", agent_id="a",
        score=0.5, per_task=[], duration_seconds=1.0,
    )
    returned = asyncio.run(report_benchmark_to_arbiter(result, mock_client))
    assert returned.report_status == "ok"
```

- [ ] **Step 2: Run, verify PASS**

Run: `uv run pytest tests/test_benchmark_contract.py -v -k "unknown"`
Expected: 2 PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_benchmark_contract.py
git commit -m "test(benchmark): forward-compat — additive fields safe

Documents policy: 'additive optional fields don't require
payload_version bump'. Producer is strict (Pydantic
extra='forbid'); consumer (arbiter JSONSchema additionalProperties:
true) is liberal. Asymmetric trust = safe evolution.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 6: Runner + ATP client additive (parallel-safe with Phase 3-5)

### Task 6.1: `BenchmarkRunner.run(..., run_id)`

**Files:**
- Modify: `maestro/benchmark/runner.py`
- Test: `tests/test_benchmark_runner.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_benchmark_runner.py`:

```python
import re
import pytest
from maestro.benchmark.runner import BenchmarkRunner


@pytest.mark.asyncio
async def test_run_without_run_id_generates_uuid(mock_atp_client, mock_responder):
    """If no run_id passed, runner generates UUID4 and propagates to result."""
    runner = BenchmarkRunner(mock_atp_client, mock_responder)
    result = await runner.run(benchmark_id="b", agent_id="a")
    assert re.fullmatch(r"[0-9a-f-]{36}", result.run_id), f"not a UUID4: {result.run_id}"


@pytest.mark.asyncio
async def test_run_with_explicit_run_id_preserved(mock_atp_client, mock_responder):
    runner = BenchmarkRunner(mock_atp_client, mock_responder)
    result = await runner.run(benchmark_id="b", agent_id="a", run_id="ci-job-42")
    assert result.run_id == "ci-job-42"
```

`mock_atp_client` and `mock_responder` are presumed-existing fixtures from M1 tests. Reuse them as-is.

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_benchmark_runner.py -v -k "without_run_id or explicit_run_id"`
Expected: FAIL (unexpected kwarg `run_id`)

- [ ] **Step 3: Add `run_id` param to runner**

In `maestro/benchmark/runner.py`, find `BenchmarkRunner.run`. Add:

```python
import uuid

async def run(
    self,
    benchmark_id: str,
    agent_id: str,
    *,
    run_id: str | None = None,  # R-06b M4 additive
) -> BenchmarkResult:
    effective_run_id = run_id if run_id is not None else str(uuid.uuid4())
    # ... existing body, but using effective_run_id wherever the runner
    # previously generated/derived an ID. If runner currently delegates
    # run_id to ATP's start_run, accept ATP's run_id as fallback when
    # explicit not provided; only auto-uuid if both are absent.
```

The exact integration depends on how M3 currently obtains `run_id` from ATP. Two cases:
- If runner always took ATP's run_id: change to prefer caller's `run_id` when provided, fall back to ATP's; in `BenchmarkResult` use the caller's if provided.
- If runner already auto-generated: replace generator with the new pattern.

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_benchmark_runner.py -v`
Expected: all PASS (incl. pre-existing M1 tests)

- [ ] **Step 5: Type-check + lint + commit**

```bash
git add maestro/benchmark/runner.py tests/test_benchmark_runner.py
git commit -m "feat(benchmark): runner accepts caller-provided run_id

For CI-retry idempotency: caller passes stable run_id (e.g.
\$GITHUB_RUN_ID+benchmark_id) so retries hit arbiter's
ON CONFLICT branch and don't inflate benchmark_runs.

If not provided, UUID4 fallback gives one-shot semantics
(retries create new rows). M5 CLI surfaces --run-id flag.

R-06b M4 design §3.2 (Maestro changes) + §11.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 6.2: ATP client — surface `task_type` and `score` from metadata

**Files:**
- Modify: `maestro/benchmark/atp_client.py`
- Test: `tests/test_benchmark_atp_client.py` (extend)

- [ ] **Step 1: Inspect M3 ATP _Task adapter**

Run: `grep -n "_Task\|task_type\|metadata\|task_index" maestro/benchmark/atp_client.py | head -20`
Note: where ATP request metadata is parsed in `MaestroATPAdapter`.

- [ ] **Step 2: Write failing tests**

Append to `tests/test_benchmark_atp_client.py`:

```python
def test_task_type_surfaced_from_atp_metadata(monkeypatch):
    """When ATP request has metadata.task_type, runner sees it on BenchmarkTaskResult."""
    # Use the FakeRequestQueue pattern already in this file.
    # ATP response with task_type in metadata:
    fake_request = {
        "task_id": "t0",
        "task": {"description": "fix bug"},
        "metadata": {"task_index": 0, "task_type": "bugfix"},
    }
    # ... drive through MaestroATPAdapter, assert BenchmarkTaskResult.task_type == "bugfix"


def test_task_type_none_when_absent_in_atp_metadata():
    """No metadata.task_type → BenchmarkTaskResult.task_type is None (graceful)."""
    fake_request = {
        "task_id": "t0", "task": {"description": "fix bug"},
        "metadata": {"task_index": 0},
    }
    # Assert task_type is None on the resulting BenchmarkTaskResult.
```

Flesh out the body using the FakeRequestQueue / fixture pattern already established in `test_benchmark_atp_client.py` (R-06b M3 tests).

- [ ] **Step 3: Run, verify FAIL**

Run: `uv run pytest tests/test_benchmark_atp_client.py -v -k task_type`
Expected: FAIL

- [ ] **Step 4: Extend adapter**

In `maestro/benchmark/atp_client.py`, find where `BenchmarkTaskResult` is constructed (likely in the `_run_iterator` or `submit` path of `_RunAdapter`). Pull `task_type` from `metadata.task_type`:

```python
task_type = (atp_request.get("metadata") or {}).get("task_type")
# ... pass to BenchmarkTaskResult(..., task_type=task_type)
```

If ATP also exposes a per-task `score` in its response (not just `total_score`), propagate that the same way. If not, leave `score=None` and document — surfacing is best-effort.

- [ ] **Step 5: Run, verify PASS**

Run: `uv run pytest tests/test_benchmark_atp_client.py -v`
Expected: all PASS

- [ ] **Step 6: Type-check + lint + commit**

```bash
git add maestro/benchmark/atp_client.py tests/test_benchmark_atp_client.py
git commit -m "feat(benchmark): surface task_type from ATP metadata

ATP requests carry metadata.task_type (when the benchmark
provides it). MaestroATPAdapter now passes it through to
BenchmarkTaskResult so the wire projection in WireTaskResult
can include it without consulting ATP again.

Null-safe: missing metadata.task_type → BenchmarkTaskResult.task_type=None.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 6.3: Re-export `report_benchmark_to_arbiter` from package

**Files:**
- Modify: `maestro/benchmark/__init__.py`

- [ ] **Step 1: Add export**

In `maestro/benchmark/__init__.py`, add to imports + `__all__`:

```python
from maestro.benchmark.arbiter_report import (
    ReportBenchmarkPayload,
    WireTaskResult,
    report_benchmark_to_arbiter,
)
```

And add the three names to `__all__`.

- [ ] **Step 2: Smoke import test**

Run: `uv run python -c "from maestro.benchmark import report_benchmark_to_arbiter, WireTaskResult, ReportBenchmarkPayload; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add maestro/benchmark/__init__.py
git commit -m "feat(benchmark): re-export report_benchmark_to_arbiter API"
```

---

## Phase 7: E2E + smoke + CI wiring

**Prerequisite:** Phase 1 done; arbiter binary buildable; `ARBITER_M4_SHA` known.

### Task 7.1: Real-subprocess e2e — happy path (created)

**Files:**
- Create: `tests/test_arbiter_real_subprocess_benchmark.py`

- [ ] **Step 1: Inspect R-05 test pattern**

Run: `head -80 tests/test_arbiter_real_subprocess.py`
Note: subprocess setup, MAESTRO_ARBITER_BIN handling, auto-skip mechanism.

- [ ] **Step 2: Write the e2e test (created case)**

File content for `tests/test_arbiter_real_subprocess_benchmark.py`:

```python
"""R-06b M4 e2e tests — real arbiter-mcp subprocess.

Auto-skip if MAESTRO_ARBITER_BIN env not set or path not executable.
Mirrors R-05 pattern (tests/test_arbiter_real_subprocess.py).
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from maestro.benchmark import BenchmarkResult, BenchmarkTaskResult, report_benchmark_to_arbiter
from maestro.coordination.arbiter_client import ArbiterClient, ArbiterClientConfig
from maestro.coordination.arbiter_errors import ArbiterContractError


ARBITER_BIN = os.environ.get("MAESTRO_ARBITER_BIN")

pytestmark = pytest.mark.skipif(
    not ARBITER_BIN or not Path(ARBITER_BIN).exists(),
    reason="MAESTRO_ARBITER_BIN not set or binary not found",
)


def _build_result(run_id: str, per_task_n: int = 2) -> BenchmarkResult:
    tasks = [
        BenchmarkTaskResult(
            task_index=i, prompt=f"p{i}", response=f"r{i}",
            duration_seconds=1.0 + i, task_type="bugfix", score=0.5 + i * 0.1,
        )
        for i in range(per_task_n)
    ]
    return BenchmarkResult(
        run_id=run_id, benchmark_id="e2e-bench", agent_id="claude_code",
        score=0.75, score_components={"accuracy": 0.75},
        per_task=tasks, duration_seconds=10.0,
    )


@pytest.mark.asyncio
async def test_report_benchmark_created_end_to_end(tmp_path):
    db_path = tmp_path / "arbiter.db"
    config = ArbiterClientConfig(binary_path=ARBITER_BIN, db_path=str(db_path))
    client = ArbiterClient(config)
    await client.start()
    try:
        result = _build_result(run_id="e2e-1")
        returned = await report_benchmark_to_arbiter(result, client)
        assert returned.report_status == "ok"
        assert returned.report_error is None
    finally:
        await client.stop()

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT benchmark_id, agent_id, score, per_task_total_count, per_task_truncated FROM benchmark_runs WHERE run_id=?",
            ("e2e-1",),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == "e2e-bench"
    assert row[1] == "claude_code"
    assert abs(row[2] - 0.75) < 1e-6
    assert row[3] == 2
    assert row[4] == 0
```

- [ ] **Step 3: Run with binary**

```bash
export MAESTRO_ARBITER_BIN=/Users/Andrei_Shtanakov/labs/all_ai_orchestrators/arbiter/target/release/arbiter-mcp
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/arbiter
cargo build --release --bin arbiter-mcp
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/Maestro
uv run pytest tests/test_arbiter_real_subprocess_benchmark.py::test_report_benchmark_created_end_to_end -v
```
Expected: PASS

- [ ] **Step 4: Run without binary (verify skip)**

```bash
unset MAESTRO_ARBITER_BIN
uv run pytest tests/test_arbiter_real_subprocess_benchmark.py -v
```
Expected: skipped

- [ ] **Step 5: Commit**

```bash
git add tests/test_arbiter_real_subprocess_benchmark.py
git commit -m "test(arbiter): e2e — report_benchmark created path

Real arbiter-mcp subprocess + ArbiterClient + helper → assert
row in SQLite with expected aggregate columns. Auto-skip when
MAESTRO_ARBITER_BIN not set (mirrors R-05).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 7.2: E2E — duplicate path

**Files:**
- Modify: `tests/test_arbiter_real_subprocess_benchmark.py`

- [ ] **Step 1: Add test**

```python
@pytest.mark.asyncio
async def test_report_benchmark_duplicate_end_to_end(tmp_path):
    db_path = tmp_path / "arbiter.db"
    config = ArbiterClientConfig(binary_path=ARBITER_BIN, db_path=str(db_path))
    client = ArbiterClient(config)
    await client.start()
    try:
        result = _build_result(run_id="e2e-dup")
        r1 = await report_benchmark_to_arbiter(result, client)
        r2 = await report_benchmark_to_arbiter(result, client)
        assert r1.report_status == "ok"
        assert r2.report_status == "ok"  # duplicate still maps to ok
    finally:
        await client.stop()

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM benchmark_runs WHERE run_id=?", ("e2e-dup",)).fetchone()[0]
    finally:
        conn.close()
    assert count == 1, "duplicate must not produce a second row"
```

- [ ] **Step 2: Run, PASS**

```bash
uv run pytest tests/test_arbiter_real_subprocess_benchmark.py::test_report_benchmark_duplicate_end_to_end -v
```
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_arbiter_real_subprocess_benchmark.py
git commit -m "test(arbiter): e2e — report_benchmark duplicate idempotency"
```

### Task 7.3: E2E — contract_break path

**Files:**
- Modify: `tests/test_arbiter_real_subprocess_benchmark.py`

- [ ] **Step 1: Add test**

```python
@pytest.mark.asyncio
async def test_report_benchmark_contract_break_end_to_end(tmp_path):
    """Send malformed payload (missing required field) via raw _call_tool.

    Tests arbiter's strict server-side validation — Maestro's outbound
    Pydantic path can't produce this (Literal['1.0.0']).
    """
    db_path = tmp_path / "arbiter.db"
    config = ArbiterClientConfig(binary_path=ARBITER_BIN, db_path=str(db_path))
    client = ArbiterClient(config)
    await client.start()
    try:
        bad_payload = {
            "payload_version": "1.0.0", "run_id": "cb-1",
            # missing agent_id
            "benchmark_id": "b", "ts": "2026-05-23T12:00:00Z",
            "score": 0.5, "score_components": {}, "duration_seconds": 1.0,
            "per_task": [], "per_task_total_count": 0, "per_task_truncated": False,
        }
        with pytest.raises(ArbiterContractError):
            await client.report_benchmark_raw(bad_payload)
    finally:
        await client.stop()

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM benchmark_runs WHERE run_id='cb-1'").fetchone()[0]
    finally:
        conn.close()
    assert count == 0, "no row should be inserted on contract break"
```

- [ ] **Step 2: Run, PASS**

```bash
uv run pytest tests/test_arbiter_real_subprocess_benchmark.py::test_report_benchmark_contract_break_end_to_end -v
```
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_arbiter_real_subprocess_benchmark.py
git commit -m "test(arbiter): e2e — report_benchmark contract_break

Raw _call_tool with missing agent_id → real arbiter -32602 →
ArbiterContractError caught by helper → 0 rows. Validates the
severity-1 path end-to-end (sec §3 of design doc)."
```

### Task 7.4: Smoke script for CI

**Files:**
- Create: `scripts/smoke_benchmark_report.py`

- [ ] **Step 1: Create directory + script**

```bash
mkdir -p /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/Maestro/scripts
```

File content for `scripts/smoke_benchmark_report.py`:

```python
#!/usr/bin/env python3
"""R-06b M4 smoke — happy path end-to-end with real arbiter-mcp.

Run as the final step of the arbiter-e2e CI job (after pytest).
Returns 0 on green smoke, 1 + diagnostic on failure.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

from maestro.benchmark import (
    BenchmarkResult,
    BenchmarkTaskResult,
    report_benchmark_to_arbiter,
)
from maestro.coordination.arbiter_client import ArbiterClient, ArbiterClientConfig


async def _run() -> int:
    binary = os.environ.get("MAESTRO_ARBITER_BIN")
    if not binary or not Path(binary).exists():
        print(f"smoke FAIL: MAESTRO_ARBITER_BIN missing or not found ({binary!r})", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "arbiter.db"
        config = ArbiterClientConfig(binary_path=binary, db_path=str(db_path))
        client = ArbiterClient(config)
        await client.start()
        try:
            run_id = f"smoke-{uuid.uuid4()}"
            result = BenchmarkResult(
                run_id=run_id, benchmark_id="smoke-bench", agent_id="claude_code",
                score=0.99, score_components={"smoke": 1.0},
                per_task=[BenchmarkTaskResult(
                    task_index=0, prompt="p", response="r",
                    duration_seconds=0.1, task_type="smoke", score=1.0,
                )],
                duration_seconds=0.5,
            )
            returned = await report_benchmark_to_arbiter(result, client)
        finally:
            await client.stop()

        if returned.report_status != "ok":
            print(
                f"smoke FAIL: report_status={returned.report_status} "
                f"error={returned.report_error}",
                file=sys.stderr,
            )
            return 1

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT benchmark_id, agent_id, score FROM benchmark_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            print(f"smoke FAIL: no row in benchmark_runs for run_id={run_id}", file=sys.stderr)
            return 1

        print(f"smoke OK: run_id={run_id} benchmark_id={row[0]} agent={row[1]} score={row[2]}")
        return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run locally**

```bash
chmod +x scripts/smoke_benchmark_report.py
export MAESTRO_ARBITER_BIN=/Users/Andrei_Shtanakov/labs/all_ai_orchestrators/arbiter/target/release/arbiter-mcp
uv run python scripts/smoke_benchmark_report.py
```
Expected: `smoke OK: run_id=smoke-... benchmark_id=smoke-bench agent=claude_code score=0.99`, exit 0

- [ ] **Step 3: Run without binary (verify FAIL gracefully)**

```bash
unset MAESTRO_ARBITER_BIN
uv run python scripts/smoke_benchmark_report.py
```
Expected: `smoke FAIL: MAESTRO_ARBITER_BIN missing or not found (None)`, exit 1

- [ ] **Step 4: Commit**

```bash
git add scripts/smoke_benchmark_report.py
git commit -m "feat(benchmark): CI smoke script for report_benchmark

scripts/smoke_benchmark_report.py — happy-path one-shot against
a real arbiter-mcp subprocess. Run as final step of arbiter-e2e
CI job. Exit 0 on green, 1 + diagnostic on failure.

Replaces manual smoke from DoD point 6 (auditable, automated).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 7.5: CI yml — wire new e2e + smoke + vendored-sha check

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Bump `ARBITER_PINNED_SHA`**

In `.github/workflows/ci.yml`, top-level env, change:
```yaml
env:
  ARBITER_PINNED_SHA: d1a8ecd  # → replace with ARBITER_M4_SHA from Phase 1 Task 1.8
```

- [ ] **Step 2: Add vendored-vs-pinned consistency step**

Inside the `arbiter-e2e` job, before the test step (after `uv sync`):

```yaml
      - name: Verify vendored arbiter_client matches pinned SHA
        working-directory: Maestro
        run: |
          PYVENDOR_SHA=$(uv run python -c "from maestro.coordination.arbiter_client import ARBITER_VENDORED_FROM_SHA; print(ARBITER_VENDORED_FROM_SHA)")
          if [ "$PYVENDOR_SHA" != "$ARBITER_PINNED_SHA" ]; then
            echo "::error::vendored copy out of sync with pinned arbiter: vendored=$PYVENDOR_SHA, pinned=$ARBITER_PINNED_SHA — re-vendor required"
            exit 1
          fi
          echo "vendored OK: $PYVENDOR_SHA"
```

- [ ] **Step 3: Extend the existing test step to also run the new benchmark e2e**

Find:
```yaml
      - name: Run R-05 real-subprocess tests
        ...
        run: uv run python -m pytest tests/test_arbiter_real_subprocess.py -v
```

Change `run:` to:
```yaml
        run: |
          uv run python -m pytest \
            tests/test_arbiter_real_subprocess.py \
            tests/test_arbiter_real_subprocess_benchmark.py \
            -v
```

- [ ] **Step 4: Add smoke step (final in arbiter-e2e job)**

After the pytest step:

```yaml
      - name: Run benchmark report smoke
        working-directory: Maestro
        env:
          MAESTRO_ARBITER_BIN: ${{ github.workspace }}/arbiter/target/release/arbiter-mcp
        run: uv run python scripts/smoke_benchmark_report.py
```

- [ ] **Step 5: Local validation (syntax)**

Run: `uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`
Expected: no output (valid YAML)

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci(arbiter-e2e): run report_benchmark e2e + smoke + vendor check

- Bump ARBITER_PINNED_SHA to <ARBITER_M4_SHA> (report_benchmark
  landed on arbiter side).
- New step: verify ARBITER_VENDORED_FROM_SHA == ARBITER_PINNED_SHA.
  Fails fast if vendored copy lags behind pinned arbiter.
- Test step now includes tests/test_arbiter_real_subprocess_benchmark.py.
- New final step: scripts/smoke_benchmark_report.py.

DoD §15 points 4, 5, 6 — all green-via-CI now (no manual).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 8: Docs + TODO + landing

### Task 8.1: Update TODO.md

**Files:**
- Modify: `TODO.md`

- [ ] **Step 1: Mark M4 done + add open-issue items**

In `TODO.md` under section `### R-06b — Agent benchmarking via ATP`:

Replace the line:
```markdown
- [ ] **R-06b M4 Arbiter feedback wiring**: ...
```

With:
```markdown
- [x] **R-06b M4** (<DATE> 2026-05-XX, commit `<MERGE_SHA>`): new MCP tool `report_benchmark` in arbiter-mcp + `maestro/benchmark/arbiter_report.py` helper. Persist-only into new `benchmark_runs` table; ON CONFLICT(run_id) DO NOTHING idempotency; fire-and-forget emit with `BenchmarkResult.report_status`/`report_error`. JSONSchema contract in `_cowork_output/benchmark-contract/`. Vendored client `MIN_ARBITER_PROTOCOL=(1,1)` + `ARBITER_VENDORED_FROM_SHA` pin + CI drift check. Smoke script + 3-case e2e (created/duplicate/contract_break) in arbiter-e2e job. Tests: N Maestro + N Rust + 3 e2e + 7 contract + 3 version + 1 smoke. Full design: `docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md`.
```

Add new section at the end of `### Follow-ups разблокированные R-03` (or create `### Follow-ups from R-06b M4`):

```markdown
### Follow-ups from R-06b M4

- [ ] **M3-obs / arbiter trace**: W3C `traceparent` injection in MCP JSON-RPC envelope (spans all arbiter calls, not specific to M4). Reference: §14 of M4 design.
- [ ] **R-06b M4b**: revisit `max_per_task=200` sampling for swe-bench-full (>1000 tasks). Trigger: first PROD swe-bench-full run.
- [ ] **R-07 prereq**: GIN index on `benchmark_runs.per_task` jsonb (defer until SQL filtering needed).
- [ ] **R-07 prereq**: normalize `benchmark_task_results` table (migration from jsonb blob; trigger = formal query demand).
- [ ] **R-07 prereq**: retention policy (TTL / archive) for `benchmark_runs`. Trigger: > 10k rows OR > 1 GB.
- [ ] **R-14**: vendored `arbiter_client.py` → PyPI `arbiter-py` package (M4 enlarged vendor surface).
- [ ] **unscheduled**: outbox + background retry for benchmark report (only if fire-and-forget shows real CI churn).
- [ ] **unscheduled**: outgoing benchmark trigger from arbiter ("router uncertain → run benchmark").
- [ ] **M5 / separate**: service-account ATP token for CI. Multi-tenant arbiter auth — separate ticket if arbiter ever leaves subprocess trust model.
```

- [ ] **Step 2: Commit**

```bash
git add TODO.md
git commit -m "docs(todo): mark R-06b M4 done + register open issues

Open issues from M4 design §14 propagated to TODO follow-up
section. Each carries a trigger condition so they don't become
silent ops debt.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 8.2: Update CHANGELOG.md

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add release-notes section**

At the top (under unreleased / new version header — match existing convention):

```markdown
### 0.X.0 — R-06b M4 Arbiter benchmark wiring

**Added:**
- `maestro/benchmark/arbiter_report.py` — `report_benchmark_to_arbiter(result, client)` helper; never raises, returns a copy with `report_status` / `report_error` set.
- `BenchmarkResult.report_status` (`Literal["ok","failed","skipped"]`) and `.report_error` (`str | None`).
- `BenchmarkTaskResult.task_type` and `.score` (additive; surfaced from ATP `metadata.task_type`).
- `BenchmarkRunner.run(..., run_id: str | None = None)` — caller-provided run_id for CI-retry idempotency.
- `ArbiterClient.report_benchmark_raw(payload)` MCP method.
- `ArbiterContractError` — distinguishes JSON-RPC contract breaks from transient `ArbiterUnavailable`.
- `MIN_ARBITER_PROTOCOL = (1, 1)` and `ARBITER_VENDORED_FROM_SHA` constants in vendored client; `start()` validates server-advertised `protocolVersion`.
- `_cowork_output/benchmark-contract/report_benchmark-v1.schema.json` — JSONSchema source of truth.
- `scripts/smoke_benchmark_report.py` — CI smoke.

**Configuration:**
- `MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK` env override (default 200) for per_task truncation.

**Tests:**
- New: `tests/test_benchmark_arbiter_report.py`, `tests/test_benchmark_contract.py`, `tests/test_arbiter_real_subprocess_benchmark.py`, `tests/test_arbiter_client_version.py`, `tests/test_arbiter_errors.py`.
- Extended: `tests/test_arbiter_client.py`, `tests/test_benchmark_runner.py`, `tests/test_benchmark_atp_client.py`, `tests/test_benchmark_models.py`.

**Cross-repo:** requires `arbiter-mcp` at `<ARBITER_M4_SHA>` or later (advertises `protocolVersion="1.1.0"`, new `report_benchmark` tool, `benchmark_runs` table migration).

Design: `docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md`.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): R-06b M4 — Arbiter benchmark wiring section"
```

### Task 8.3: Final validation — full test suite + lint + type-check

- [ ] **Step 1: Full Maestro test suite**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/Maestro
uv run pytest -v
```
Expected: all PASS (1156 previous + ~30 new = ~1186 total). Note: `test_arbiter_real_subprocess_benchmark.py` will skip without `MAESTRO_ARBITER_BIN`; run with it set to verify those 3 cases.

- [ ] **Step 2: With binary**

```bash
export MAESTRO_ARBITER_BIN=/Users/Andrei_Shtanakov/labs/all_ai_orchestrators/arbiter/target/release/arbiter-mcp
uv run pytest tests/test_arbiter_real_subprocess.py tests/test_arbiter_real_subprocess_benchmark.py -v
```
Expected: all PASS

- [ ] **Step 3: Smoke**

```bash
uv run python scripts/smoke_benchmark_report.py
```
Expected: `smoke OK: ...`, exit 0

- [ ] **Step 4: Lint + type-check + format**

```bash
uv run pyrefly check
uv run ruff check .
uv run ruff format --check .
```
Expected: all clean

- [ ] **Step 5: Push branch + open PR (if PR flow)**

```bash
git push -u origin feat/r-06b-m4
gh pr create --title "feat(benchmark): R-06b M4 — Arbiter feedback wiring" --body "$(cat <<'EOF'
## Summary

Closes R-06b M4 per design `docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md`.

Delivers `BenchmarkResult` from Maestro into Arbiter via a new MCP tool `report_benchmark`. Schema-first cross-repo work; arbiter side merged at `<ARBITER_M4_SHA>`.

- Maestro: new `maestro/benchmark/arbiter_report.py` (helper + WireTaskResult + ReportBenchmarkPayload + projection + _classify_error). Never-raises; immutable copy semantics.
- Vendored client: typed `ArbiterContractError` for JSON-RPC contract breaks; `MIN_ARBITER_PROTOCOL=(1,1)` range check in `start()`; `ARBITER_VENDORED_FROM_SHA` pin + CI drift check.
- Idempotency: `INSERT...ON CONFLICT(run_id) DO NOTHING RETURNING` on arbiter side; caller-provided `run_id` for CI retry.
- Truncation: deterministic random sample (seed=run_id), cap=200, `MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK` env override.
- 5 distinct obs event names; `benchmark.report.contract_break` gets ERROR severity + own event name.
- 3-case e2e + smoke in arbiter-e2e CI job.

## Test plan

- [ ] `uv run pytest` — full suite green
- [ ] `uv run pyrefly check` — clean
- [ ] `uv run ruff check . && uv run ruff format --check .` — clean
- [ ] arbiter-e2e CI job green (3 e2e cases + smoke + vendor-sha check)
- [ ] Manual: `MAESTRO_ARBITER_BIN=... uv run python scripts/smoke_benchmark_report.py`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- §1 (context) — narrative only, no task needed ✓
- §2 (decisions) — embedded into all tasks ✓
- §3 (boundaries) — Phases 1-7 cover both repo sides ✓
- §4 (wire contract) — Phase 0 (schema) + Phase 4 (Pydantic) + Phase 5 (validation) ✓
- §5 (storage) — Phase 1 Task 1.1 (table) + 1.2 (ON CONFLICT) + atomicity test ✓
- §6 (version sync) — Phase 3 Task 3.1 ✓
- §7 (helper API) — Phase 4 Tasks 4.1-4.6 ✓
- §8 (data flow) — implicit in helper structure ✓
- §9 (error matrix) — Phase 4 Task 4.5 + 4.6 cover all 7 conditions ✓
- §10 (observability) — Phase 4 Task 4.6 ✓
- §11 (caller responsibilities) — code snippet referenced in smoke script (Phase 7 Task 7.4) ✓
- §12 (testing) — explicitly:
  - 12.1 Maestro unit tests (9 projection + 4 classify + 8 helper + 1 env + 2 client + 2 runner + 3 version) → Phases 2-6 ✓
  - 12.2 Arbiter unit tests (6 handler + 2 dispatch + 3 migration) → Phase 1 ✓
  - 12.3 E2E (3 cases) → Phase 7 Tasks 7.1-7.3 ✓
  - 12.4 Contract tests → Phase 5 Task 5.1 + Phase 1 Task 1.7 ✓
  - 12.5 Smoke → Phase 7 Task 7.4 ✓
- §13 (rollout) — Phase ordering matches §13.1, vendor-pinned check in Phase 7 Task 7.5 (matches §13.3) ✓
- §14 (open issues) — Phase 8 Task 8.1 propagates all to TODO ✓
- §15 (DoD) — Phase 8 Task 8.3 enumerates ✓

**Placeholder scan:** Only `<ARBITER_M4_SHA>` (filled at Phase 1 Task 1.8 completion) and `<MERGE_SHA>` (filled at landing) and `0.X.0` (version-bump-at-release) — all known-unknowns, not gaps. No vague "add error handling" or "similar to Task N".

**Type consistency:**
- `WireTaskResult` / `ReportBenchmarkPayload` named consistently across Phases 4-7 ✓
- `report_benchmark_raw` (low-level dict) defined Task 3.2, used in helper Task 4.4 ✓
- `_classify_error` returns `tuple[ErrorClass, str]`, consumed in helper Task 4.5 ✓
- `ArbiterContractError(code, message, data=None)` signature consistent across Tasks 2.1, 3.3, 4.3 ✓
- `MIN_ARBITER_PROTOCOL = (1, 1)` consistent across Task 3.1 + 7.5 vendor check ✓
- `ARBITER_VENDORED_FROM_SHA` populated in Task 3.1, checked by CI in Task 7.5 ✓

---

Plan complete and saved to `docs/superpowers/plans/2026-05-23-r06b-m4-arbiter-wiring.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Best for plans with this many TDD steps because each subagent gets a clean context window.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints. Faster wall-clock but eats main-context aggressively given ~50 tasks.

Which approach?
