# Changelog

## v0.4.0 — Rename Zadacha → Workstream (2026-05-23)

**Breaking changes** (no backward compatibility):
- CLI: `maestro zadachi` → `maestro workstreams`
- REST API: `/zadachi` endpoints → `/workstreams`; `/zadachi/{zadacha_id}` → `/workstreams/{workstream_id}`
- `project.yaml`: top-level key `zadachi:` → `workstreams:`
- DB schema: `zadachi` table → `workstreams`; `zadacha_dependencies` → `workstream_dependencies`; `zadacha_id` columns → `workstream_id`. Migration auto-applied on first run.
- Python API: `Zadacha`, `ZadachaStatus`, `ZadachaConfig`, `ZadachaNotFoundError`, `ZadachaAlreadyExistsError` → `Workstream`, `WorkstreamStatus`, `WorkstreamConfig`, `WorkstreamNotFoundError`, `WorkstreamAlreadyExistsError`. Code that imports these symbols must update.

**Motivation:** transliterated Russian word ("zadacha" / "zadachi") in identifiers was confusing for English-speaking users and code review. `Workstream` is the natural English term for the concept — a parallel independent track of work that owns its own git worktree, spec-runner subprocess, and final PR.

**Scheduler-mode `Task` concept is UNAFFECTED** — only the orchestrator-mode concept was renamed.

---

## v0.3.0 (2026-05-23)

### Added
- `maestro/benchmark/arbiter_report.py` — `report_benchmark_to_arbiter(result, client)` helper; never raises (except `CancelledError`); returns a copy with `report_status` / `report_error` set.
- `BenchmarkResult.report_status` (`Literal["ok","failed","skipped"]`) + `.report_error` (`str | None`) on the M1 model.
- `BenchmarkTaskResult.task_type` and `.score` (additive; populated from ATP `metadata.task_type` when present).
- `BenchmarkRunner.run(..., run_id: str | None = None)` — caller-provided `run_id` overrides ATP's for CI-retry idempotency.
- `ArbiterClient.report_benchmark_raw(payload)` — low-level MCP method.
- `ArbiterContractError(code, message, data)` — distinguishes JSON-RPC contract breaks (`-32600`/`-32602`/`-32603`) from transient `ArbiterUnavailable`.
- Vendored client: `ARBITER_PROTOCOL_VERSION = "1.1.0"`, `MIN_ARBITER_PROTOCOL = (1, 1)`, `ARBITER_VENDORED_FROM_SHA = "7aeb6b1..."`; `start()` validates server-advertised `protocolVersion` (major-mismatch → `ArbiterContractError`, minor-low → WARNING).
- `_cowork_output/benchmark-contract/report_benchmark-v1.schema.json` — single source of truth (schema-first).
- `scripts/smoke_benchmark_report.py` — CI smoke against a real arbiter subprocess.
- 5 distinct observability events: `benchmark.report.{skipped,succeeded,duplicate,failed,contract_break}` (contract_break gets ERROR severity).

### Configuration
- `MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK` env override (default 200) for per_task truncation.

### Changed
- `ARBITER_MCP_REQUIRED_VERSION` bumped `"0.1.0" → "0.2.0"` to match arbiter Phase 1 binary.
- `_send_and_receive` now raises `ArbiterContractError` (not `ArbiterUnavailable`) on JSON-RPC error codes -32600/-32602/-32603.

### Tests
- New: `tests/test_benchmark_arbiter_report.py` (~33 tests — projection, classification, helper paths, obs emit), `tests/test_benchmark_contract.py` (~9 tests — JSONSchema validation + forward-compat), `tests/test_arbiter_real_subprocess_benchmark.py` (3 e2e cases: created + duplicate + contract_break), `tests/test_arbiter_client_version.py` (5 version-sync tests), `tests/test_arbiter_errors.py` (4 contract-error tests), `tests/test_benchmark_models.py` (4 additive-field tests).
- Extended: `tests/test_arbiter_client.py` (+5 method/error-classification tests), `tests/test_benchmark_runner.py` (+4 run_id/task_type tests), `tests/test_benchmark_atp_client.py` (+3 task_type extraction tests).

