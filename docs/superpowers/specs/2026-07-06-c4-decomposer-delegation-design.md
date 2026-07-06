# C4: decomposer → spec-runner delegation — design

**Date:** 2026-07-06
**Status:** approved
**Context:** Steward proposal (draft, spec-runner format) at
`docs/proposals/2026-07-05-c4-decomposer-delegation/`. Maestro's
`decomposer.py` carries a built-in copy of spec-runner's `tasks.md` format
(`SPEC_GENERATION_PROMPT`, "spec-runner parses this EXACT format") — a
duplicated format contract with a single true owner (spec-runner). This spec
replaces the built-in prompt with a delegation to `spec-runner plan --full`.

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

### 1. `generate_spec` delegates to spec-runner (REQ-501/503/504)

`ProjectDecomposer.generate_spec(workstream, workspace_path)`
(decomposer.py:327) — replace the `SPEC_GENERATION_PROMPT` + `_run_claude` +
write-`tasks.md` body with a spec-runner subprocess:

- Build the feature description from `WorkstreamConfig` fields:
  `"Title: {title}\n\nDescription: {description}\n\nScope: {', '.join(scope)}"`
  (the shape verified working with `--from-file` during the measurement).
  Write it to a `tempfile.NamedTemporaryFile` OUTSIDE `workspace_path`
  (the workspace is a git worktree that gets committed — a desc file inside
  it could be swept into the spec commit); delete it in a `finally`.
- `subprocess.run(cmd, cwd=workspace_path, capture_output=True, text=True,
  timeout=..., check=False)` where
  `cmd = ["spec-runner", "plan", "--full", "--from-file", <desc_path>,
  "--no-branch", "--no-commit", "--no-interactive"]` plus
  `["--budget", str(budget)]` when a budget is set (see §4). `--full` writes
  `spec/{requirements,design,tasks}.md` into `cwd` — exactly where the
  orchestrator already expects them.
- Timeout: `--full` runs ~6 min; use a generous default (e.g. 30 min,
  matching `_run_claude`'s existing 15-min-per-call ×~2 headroom — parametrize
  so tests don't wait).
- Non-zero exit → `DecomposerError` with the return code and `stderr[:500]`
  logged (mirrors `_run_claude`'s existing error shape). `FileNotFoundError`
  (spec-runner not installed) → `DecomposerError` with an actionable message.
- The subprocess runs synchronously (`subprocess.run`), matching the current
  synchronous `generate_spec` contract; the orchestrator calls it the same
  way (orchestrator.py:334).

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

- New `SpecRunnerConfig.spec_gen_budget_usd: float | None = Field(default=5.0,
  ge=0, description="USD cap for `spec-runner plan --full` spec generation; "
  "None disables the cap")` — configurable per project.yaml; the 5.0 default
  is ~20× the measured ~$0.25/workstream, generous headroom against a
  runaway plan.
- `ProjectDecomposer.__init__` gains `spec_gen_budget_usd: float | None = 5.0`.
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

`generate_spec`'s signature and its contract ("writes `spec/` into the
workspace") are unchanged. orchestrator (spawn/scope/PR), workspace lifecycle,
and downstream `spec-runner run --all` are untouched. The only observable
change: `spec/` now also contains `requirements.md` and `design.md` (extra
files; harmless — `run --all` reads only `tasks.md`).

## Testing

- Unit (mock `subprocess.run`): command shape is
  `["spec-runner", "plan", "--full", "--from-file", <path>, "--no-branch",
  "--no-commit", "--no-interactive", "--budget", "5.0"]`; `cwd=workspace_path`;
  the description file contains title/description/scope; non-zero exit →
  `DecomposerError` carrying the code + stderr; `FileNotFoundError` →
  `DecomposerError` with an actionable message.
- Budget: `spec_gen_budget_usd=None` → no `--budget` flag; a custom value →
  `--budget <value>`.
- Regression: `decompose()` still works via `_run_claude` (untouched);
  existing decomposer tests green.
- Grep guard: `SPEC_GENERATION_PROMPT` and `_write_spec_files` absent from the
  repo after the change.
- Golden (optional, real subprocess): a real `spec-runner plan --full` on a
  fixture workstream produces a `spec/tasks.md` that `spec-runner`'s own task
  parser accepts — auto-skipped when spec-runner is absent (mirrors the
  `arbiter-e2e` optional-binary pattern), NOT in the default suite.

## Out of scope

- `--profile lite` (open question in the steward draft; NOT in `spec-runner
  plan --help` today — upstream work).
- A runtime spec-runner version gate (separate hardening ticket).
- Project decomposition (`decompose()` / `DECOMPOSE_PROMPT`) — only spec
  generation is delegated.
- Mode-1 scheduler (this is the Mode-2 orchestrator path).
- Consuming requirements.md/design.md in Maestro (they are written but only
  the steward layer above may read them).
