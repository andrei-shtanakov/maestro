# `maestro benchmark` CLI (R-06b M5) — design

**Date:** 2026-07-06
**Status:** approved
**Context:** R-06b "Agent benchmarking via ATP", final milestone M5 (TODO.md).
M1-M4 delivered every building block: `BenchmarkRunner` (M1, Protocol-first
API), `SpawnerResponder` (M2, wraps any `AgentSpawner`), `MaestroATPAdapter`
(M3, live ATP via `atp-platform-sdk`, auth delegated to the SDK), and
`report_benchmark_to_arbiter` (M4, fire-and-forget persist into arbiter's
`benchmark_runs`). M5 is the user-facing wiring only — no new benchmark
logic.

## Goal

```
maestro benchmark <benchmark-id> --agent claude_code
    [--workdir PATH] [--timeout 300.0] [--run-id ID] [--json] [--no-report]
```

Run one benchmark against one local agent harness and print the result;
optionally report it to the arbiter for routing signal.

## Decisions locked during brainstorm

- **Arbiter reporting: gated on env presence.** `MAESTRO_ARBITER_BIN` set
  and `--no-report` absent → report via the M4 path (fire-and-forget: a
  report failure never fails the benchmark; `report_status`/`report_error`
  are printed). Env unset → one skipped-note line. `--no-report` → skip
  explicitly.
- **Placement: inline `@app.command("benchmark")` in `maestro/cli.py`** —
  the house pattern for single commands (run/status/orchestrate are inline;
  `catalog_cli.py` earned its module by being a 4-command sub-app).

## Command semantics

### Agent selection

- `--agent` (required) must be a spawnable `AgentType` value; `auto` is
  rejected with "auto is a routing sentinel — pick a concrete harness"
  (exit 1). No spawner-registry lookup beyond the default set used by
  `maestro run` (five built-ins).
- `spawner.is_available()` is checked BEFORE contacting ATP: false →
  "agent CLI '<type>' not found in PATH" (exit 1). No tokens are spent on
  a doomed run.
- Model override is NOT part of M5 (`SpawnerResponder` does not take a
  model). The existing env layer works: `MAESTRO_<HARNESS>_MODEL` /
  catalog default. The command help mentions this.

### Working directory

- `--workdir PATH` or a fresh `tempfile.mkdtemp(prefix="maestro-bench-")`.
- `log_dir = <workdir>/logs` (created). Neither is cleaned up after the
  run — logs are the post-mortem material; the summary prints the path.

### Run

- `MaestroATPAdapter.from_env()`; construction/auth errors surface as an
  actionable exit-1 message naming the resolution chain (`ATP_TOKEN` env →
  `~/.atp/config.json`), never a traceback.
- `SpawnerResponder(spawner, workdir=workdir, log_dir=log_dir,
  timeout_seconds=--timeout)` (default 300.0 — the responder's own
  default).
- `asyncio.run()` around `async with adapter:` +
  `BenchmarkRunner(adapter, responder).run(benchmark_id, run_id=--run-id)`.
- ATP-side failures (unknown benchmark id, network, non-2xx) → exit 1 with
  the SDK's message, no traceback.

### Arbiter report (M4 path)

- Condition: `MAESTRO_ARBITER_BIN` set AND not `--no-report`.
- Build the vendored `ArbiterClient` with that binary,
  `report_benchmark_to_arbiter(result, client)`; the returned
  `BenchmarkResult` copy carries `report_status` (`succeeded` / `duplicate`
  / `failed` / `contract_break` / `skipped`) — printed in the summary and
  included in `--json` output.
- A failed/contract-break report does NOT change the exit code (M4
  fire-and-forget semantics; the obs events already grade severity).
- Env unset or `--no-report`: print
  "arbiter report skipped (MAESTRO_ARBITER_BIN unset)" /
  "(--no-report)" respectively.

### Output

- Human (default): Rich summary — benchmark id, agent, run_id, score +
  score_components, totals (tokens / cost / duration), per-task table
  (index, duration, tokens, cost, error), report status line, workdir/log
  path.
- `--json`: `BenchmarkResult.model_dump_json(indent=2)` to STDOUT; the
  human summary goes to STDERR so stdout is clean JSON for pipes.

### Exit codes

- 0 — the run completed (individual task errors do NOT fail the command:
  they are visible in the score and the per-task table; this matches
  `BenchmarkRunner`'s own semantics).
- 1 — infrastructure failure: bad `--agent`, unavailable agent CLI, ATP
  auth/start failure, benchmark not found.
- Message-not-traceback error contract throughout (same as
  `maestro models`).

## Files

- `maestro/cli.py` — new `@app.command("benchmark")` (imports from
  `maestro.benchmark`; async body via `asyncio.run`).
- `tests/test_cli_benchmark.py` — new test module (CliRunner; fakes
  adapted from `tests/test_spawner_responder.py` /
  `tests/test_benchmark_runner.py` patterns; monkeypatched
  `MaestroATPAdapter.from_env` and `report_benchmark_to_arbiter`).
- `CLAUDE.md` — Development Commands gains the benchmark line; TODO.md M5
  item ticked.

## Testing

- Happy path: mocked adapter yields 2 tasks; fake spawner writes parsable
  logs; output contains score, per-task rows; exit 0.
- `--json`: stdout parses as JSON and round-trips through
  `BenchmarkResult.model_validate_json`; human text absent from stdout.
- `--agent auto` → exit 1 (sentinel rejection); `--agent nosuch` → exit 1
  (invalid enum, Typer/our message); unavailable agent (mock
  `is_available` → False) → exit 1, ATP never contacted (assert the
  mocked `from_env` not called).
- Auth failure (from_env raises) → exit 1, message mentions ATP_TOKEN, no
  traceback.
- Report gating: env unset → "skipped" note, `report_benchmark_to_arbiter`
  not called; `--no-report` with env set → not called; env set → called
  once, `report_status` from the returned copy printed.
- `--run-id` forwarded to `BenchmarkRunner.run`.
- Task-error run (one task errors) → still exit 0, error visible in table.

## Out of scope

- `--model` override (needs SpawnerResponder API change — separate ticket
  if demanded).
- Score-threshold exit codes; multi-agent comparison runs.
- Changing `MAESTRO_BENCHMARK_MAX_PER_TASK` handling (already env-driven
  in arbiter_report).
- Mode 2 / orchestrator integration.