### Cross-repo
- Requires `arbiter-mcp` at SHA `7aeb6b1a987a2610c9f2cddb38d90f42d849da42` or later (advertises `protocolVersion="1.1.0"`, new `report_benchmark` MCP tool, `benchmark_runs` table migration).

Design: `docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md`.
Plan: `docs/superpowers/plans/2026-05-23-r06b-m4-arbiter-wiring.md`.

## v0.2.0 (2026-04-17)

### Added
- **Arbiter MCP client integration (R-03)** — optional policy-engine routing.
  Declare an `arbiter:` section in the project YAML to spawn an arbiter
  subprocess, ask it to route every ready task (`advisory` or `authoritative`
  mode), and report back outcomes for the learning loop. See
  [`examples/with-arbiter.yaml`](examples/with-arbiter.yaml) for a full
  configuration reference. When the section is absent or `enabled: false`,
  Maestro stays on the zero-config `StaticRouting` path — **byte-identical
  to v0.1.0**; no subprocess, no routing overhead.
- `AgentType.AUTO` routing sentinel — let the arbiter pick the agent per task.
- New `maestro/coordination/` subpackage: `routing.py` (`StaticRouting`,
  `ArbiterRouting`, `make_routing_strategy` factory), `arbiter_client.py`
  (vendored MCP client), `arbiter_errors.py`.
- `Task` gains persisted arbiter routing fields (`routed_agent_type`,
  `arbiter_decision_id`, `arbiter_route_reason`, `arbiter_outcome_reported_at`)
  with automatic SQLite migration for pre-R-03 databases.
- Scheduler delivers outcomes on completion/failure, gates retries on
  arbiter mode (advisory retries regardless of delivery failure;
  authoritative waits for successful `report_outcome`), and runs a
  bounded re-attempt pass (5/tick) each loop iteration with an
  authoritative abandon timer (`abandon_outcome_after_s`, default 300s)
  as the escape hatch when the arbiter stays unreachable.
- Crash recovery closes dangling arbiter decisions on startup via
  `recover_arbiter_outcomes` (available standalone or through
  `StateRecovery.recover(routing=...)`).
- 10 new structured `EventType` members cover the route/outcome/recovery
  lifecycle; `HoldThrottle` helper collapses repeat HOLD events.
- Dependency bump: `authlib` 1.6.9 → 1.6.11 (transitive via `fastmcp`).

### Compatibility
- Zero-config projects (no `arbiter:` section) behave exactly as in v0.1.0.
  No subprocess is spawned, no routing overhead, and the scheduler's
  route-then-spawn path short-circuits through `StaticRouting`.
- SQLite migration is idempotent; upgrading an existing v0.1.0 database
  adds four nullable columns with no data changes.

### Docs
- [`docs/superpowers/specs/2026-04-16-r03-arbiter-mcp-client-design.md`](docs/superpowers/specs/2026-04-16-r03-arbiter-mcp-client-design.md) —
  architecture spec.
- [`docs/superpowers/plans/2026-04-16-r03-arbiter-mcp-client.md`](docs/superpowers/plans/2026-04-16-r03-arbiter-mcp-client.md) —
  32-step implementation plan (all complete).

### Tests
- +113 tests (1112 total), `pyrefly check` 0 errors, `ruff check .` clean,
  `ruff format --check .` clean.

## v0.1.0 (2026-04-06)

First public release.

### Features
- **Mode 1 (Task Scheduler):** DAG-based scheduling of AI coding agents
  (Claude Code, Codex, Aider) in a shared directory
- **Mode 2 (Multi-Process Orchestrator):** Decompose projects into independent
  workstreams, run each in isolated git worktrees via spec-runner, auto-create PRs
- Spawner registry with 4 built-in spawners (claude_code, codex, aider, announce)
- SQLite state persistence with crash recovery
- CLI: run, status, retry, stop, orchestrate, workstreams, workspaces
- Web dashboard with DAG visualization and SSE updates
- Desktop notifications (macOS/Linux)
- Auto-commit per task with git diff summary
- Dogfood-tested: Maestro builds itself (3 weeks of real usage)
