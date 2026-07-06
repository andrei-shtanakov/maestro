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

- `--agent` (required). Allowed set is EXPLICIT: `claude_code`,
  `codex_cli`, `aider`, `opencode` — the AI harnesses. Two values are
  rejected with distinct messages (exit 1):
  - `auto` — "auto is a routing sentinel — pick a concrete harness";
  - `announce` — "announce is a no-op echo harness — benchmarking it would
    record a fake success as routing signal".
  Anything else → invalid-choice error naming the allowed set.
- `spawner.is_available()` is checked BEFORE contacting ATP: false →
  "agent CLI '<type>' not found in PATH" (exit 1). No tokens are spent on
  a doomed run.
- Model override is NOT part of M5 (`SpawnerResponder` does not take a
  model). The existing env layer works: `MAESTRO_<HARNESS>_MODEL` /
  catalog default. The command help mentions this.

### Working directory / timeout validation

- `--workdir PATH` (created via `mkdir(parents=True, exist_ok=True)` if
  absent) or a fresh `tempfile.mkdtemp(prefix="maestro-bench-")`.
- The workdir path is printed BEFORE the run starts — on a crash the user
  must still know where the partial logs live.
- `log_dir = <workdir>/logs` (created). Neither is cleaned up after the
  run — logs are the post-mortem material.
- `--timeout` validated `> 0` (Typer `min=` constraint), default 300.0
  (the responder's own default).

### Run

- ATP endpoint: `--atp-url` option, default =
  `$MAESTRO_ATP_BASE_URL` env, else `http://localhost:8000` (the SDK
  default `from_env` hard-codes). Passed as
  `MaestroATPAdapter.from_env(platform_url=...)`.
- Token resolution is the SDK's (`ATP_TOKEN` env → `~/.atp/config.json`);
  note: the adapter CONSTRUCTOR does not authenticate — auth/network
  errors surface at `start_run`/first request. The error handler therefore
  wraps the ENTIRE ATP async flow (adapter context + runner.run), not just
  construction: any SDK/network exception → exit 1 with the message and a
  hint naming the token resolution chain, never a traceback.
- `SpawnerResponder(spawner, workdir=workdir, log_dir=log_dir,
  timeout_seconds=--timeout)`.
- `asyncio.run()` around `async with adapter:` +
  `BenchmarkRunner(adapter, responder).run(benchmark_id, run_id=--run-id)`.

### Arbiter report (M4 path)

- Condition: `MAESTRO_ARBITER_BIN` set AND not `--no-report`.
- **Client lifecycle is explicit** (mirrors
  `scripts/smoke_benchmark_report.py`):

  ```python
  client = ArbiterClient(ArbiterClientConfig(binary_path=arbiter_bin))
  try:
      await client.start()
      result = await report_benchmark_to_arbiter(result, client)
  except Exception as exc:           # start() failure = report failure
      result = result.model_copy(
          update={"report_status": "failed", "report_error": str(exc)}
      )
  finally:
      await client.stop()            # never leak the subprocess
  ```

  `client.start()` failure is a fire-and-forget report failure: the
  benchmark still exits 0, `report_status="failed"` with the error in
  `report_error`. `stop()` runs on every path, including exceptions.
- `report_status` values are the M4 MODEL contract —
  `Literal["ok", "failed", "skipped"]` (`BenchmarkResult.report_status`,
  models.py:79). `duplicate` and `contract_break` are observability
  events / `report_error` detail, NOT model statuses; the CLI prints
  `report_status` + `report_error` and does not extend the model
  (extending it for the CLI would stop being "wiring only").
- A failed report does NOT change the exit code (M4 fire-and-forget
  semantics; the obs events already grade severity).
- Env unset or `--no-report`: print
  "arbiter report skipped (MAESTRO_ARBITER_BIN unset)" /
  "(--no-report)" respectively; `report_status` stays `skipped`.

### Output

- Human (default): Rich summary — benchmark id, agent, run_id, score +
  score_components, totals (tokens / cost / duration), per-task table
  (index, duration, tokens, cost, error), report status line, workdir/log
  path.
- `--json`: `BenchmarkResult.model_dump_json(indent=2)` to STDOUT — and
  STRICTLY nothing else on stdout: the human summary, the workdir
  announcement, the skipped-report note, and any reporting errors ALL go
  to STDERR. stdout is clean JSON for pipes, byte-for-byte.

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
  `BenchmarkResult.model_validate_json`; stdout contains NOTHING but the
  JSON (workdir note, summary, report note all on stderr).
- `--agent auto` → exit 1 (sentinel message); `--agent announce` → exit 1
  (fake-signal message); `--agent nosuch` → exit 1 (invalid choice);
  unavailable agent (mock `is_available` → False) → exit 1, ATP never
  contacted (assert the mocked adapter factory not called). announce
  rejection likewise asserts ATP untouched.
- ATP failure surfaced from `start_run` (not just construction): mocked
  adapter whose `start_run` raises → exit 1, message mentions ATP_TOKEN
  hint, no traceback.
- Report gating: env unset → "skipped" note, `report_benchmark_to_arbiter`
  not called; `--no-report` with env set → not called; env set → called
  once, printed `report_status` comes from the RETURNED copy (not the
  input result).
- **Arbiter lifecycle**: with env set, mocked `ArbiterClient` records call
  order `start → (report) → stop`; `start()` raising → exit still 0,
  `report_status == "failed"`, `report_error` carries the message, and
  `stop()` was still awaited; report raising mid-flight → `stop()` still
  awaited (finally).
- `--atp-url` and `$MAESTRO_ATP_BASE_URL` reach the adapter factory as
  `platform_url` (flag beats env; env beats the localhost default).
- `--timeout 0` / negative → Typer validation error (exit 2).
- `--run-id` forwarded to `BenchmarkRunner.run`.
- Task-error run (one task errors) → still exit 0, error visible in table.

## Out of scope

- `--model` override (needs SpawnerResponder API change — separate ticket
  if demanded).
- Score-threshold exit codes; multi-agent comparison runs.
- Changing `MAESTRO_BENCHMARK_MAX_PER_TASK` handling (already env-driven
  in arbiter_report).
- Mode 2 / orchestrator integration.
