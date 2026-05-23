# R-06b M4 — Benchmark feedback wiring (Maestro → Arbiter)

> **Status:** approved (brainstorm 2026-05-23, ready for writing-plans)
> **Supersedes:** open question #1 in `_cowork_output/decisions/2026-04-25-r06b-design.md`
> **Predecessors:** R-06b M1/M2/M3 landed on master (commits `5758dd8`, `290acde`, `a3e7aed`)
> **Successors:** R-06b M5 (CLI), R-07 (eval-driven routing)

## 1. Context

R-06b M1-M3 built the local benchmark pipeline: `BenchmarkRunner` (Protocols-driven async runner), `SpawnerResponder` (real `claude_code` / `codex_cli` / `aider` spawners as agents under test), `MaestroATPAdapter` (live ATP HTTP via `atp-platform-sdk≥2.0.0`). Output: a `BenchmarkResult` Pydantic object with per-agent per-benchmark score and per-task drill-down.

M4 closes the feedback loop: deliver `BenchmarkResult` into Arbiter so R-07 can use scores as a routing-decision input. The TODO formulation ("new MCP tool `report_benchmark` vs synthetic `task_id` in `report_outcome`") is resolved here as **new MCP tool**, with persist-only semantics for M4.

## 2. Decisions (rolled-up)

| Axis | Decision |
|------|----------|
| Cross-repo scope | New MCP tool `report_benchmark` in arbiter-mcp + Maestro emit-side |
| Arbiter behaviour in M4 | Persist only into new `benchmark_runs` table. No routing effect, no agent-stats update. R-07 reads later. |
| Storage shape | Single table + `per_task` jsonb blob; no normalization, no GIN index in M4 |
| Wire payload discipline | Strip free-form text (`prompt`, `response`, stderr); explicit typed fields only; no `dict[str, Any]` smuggling |
| Versioning | `payload_version` string in payload, validated by JSONSchema both sides; arbiter advertises `protocolVersion` in `initialize`, client checks `>= MIN_ARBITER_PROTOCOL` |
| Emit failure mode | Fire-and-forget + WARNING; helper never raises; `BenchmarkResult.report_status` + `report_error` for caller-side strict-mode (M5 CLI) |
| Emit site | Separate `report_benchmark_to_arbiter(result, client)` helper one layer above `BenchmarkRunner` (M1 contract preserved: runner stays protocol-only) |
| Idempotency | `run_id` is primary key on arbiter side; `INSERT ... ON CONFLICT(run_id) DO NOTHING RETURNING run_id` pattern; response `{status: "created" | "duplicate"}`. `run_id` is caller-generated (M5 CLI flag `--run-id`); runner accepts but does not own. |
| Truncation | Deterministic random sample (seed=`run_id`) of `per_task` when > cap; cap=200 default, overridable via `MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK` env or `max_per_task` arg |

## 3. Boundaries

### 3.1 Arbiter (Rust, `arbiter/arbiter-mcp/`)

- New MCP tool `report_benchmark` (4th, alongside `route_task` / `report_outcome` / `get_agent_status`)
- New module `src/tools/report_benchmark.rs` + registration in `server.rs` (schema declaration + handler + dispatch + `tools/list` entry)
- New SQLite migration through existing `schema_migrations` journal (LABS-85 pattern)
- Bump `Cargo.toml` minor version (e.g. `0.x.0 → 0.(x+1).0`)
- `initialize` response advertises bumped `protocolVersion` (e.g. `1.0 → 1.1`)

### 3.2 Maestro (Python, `maestro/`)

