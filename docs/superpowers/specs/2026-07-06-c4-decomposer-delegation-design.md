# C4: decomposer → spec-runner delegation — design

**Date:** 2026-07-06
**Status:** approved
**Context:** Steward proposal (draft, spec-runner format) at
`docs/proposals/2026-07-05-c4-decomposer-delegation/`. Maestro's
`decomposer.py` carries a built-in copy of spec-runner's `tasks.md` format
(`SPEC_GENERATION_PROMPT`, "spec-runner parses this EXACT format") — a
duplicated format contract with a single true owner (spec-runner). This spec
replaces the built-in prompt with a delegation to `spec-runner plan --full`,
and — because `--full` runs ~6 min vs today's ~54s — also converts the
now-longer spec generation to an async background task so it does not
serialize Mode-2's parallel pipeline (see §1b). So C4 is delegation PLUS a
targeted orchestrator concurrency change, not pure wiring.

## Delegation mode decided by measurement (2026-07-06)

A real cost measurement rejected the cheaper-looking alternatives and
confirmed the steward's `--full`:

- `plan --gated --stage tasks` fails instantly ("requirements must be
  APPROVED first") — gated forces the req→design→tasks approval chain,
  incompatible with Maestro's automatic parallel flow.
- `plan --stage tasks` (ungated) prints to stdout and never writes the file —
  using it would swap the format-prompt coupling for brittle stdout-scraping.
- **No 1-call, ungated, file-writing, tasks-only path exists.** Every
  file-writing path generates all three stages. `--full` (3 sequential Claude
  calls: req 53s / design 160s / tasks 148s ≈ 6 min; ~3× cost, ~$0.1-0.25 per
  workstream) is therefore the only clean delegation. The extra
  requirements/design are unused by Maestro's executor (`run --all` reads only
  tasks.md) but are plausibly consumed by the steward/governance layer above.

See memory `project_c4_full_deferred` for the full measurement.

## Changes

### 1. `generate_spec` delegates to spec-runner, ASYNC (REQ-501/503/504)

`ProjectDecomposer.generate_spec` becomes
`async def generate_spec(workstream, workspace_path)` (decomposer.py:327) —
replacing the `SPEC_GENERATION_PROMPT` + `_run_claude` + write-`tasks.md` body
with an async spec-runner subprocess. Async is REQUIRED, not cosmetic: `--full`
runs ~6 min and today's call blocks the orchestrator's event loop inline (see
§1b).

- Build the feature description from `WorkstreamConfig` fields:
  `"Title: {title}\n\nDescription: {description}\n\nScope: {', '.join(scope)}"`
  (the shape verified working with `--from-file` during the measurement).
  Write it to a `tempfile.NamedTemporaryFile("w", encoding="utf-8",
  delete=False)` OUTSIDE `workspace_path` (the workspace is a git worktree
  that gets committed — a desc file inside it could be swept into the spec
  commit); `Path(tmp).unlink(missing_ok=True)` in a `finally` (delete=False +
  explicit unlink is the Windows-safe pattern).
- `proc = await asyncio.create_subprocess_exec(*cmd, cwd=workspace_path,
  env={**os.environ, **child_env()}, stdout=PIPE, stderr=PIPE)` then
  `await asyncio.wait_for(proc.communicate(), timeout=...)` where
  `cmd = ["spec-runner", "plan", "--full", "--from-file", <desc_path>,
  "--no-branch", "--no-commit", "--no-interactive"]` plus
  `["--budget", str(budget)]` when a budget is set (§4). This mirrors the
  existing `run --all` spawn (orchestrator.py:360), including `child_env()`
  for trace propagation. `--full` writes `spec/{requirements,design,tasks}.md`
  into `cwd`.
- Timeout: `--full` runs ~6 min; default 30 min (parametrized so tests don't
  wait). `asyncio.TimeoutError` → kill the subprocess → `DecomposerError`.
- **Cancellation kills the subprocess (no orphan burning tokens):**
  `generate_spec` itself catches `asyncio.CancelledError` around the
  `communicate()` await, calls `proc.terminate()` (escalating to
  `proc.kill()` if it does not exit within a short grace), awaits the proc,
  and re-raises `CancelledError`. Without this, cancelling the background
  task (§1b shutdown) unwinds the coroutine but leaves `spec-runner plan
  --full` alive, spending real money. Timeout and generic-error paths
  terminate the proc the same way.