- `maestro/benchmark/models.py` — additive: `BenchmarkResult` gets `report_status: Literal["ok","failed","skipped"] = "skipped"` and `report_error: str | None = None`. `BenchmarkTaskResult` gets `task_type: str | None = None` and `score: float | None = None`. **Not touched:** existing fields (`prompt`, `response`, etc.) — still needed by M2 spawner and M5 CLI for local display.
- `maestro/benchmark/arbiter_report.py` (new) — `WireTaskResult` + `ReportBenchmarkPayload` Pydantic models, `_classify_error` normalization, `_build_wire_payload` projection (incl. truncation), `report_benchmark_to_arbiter(result, client)` helper. Single Python module owning the emit path.
- `maestro/coordination/arbiter_client.py` (vendored update) — new method `async def report_benchmark(self, payload: ReportBenchmarkPayload) -> dict[str, Any]`. New constants `ARBITER_PROTOCOL_VERSION` (now `"1.1.0"`) and `MIN_ARBITER_PROTOCOL = (1, 1)`. Updated `start()` performs version check.
- `maestro/coordination/arbiter_errors.py` (additive) — new `ArbiterContractError(ArbiterClientError)` for JSON-RPC error codes -32600/-32602/-32603. Existing `ArbiterUnavailable` stays for transient (broken pipe, timeout).
- `maestro/benchmark/runner.py` — **minimal** additive: `BenchmarkRunner.run(...)` accepts optional `run_id: str | None = None`; auto-generates `uuid4()` when absent. Runner stays protocol-only per M1 contract — does not depend on arbiter.
- `maestro/benchmark/atp_client.py` — small extension to surface `task_type` / `score` from ATP request metadata into `BenchmarkTaskResult` (if ATP exposes them; null otherwise).
- `scripts/smoke_benchmark_report.py` (new) — happy-path CI smoke script.
- `_cowork_output/benchmark-contract/report_benchmark-v1.schema.json` (new) — single source of truth for wire shape.
- `_cowork_output/benchmark-contract/README.md` (new) — fetch instructions for arbiter CI.

### 3.3 Out-of-scope (explicit)

- ❌ CLI command `maestro benchmark` — R-06b M5
- ❌ `--strict-report` flag — R-06b M5
- ❌ Routing effect of benchmark scores — R-07
- ❌ W3C `traceparent` propagation across MCP JSON-RPC boundary — Observability M3 follow-up; affects all arbiter calls equally (not specific to M4)
- ❌ GIN index on `per_task` jsonb — R-07 (under real query patterns)
- ❌ Normalized `benchmark_task_results` table — R-07
- ❌ Outbox / background retry for report — YAGNI; revisit if CI churn shows loss
- ❌ Pre-flight `get_agent_status` before `report_benchmark` — never (extra RPC)
- ❌ Writing into `event_log.py` — never (event_log scope is scheduler/orchestrator task lifecycle; benchmark.report.* lives in `obs.emit` only)
- ❌ Multi-agent benchmark in one run — runner is single-agent-per-run by M1
- ❌ Forward-compat for major version bumps — additive fields covered; major bump requires explicit `payload_version` migration
- ❌ Authentication / authorization for `report_benchmark` — M4 assumes subprocess-local trust model (arbiter spawned by the same caller process as Maestro, single tenant). Multi-tenant arbiter (shared service across Maestro instances) requires real auth — see §14 (auth in CI / service-account token tracked under M5 scope; multi-tenant auth would be a separate ticket triggered by that move)

## 4. Wire contract

### 4.1 Single source of truth

`_cowork_output/benchmark-contract/report_benchmark-v1.schema.json` (JSONSchema draft 2020-12, mirrors `_cowork_output/observability-contract/` pattern). Both repos validate against this file; arbiter CI fetches it from Maestro at pinned SHA (concrete mechanism — HTTP fetch / git submodule / copy-on-bump — chosen in writing-plans).

### 4.2 Pydantic models (Maestro side)

```python
# maestro/benchmark/arbiter_report.py
from typing import Literal
from pydantic import BaseModel, ConfigDict

class WireTaskResult(BaseModel):
    """Projection of BenchmarkTaskResult for arbiter persistence.
    Fields are explicit; no Dict[str, Any] smuggling. Schema evolution =
    explicit field + payload_version bump + contract test update."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_index: int
    task_type: str | None
    score: float | None
    tokens_used: int | None
    duration_seconds: float
    error_class: Literal["timeout", "crash", "test_failure", "other"] | None

class ReportBenchmarkPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    payload_version: Literal["1.0.0"]
    run_id: str
    benchmark_id: str
    agent_id: str
    ts: str  # RFC3339 UTC
    score: float
    score_components: dict[str, float]  # always present, possibly empty
    total_tokens: int | None
    total_cost_usd: float | None
    duration_seconds: float
    per_task: list[WireTaskResult]
    per_task_total_count: int
    per_task_truncated: bool
```

### 4.3 Response

```json
{
  "status": "created" | "duplicate",
  "run_id": "string"
}
```

`status` is explicit literal (not boolean `recorded`); extensible to `"superseded"` / `"rejected"` without breaking clients.

### 4.4 Free-form strip rationale

`WireTaskResult` excludes `prompt`, `response`, stderr, stack traces. Rationale:
- Bounds per-row payload size (a typical swe-bench task has 2-10 KB of prompt text; 100 tasks × 10 KB = 1 MB per row, untenable)
- Maestro retains free-form data in its in-memory `BenchmarkResult` and `logs/` for local debug; arbiter stores only what routing decisions need
- jsonb is not enforced-typed; `extra="forbid"` on `WireTaskResult` + JSONSchema validation both sides catches accidental field smuggling

## 5. Storage (arbiter side)

### 5.1 Schema

```sql
-- migration <next> (uses schema_migrations journal from LABS-85)
BEGIN IMMEDIATE;

CREATE TABLE benchmark_runs (
    run_id                TEXT PRIMARY KEY,
    payload_version       TEXT NOT NULL,
    benchmark_id          TEXT NOT NULL,
    agent_id              TEXT NOT NULL,
    ts                    TEXT NOT NULL,
    score                 REAL NOT NULL,
    score_components      TEXT NOT NULL,   -- JSON dict
    total_tokens          INTEGER,
    total_cost_usd        REAL,
    duration_seconds      REAL NOT NULL,
    per_task              TEXT NOT NULL,   -- JSON array of WireTaskResult
    per_task_total_count  INTEGER NOT NULL,
    per_task_truncated    INTEGER NOT NULL, -- 0/1
    inserted_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_benchmark_runs_agent_bench_ts
    ON benchmark_runs(agent_id, benchmark_id, ts DESC);

INSERT INTO schema_migrations (version, applied_at) VALUES (<N>, datetime('now'));

COMMIT;
```

Covering index supports the obvious R-07 query: "latest N benchmark scores for agent X on benchmark Y".

Whole migration wrapped in `BEGIN IMMEDIATE ... COMMIT`. Partial failure → automatic ROLLBACK to pre-migration state.

### 5.2 Insert pattern (idempotency)

```sql
INSERT INTO benchmark_runs (run_id, payload_version, ...)
VALUES (?, ?, ...)
ON CONFLICT(run_id) DO NOTHING
RETURNING run_id;
```

If `RETURNING` yielded a row → `status="created"`. If empty → `status="duplicate"`. Atomic; concurrent writers cannot produce both INSERTs.

## 6. Version sync (vendored client)

```python
# maestro/coordination/arbiter_client.py
ARBITER_PROTOCOL_VERSION = "1.1.0"             # advertised by current vendor build
MIN_ARBITER_PROTOCOL: tuple[int, int] = (1, 1) # minimum (major, minor) we accept
ARBITER_VENDORED_FROM_SHA = "<arbiter sha after report_benchmark lands>"

async def start(self) -> dict[str, Any]:
    response = await self._send_initialize(...)
    server_version = _parse_version(response.get("protocolVersion", "0.0"))
    if server_version[0] < MIN_ARBITER_PROTOCOL[0]:
        raise ArbiterContractError(
            -1, f"protocol major mismatch: server={server_version}, min={MIN_ARBITER_PROTOCOL}"
        )
    if server_version < MIN_ARBITER_PROTOCOL:
        logger.warning(
            "arbiter protocol minor lower than required: server=%s, min=%s — "
            "report_benchmark may be missing",
            server_version, MIN_ARBITER_PROTOCOL,
        )
    return response
```

Server (arbiter `Cargo.toml` + initialize handler) is the **single source of truth** for the actual protocol version. Vendored client declares only the **minimum**. Bumping arbiter without bumping the Python vendor → graceful degradation: WARNING on minor low, hard `ArbiterContractError` on major low. No silent drift.