- Non-zero exit → `DecomposerError` with the return code and `stderr[:500]`
  (mirrors `_run_claude`'s existing error shape). `FileNotFoundError`
  (spec-runner not installed) → `DecomposerError` with an actionable message.
- **Post-condition check (fail fast):** after a zero exit, assert
  `(workspace_path / "spec" / "tasks.md").is_file()`; if absent →
  `DecomposerError("spec-runner plan --full exited 0 but spec/tasks.md was
  not created")`. A silent output-path/behavior shift must fail HERE, not
  surface later inside `run --all`.

### 1b. Concurrency model — generation is a background task (fixes the 6-min block)

**Problem this change would otherwise cause:** `_spawn_ready` (orchestrator.py:
279) loops `for zid in ready[:available]: await self._spawn_workstream(zid)`,
and `_spawn_workstream` calls `generate_spec` inline. Today's ~54s Claude call
already blocks the event loop per workstream; `--full`'s ~6 min turns
max_concurrent=3 into ~18 min of blocked loop during which `_monitor_running`
and shutdown cannot run — silently converting Mode-2's parallel pipeline into
a serial queue.

**Design:**

- `_spawn_workstream` no longer runs generation inline. It launches a
  background task: `self._generating[zid] = asyncio.create_task(
  self._generate_and_launch(zid))` and returns immediately.
- `_generate_and_launch(zid)` runs the former inline body as a coroutine:
  DECOMPOSING → create workspace → `await generate_spec(...)` → setup
  spec-runner config → commit spec (existing `run_in_executor` git step) →
  spawn `run --all` (existing `create_subprocess_exec`) → add to
  `self._running`. Lifecycle discipline:
  - **`self._generating.pop(zid, None)` runs in a `finally`** so a slot never
    hangs on any error, cancel, or success.
  - **Failures route through `_handle_failure(zid, str(exc))`, NOT a raw
    `update_workstream_status(..., FAILED)`.** `_handle_failure`
    (orchestrator.py:625) does the retry accounting — retries left →
    FAILED→READY with `retry_count++`; exhausted → FAILED→NEEDS_REVIEW +
    `stats.failed++`. A raw FAILED (what today's inline spawn-error handler
    does) would silently break retry semantics; the background path must be
    at least as correct.
  - **`CancelledError` is distinct from `DecomposerError`.** A shutdown
    cancel (§ below) → return the workstream to READY (resumable, no retry
    consumed) and re-raise/propagate the cancellation. A real
    `DecomposerError` → `_handle_failure` (retry path). Do not conflate them.
- **Slot accounting (no overspawn):**
  `available = max_concurrent - len(self._running) - len(self._generating)` —
  a DECOMPOSING workstream occupies a slot for the whole generation, so the
  loop never launches more than `max_concurrent` concurrent
  generation+run pipelines.
- The main loop is unchanged in shape (`_spawn_ready` → `_monitor_running` →
  wait) but `_spawn_ready` now returns in milliseconds; monitoring and
  shutdown stay responsive while generations proceed concurrently in the
  background.
- **Shutdown:** on `_shutdown_requested`, in-flight `_generating` tasks are
  cancelled; the cancellation propagates into `generate_spec`, which
  terminates the spec-runner subprocess (see §1's CancelledError handling —
  this is what prevents an orphaned token-burning process). Because the
  cancel is shutdown-driven (not a `DecomposerError`), `_generate_and_launch`
  returns the workstream to READY (no retry consumed) so `--resume`
  re-generates. Generations already past the `run --all` spawn are in
  `_running` and handled by the existing shutdown path.

### 2. Remove the format duplication (REQ-502)

Delete `SPEC_GENERATION_PROMPT` entirely. No `tasks.md` format description
remains anywhere in `decomposer.py`. spec-runner is the sole format owner.

### 3. Dead-code cleanup (REQ-507)

- `_write_spec_files` (marker-parser) — verified never called (grep across
  `maestro/`); delete it.
- `_run_claude` — KEPT: still used by `decompose()` (project → workstreams,
  a different path that stays on Claude CLI).
- The `_parse_decomposition` / `DECOMPOSE_PROMPT` path (project decomposition)
  is untouched — out of scope; only spec GENERATION is delegated.

### 4. Budget cap (plumbed, user decision 2026-07-06)

- New `SpecRunnerConfig.spec_gen_budget_usd: float | None = Field(default=1.0,
  ge=0, description="USD cap for `spec-runner plan --full` spec generation; "
  "None disables the cap")` — configurable per project.yaml. Default 1.0 is
  ~4-10× the measured ~$0.1-0.25/workstream: real protection against a runaway
  plan without being a de-facto no-cap (5.0 × 10-20 workstreams would be a
  meaningless ceiling). Explicit `None` is the opt-out for large/unusual
  specs.
- `ProjectDecomposer.__init__` gains `spec_gen_budget_usd: float | None = 1.0`.
- The `orchestrate` CLI (where the decomposer is constructed) passes
  `config.spec_runner.spec_gen_budget_usd` into the decomposer.
- `generate_spec` appends `["--budget", str(budget)]` only when the value is
  not None (None → no `--budget` flag, unbounded).

### 5. Version pin (REQ-505/DESIGN-505 — scoped down, see rationale)

The steward's REQ-505 wants "incompatible version → clear error at start".
But `SPEC_RUNNER_REQUIRED_VERSION = "2.0.0"` (spec_runner.py:31) is a pure
DOC constant — it is asserted NOWHERE at runtime (verified). Adding a runtime
`spec-runner --version` gate is net-new behavior absent everywhere in the
codebase, and the pin/installed versions already diverge (pin 2.0.0, local
2.8.1) with no ill effect.

**This spec extends the pin's COMMENT** to record that the contract now also
covers the `plan --full` authoring surface (the `--full`/`--from-file`/
`--no-interactive` flags and the `spec/{requirements,design,tasks}.md`
layout), and leaves the runtime-enforcement question to a separate hardening
ticket. Bumping the numeric floor is deferred: we do not know which
spec-runner version introduced `plan --full`, and inventing a floor without
that fact would be guesswork.

### 6. Backward compatibility (REQ-506)

`generate_spec`'s output contract ("writes `spec/` into the workspace") is
unchanged; its signature gains `async` (the sole caller is the orchestrator,
updated in §1b). workspace lifecycle, scope, PR flow, and downstream
`spec-runner run --all` are untouched. Observable changes: (a) `spec/` now
also contains `requirements.md` and `design.md` (harmless — `run --all` reads
only `tasks.md`); (b) Mode-2 spec generation now runs concurrently in the
background instead of blocking the loop (a strict improvement). `--resume`
semantics are preserved: a workstream interrupted during DECOMPOSING returns
to READY and re-generates on resume.

## Testing

**Decomposer unit (mock `asyncio.create_subprocess_exec`):** command shape is
`["spec-runner", "plan", "--full", "--from-file", <path>, "--no-branch",
"--no-commit", "--no-interactive", "--budget", "1.0"]`; `cwd=workspace_path`;
the description file contains title/description/scope; non-zero exit →
`DecomposerError` carrying code + stderr; `FileNotFoundError` →
`DecomposerError` with an actionable message; timeout → subprocess killed +
`DecomposerError`. Post-condition: zero exit but no `spec/tasks.md` →
`DecomposerError` (mock the subprocess to exit 0 without writing the file).
Budget: `None` → no `--budget` flag; a custom value → `--budget <value>`. The
async test uses `@pytest.mark.anyio`. **Cancellation:** cancelling an
in-flight `generate_spec` calls `proc.terminate()` (assert against a fake
proc, not merely that the task was cancelled) and re-raises `CancelledError`;
the temp desc file is still unlinked.

**Orchestration-level (the point-1 regression guard, mock decomposer +
spawner):**
- Generation runs as a background task: `_spawn_ready` returns promptly while
  a slow (awaitable) `generate_spec` is still in flight; `_monitor_running`
  gets to run in the same period.
- Slot accounting: with `max_concurrent=2` and 3 ready workstreams, at most 2
  are in `_generating`+`_running` at once — the 3rd waits (no overspawn).
- On generation success the workstream moves `_generating` → `_running` and
  `run --all` is spawned; the `_generating` slot is freed in `finally`.
- Generation `DecomposerError` routes through `_handle_failure`: with retries
  left → workstream back to READY and `retry_count` incremented (NOT a raw
  terminal FAILED); with retries exhausted → NEEDS_REVIEW + `stats.failed++`.
- Shutdown mid-generation: in-flight generation is cancelled, its subprocess
  terminated (assert terminate called), the workstream returns to READY with
  `retry_count` UNCHANGED (a shutdown is not a failure).

**Regression:** `decompose()` still works via `_run_claude` (untouched);
existing decomposer/orchestrator tests green.

**Grep guard:** `SPEC_GENERATION_PROMPT` and `_write_spec_files` absent from
the repo after the change.

**Golden — a real CI job (drift guard, not optional):** with the runtime
version gate deferred (§5), a real `spec-runner plan --full` on a fixture
workstream, asserting the produced `spec/tasks.md` is accepted by
spec-runner's own task parser, is the ONLY protection against authoring-format
drift. It runs as a **weekly-scheduled** CI job (mirroring the `arbiter-e2e`
drift-check cadence) — NOT per-PR, because `plan --full` spends real Claude
tokens. Locally it auto-skips when spec-runner is absent. This job is a
required deliverable of C4, not a nice-to-have.

## Out of scope

- `--profile lite` (open question in the steward draft; NOT in `spec-runner
  plan --help` today — upstream work).
- A runtime spec-runner version gate (separate hardening ticket).
- Project decomposition (`decompose()` / `DECOMPOSE_PROMPT`) — only spec
  generation is delegated.
- Mode-1 scheduler (this is the Mode-2 orchestrator path).
- Consuming requirements.md/design.md in Maestro (they are written but only
  the steward layer above may read them).