**Two independent version axes.** `payload_version` (§4.2, currently `"1.0.0"`) tracks the data schema of a single `report_benchmark` payload — bumps when fields are removed or reshaped (additive fields don't bump). `protocolVersion` (this section, currently `"1.1.0"`) tracks the MCP tool surface of arbiter — bumps when tools are added (e.g. R-03 was `1.0`, M4 adds `report_benchmark` → `1.1`) or removed. They evolve independently; a single payload-schema change can ship under the same protocol version, and a new tool addition doesn't change existing payload schemas.

**Additive-vs-breaking rule for `payload_version`.** Bump only when fields are removed or reshaped (breaking). Additive optional fields don't bump — they're detected through their presence in the payload, which is safe because the producer (Maestro `WireTaskResult` / `ReportBenchmarkPayload`) is strict (`extra="forbid"`) while the consumer (arbiter JSONSchema validation) is liberal (`additionalProperties: true`). This asymmetric trust lets old arbiters accept new-Maestro payloads silently and lets new arbiters surface unexpected fields from old Maestros as errors — both desirable directions.

**Pragmatic deviation from canonical MCP.** Bumping `protocolVersion` for a tool addition is not strictly canonical — MCP spec uses `protocolVersion` for wire-level protocol semantics, and individual tool presence is canonically discovered through `tools/list`. We bump it here because checking `tools/list` at every `start()` adds a round-trip per session and complicates the start handshake. The vendored client's `MIN_ARBITER_PROTOCOL = (1, 1)` is a session-startup invariant check; during a session, `tools/list` remains the source of truth (and is unaffected by this decision). Arbiter reviewers may push back — this is the agreed compromise.

## 7. Helper API

```python
# maestro/benchmark/arbiter_report.py
import os

_DEFAULT_MAX_PER_TASK = 200
REPORT_MAX_PER_TASK = int(
    os.getenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK", _DEFAULT_MAX_PER_TASK)
)
REPORT_TIMEOUT_S = 30.0

ErrorClass = Literal["unavailable", "timeout", "contract_break", "unexpected"]

async def report_benchmark_to_arbiter(
    result: BenchmarkResult,
    client: ArbiterClient | None,
    *,
    max_per_task: int = REPORT_MAX_PER_TASK,
) -> BenchmarkResult:
    """Send benchmark result to arbiter; return updated copy with report_status set.

    Never raises (except CancelledError, which is BaseException). On arbiter
    error/timeout, returns result.model_copy(update={
        "report_status": "failed",
        "report_error": "<error_class>: <details>",
    }).

    On client=None or arbiter disabled, returns "skipped".
    On success: "ok".
    """
```

Immutable update via `model_copy(update=...)`. Input/output are distinct objects. `BenchmarkResult` stays mutable (preserves M1 contract); helper never mutates.

### 7.1 Error classification

```python
def _classify_error(exc: BaseException) -> tuple[ErrorClass, str]:
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout", "report timed out"
    if isinstance(exc, ArbiterContractError):
        return "contract_break", f"{exc.code}: {exc.message}"
    if isinstance(exc, ArbiterUnavailable):
        return "unavailable", "arbiter unavailable"
    return "unexpected", f"{type(exc).__name__}: {exc}"

_ERROR_SEVERITY: dict[ErrorClass, Literal["warning", "error"]] = {
    "unavailable": "warning",     # transient
    "timeout": "warning",         # transient
    "contract_break": "error",    # vendored drift or payload bug — must fix
    "unexpected": "error",        # catch-all → tracking
}
```

`report_error` always has format `f"{error_class}: {details}"` — single grep-friendly format.

### 7.2 Truncation

Deterministic random sample (seed=`run_id`) when `len(per_task) > max_per_task`. **Not** head-N: many benchmarks order tasks by difficulty (easy first); head-N would systematically truncate hardest tasks → biased report. Random sample with seed is reproducible (re-runs with same `run_id` produce identical sub-sample, useful for debugging).

```python
def _sample_per_task(
    tasks: list[BenchmarkTaskResult], cap: int, run_id: str
) -> tuple[list[WireTaskResult], bool]:
    if len(tasks) <= cap:
        return [WireTaskResult.from_domain(t) for t in tasks], False
    rng = random.Random(run_id)  # deterministic per run
    sampled = sorted(rng.sample(tasks, cap), key=lambda t: t.task_index)
    return [WireTaskResult.from_domain(t) for t in sampled], True
```

`per_task_total_count` preserves original length → R-07 knows that 200 of 2000 = 10% sample and can adjust weighting.

## 8. Data flow

```
Caller (M5 CLI / CI)     Runner            arbiter_report     ArbiterClient    arbiter-mcp     SQLite
       │                   │                     │                  │              │              │
       │─runner.run(...)──▶│                     │                  │              │              │
       │                   │ (M1+M2+M3 flow)     │                  │              │              │
       │◀── BenchmarkResult│                     │                  │              │              │
       │                                         │                  │              │              │
       │─report_benchmark_to_arbiter(r, client)─▶│                  │              │              │
       │                                         │ project+sample   │              │              │
       │                                         │─report_benchmark▶│              │              │
       │                                         │                  │─JSON-RPC───▶│              │
       │                                         │                  │              │─INSERT─────▶│
       │                                         │                  │              │◀─ok/dup─────│
       │                                         │                  │◀─{status}────│              │
       │                                         │◀── dict ─────────│              │              │
       │◀── BenchmarkResult(report_status="ok")──│                  │              │              │
```

Three layers, isolated and independently testable:
- **runner**: protocol-only (M1 contract preserved)
- **helper**: projection + emit + classification (M4)
- **client**: vendored MCP transport

Single RPC. No pre-flight `get_agent_status`. Helper never raises.

## 9. Error matrix

| Condition | `report_status` | `event_name` | severity |
|-----------|-----------------|--------------|----------|
| `client is None` | `skipped` | `benchmark.report.skipped` | info |
| Success, `status="created"` | `ok` | `benchmark.report.succeeded` | info |
| Success, `status="duplicate"` | `ok` | `benchmark.report.duplicate` | info |
| `error_class="unavailable"` | `failed` | `benchmark.report.failed` | warning |
| `error_class="timeout"` | `failed` | `benchmark.report.failed` | warning |
| `error_class="contract_break"` | `failed` | `benchmark.report.contract_break` | **error** |
| `error_class="unexpected"` | `failed` | `benchmark.report.failed` | error |

Contract break has dedicated event name (not just severity) — alerting rules can match `event_name = "benchmark.report.contract_break"` directly.

## 10. Observability

Span: `obs.span("benchmark.report", run_id=..., agent_id=...)` wraps the helper body. Events as in §9. Uses vendored `obs.py` from spec-runner@`fa6b106` (M2 scheduler instrumentation pattern). `trace_id` propagates within the async context naturally.

`obs.emit` is the **only** report channel — `event_log.py` is not touched (its scope is scheduler/orchestrator task lifecycle, not benchmark emit).

## 11. Caller responsibilities (M5 / CI)

| Decision | Owner | Default |
|----------|-------|---------|
| Whether to emit (arbiter configured?) | Caller passes `client=None` to disable | If `arbiter` block in config → on |
| Exit code on `report_status="failed"` | CLI `--strict-report` flag (M5) | exit 0 + WARNING |
| stdout format on `report_status="ok"` | CLI presentation | text summary + run_id |
| Cleanup of arbiter subprocess | Caller uses `try/finally` around `client.start()` / `client.stop()` | Maestro doesn't manage |
| `run_id` stability for CI retry | Caller passes explicit `--run-id` | UUID4 fallback (new row per retry) |

### 11.1 Canonical caller pattern

```python
import sys
from maestro.benchmark import BenchmarkRunner, report_benchmark_to_arbiter
from maestro.coordination.arbiter_client import ArbiterClient, ArbiterClientConfig

async def main() -> int:
    client = ArbiterClient(ArbiterClientConfig(binary_path=...))
    await client.start()
    try:
        result = await runner.run(benchmark_id="swe-mini", agent_id="claude_code",
                                  run_id="ci-job-42")
        result = await report_benchmark_to_arbiter(result, client)
        print(f"benchmark {result.benchmark_id}: score={result.score:.3f}, "
              f"report={result.report_status}")
        if result.report_status == "failed":
            print(f"  warning: {result.report_error}", file=sys.stderr)
        return 0
    finally:
        await client.stop()
```

This pattern is what M5 CLI wraps; CI scripts use it directly. The 5-line shape isolates the cleanup obligation and the strict/non-strict decision (CI can branch on `result.report_status == "failed"` for `--strict-report` exit-1 semantics without any M4-side support).

## 12. Testing

### 12.1 Maestro unit tests (`tests/test_benchmark_arbiter_report.py`)

**Projection (`_build_wire_payload`) — 9 cases:**
1. Happy: all fields mapped, `payload_version` constant
2. No truncation: `per_task ≤ cap` → all preserved, `truncated=False`
3. Truncation: `per_task > cap` → sample applied, `truncated=True`, original count preserved
4. Deterministic: same `run_id` → identical sub-sample (call twice, assert equality)
5. Free-form strip: `"prompt" not in payload.per_task[0].model_dump()`
6. `error` → `error_class` bucketing
7. Empty `per_task=[]` → `truncated=False`, `total_count=0`
8. Boundary `len == cap` → not truncated
9. Different `run_id`s → different sub-samples (guard against global-seed regression)

**Error classification (`_classify_error`) — 4 cases using typed exceptions (not string match):**
- `ArbiterUnavailable("…")` → `("unavailable", "arbiter unavailable")`
- `ArbiterContractError(-32602, "…")` → `("contract_break", "-32602: …")`
- `asyncio.TimeoutError()` → `("timeout", "report timed out")`
- `ValueError("oops")` → `("unexpected", "ValueError: oops")`

**Helper (`report_benchmark_to_arbiter`) — 8 cases with `MagicMock(spec=ArbiterClient)`:**
1. `client=None` → `report_status="skipped"`, no RPC, `benchmark.report.skipped` emitted
2. Success `status="created"` → `report_status="ok"`, `benchmark.report.succeeded` (info); `client.report_benchmark` called exactly once with right payload
3. Success `status="duplicate"` → `report_status="ok"`, `benchmark.report.duplicate` (not `succeeded`)
4. `ArbiterUnavailable` → `failed`, warning severity
5. `ArbiterContractError` → `failed`, `benchmark.report.contract_break` event, error severity
6. `asyncio.TimeoutError` → `failed`, error_class=timeout
7. Immutability: returned object ≠ input; input `.report_status == "skipped"` (default) after call
8. `asyncio.CancelledError` propagates (not caught by `except Exception`) — `pytest.raises(CancelledError)`

**`env REPORT_MAX_PER_TASK` override — 1 case:** monkeypatch env → helper uses overridden cap

**ArbiterClient additive — 2 cases:**
- `report_benchmark(payload)` → `_call_tool("report_benchmark", payload.model_dump(mode="json"))`
- `ReportBenchmarkPayload(missing required)` → `ValidationError` at construct, not RPC

**Runner additive — 2 cases:**
- `run(...)` without `run_id` → result contains UUID4
- `run(..., run_id="ci-job-42")` → result contains exactly `"ci-job-42"`

**Version sync — 3 cases:**
- mock `initialize.protocolVersion="1.0"` → `start()` logs WARNING (minor low)
- mock `initialize.protocolVersion="2.0"` → `start()` raises `ArbiterContractError` (major mismatch)
- mock `initialize.protocolVersion="1.5"` → `start()` ok, no warning

### 12.2 Arbiter unit tests (Rust)

**`tools/report_benchmark.rs` — 6 cases:**
1. Valid payload → `INSERT`, response `{status: "created"}`
2. Duplicate `run_id` → `{status: "duplicate"}`, exactly 1 row
3. Concurrent duplicate (two `tokio::join!` writers same `run_id`) → one created, one duplicate, 1 row, no error
4. Missing required field → JSON-RPC `-32602`, no INSERT
5. `payload_version` not matching server's accepted set (e.g. `"2.0.0"` when server pins `"1.0.0"`) → JSON-RPC error `"unsupported payload_version"`. **Note:** Rust test sends a raw synthetic JSON-RPC frame with `"payload_version": "2.0.0"` directly, bypassing Pydantic — Maestro's outbound path can't produce this thanks to `Literal["1.0.0"]`. This test exercises arbiter's strict server-side validation, not Maestro's client-side.
6. Malformed `per_task` (non-JSON string) → JSON-RPC error, no INSERT

**`server.rs` dispatch — 2 cases:**
- `tools/list` includes `"report_benchmark"`
- `tools/call name=report_benchmark` routes to handler

**Migration — 3 cases:**
- Fresh DB: migrations run, `benchmark_runs` + index exist
- Upgrade: pre-M4 DB → migration applies, idempotent re-run
- **Atomicity**: fault injection between `CREATE TABLE` and `INSERT INTO schema_migrations` → ROLLBACK to pre-migration state

### 12.3 Cross-repo e2e (`tests/test_arbiter_real_subprocess_benchmark.py`)

Auto-skip without binary; `MAESTRO_ARBITER_BIN` override. Mirrors R-05 pattern.

1. End-to-end **created**: real subprocess + real `ArbiterClient` + real payload → `{status: "created"}`; read SQLite, verify all columns including JSON-parsed `per_task`
2. End-to-end **duplicate**: same `run_id` twice → first created, second duplicate, 1 row, identical `per_task` blob in both responses
3. End-to-end **contract_break**: send payload with missing required field via raw `_call_tool` → expect `ArbiterContractError`; via helper → expect `report_status="failed"` + `benchmark.report.contract_break` event + 0 rows

### 12.4 Contract tests (machine-checkable JSONSchema)

Shared: `_cowork_output/benchmark-contract/report_benchmark-v1.schema.json` (request + response).

**Maestro (`tests/test_benchmark_contract.py`) — 3 cases:**
- Valid `ReportBenchmarkPayload.model_dump_json()` validates against schema
- Valid response `{"status": "created", "run_id": "x"}` validates against response schema
- **Forward-compat (evolution test):** payload with extra optional field validates (additionalProperties allowed); response with extra info field doesn't crash helper

**Arbiter (Rust, `tests/contract_test.rs`) — 2 cases:**
- Incoming `arguments` validates against shared schema before routing into handler
- Outgoing response validates against shared schema

### 12.5 CI smoke (`scripts/smoke_benchmark_report.py`)

Happy-path one-function script. Run as final step of `arbiter-e2e` CI job (after pytest). Spawns real arbiter, sends one `BenchmarkResult` through helper, asserts row in SQLite, prints `smoke OK: run_id=...` and exits 0. Replaces manual smoke (no manual checks in DoD).

## 13. Rollout

### 13.1 Step order (schema-first)

| Step | Repo | What | Gate |
|------|------|------|------|
| **0** | Maestro | **Finalize** `_cowork_output/benchmark-contract/report_benchmark-v1.schema.json` (request + response sections) | **Schema file committed during brainstorm completion, before writing-plans starts; any later schema change requires re-review** |
| 1 | arbiter | Migration + table + handler + dispatch + unit tests, all against schema from step 0 | Rust CI green |
| 2 | arbiter | Bump `Cargo.toml` minor; tag `arbiter@<sha>` | Release binary built |
| 3 | Maestro | Pin `ARBITER_PINNED_SHA=<arbiter step-2 sha>` in `.github/workflows/ci.yml` (arbiter-e2e job); also set vendored `ARBITER_VENDORED_FROM_SHA` to same value | arbiter-e2e job green |
| 4 | Maestro | Vendored client update + helper + projection + tests, all against schema from step 0 | Unit + contract + e2e green; smoke script green |
| 5 | Maestro | Merge M4 PR | master |

Steps 1-2 can be a separate mini-PR on arbiter; they don't block Maestro writing-plans (plans can be written and reviewed in parallel with arbiter work).

### 13.2 Schema location — fixed

`_cowork_output/benchmark-contract/report_benchmark-v1.schema.json` **lives in Maestro repo** as single source of truth. Not duplicated. Arbiter CI fetches at pinned Maestro SHA. Mechanism (HTTP fetch / git submodule / copy-on-bump) — chosen in writing-plans.

### 13.3 Vendored-vs-pinned consistency CI check

In the `arbiter-e2e` CI job (Maestro side), add an early shell step:

```bash
PYVENDOR_SHA=$(python -c "from maestro.coordination.arbiter_client import ARBITER_VENDORED_FROM_SHA; print(ARBITER_VENDORED_FROM_SHA)")
if [ "$PYVENDOR_SHA" != "$ARBITER_PINNED_SHA" ]; then
    echo "::error::vendored copy out of sync with pinned arbiter: vendored=$PYVENDOR_SHA, pinned=$ARBITER_PINNED_SHA — re-vendor required"
    exit 1
fi
```

This is the only mechanical guarantee that `ARBITER_VENDORED_FROM_SHA` stays meaningful. Without it, the constant becomes decoration: someone bumps `ARBITER_PINNED_SHA`, forgets the vendored constant, CI keeps testing the old vendored copy against the new arbiter binary — silent drift exactly as the constant was meant to prevent.

### 13.4 Cross-repo coordination prerequisite

Before writing-plans starts: if arbiter is maintained by a different person, send them §3.1 (boundaries), §5 (storage), §4 (wire contract + schema file). Lock-in agreement on those artifacts = green light for parallel implementation. Otherwise, schema can drift mid-implementation.

## 14. Open issues (deferred / out-of-scope, registered in TODO at landing)

| Issue | Destination | Severity | Trigger |
|-------|-------------|----------|---------|
| Trace propagation across MCP JSON-RPC boundary (arbiter) | New ticket under Observability M3 | Medium | When benchmark.report.* events need to correlate with arbiter-side INSERT by trace_id (not just run_id) |
| Sampling policy for swe-bench-full (>1000 tasks) | R-06b M4b | Low | First PROD run of swe-bench-full; reassess cap=200 |
| GIN index on `per_task` jsonb | R-07 | Low | When R-07 starts writing SQL filters on per_task |
| Normalized `benchmark_task_results` table (migration from blob) | R-07 | Low | Same trigger as GIN — formal query demand |
| Retention policy (TTL / archive for `benchmark_runs`) | R-07 / unscheduled | Low | When table > 10k rows OR JSON blobs total > 1 GB |
| Vendored client → standalone PyPI `arbiter-py` package | R-14 | Low | Existing roadmap item; M4 enlarges vendor surface |
| Outbox + background retry for benchmark report | unscheduled | Low | If fire-and-forget often paints CI pipelines red |
| Outgoing benchmark trigger from arbiter ("router uncertain → run benchmark") | R-07 / unscheduled | Low | From open question #2 in `2026-04-25-r06b-design.md` |
| Auth in CI (service-account ATP token) | R-06b M5 / separate | Medium | M5 CLI scope; see open question #4 |

## 15. Definition of Done

1. All Maestro unit tests (~17 new + extensions) green, `pyrefly check` clean, `ruff check .` and `ruff format --check .` clean
2. All Rust unit tests (~11 new) green, `cargo test` clean, `cargo clippy` clean
3. Contract tests on both sides validate against `report_benchmark-v1.schema.json`
4. E2E real-subprocess CI job (3 cases: created + duplicate + contract_break) green
5. Vendored client version-sync test green (raises on major mismatch, warning on minor)
6. `scripts/smoke_benchmark_report.py` green in arbiter-e2e CI job (automated, not manual)
7. Migration runs in `BEGIN IMMEDIATE ... COMMIT` transaction; partial-fail rollback test green

**Documentation:**
- `TODO.md` — `[x] R-06b M4` entry with commit hash + 3-line sanity summary + open-issue entries from §14
- `_cowork_output/benchmark-contract/README.md` — schema overview + fetch instructions for arbiter CI
- `CHANGELOG.md` — `### 0.X.0 — R-06b M4` section
