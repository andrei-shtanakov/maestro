# D2 + D1: open the AgentType gate & pass the routed model through to spawn

**Date:** 2026-07-01
**Status:** Approved (design)
**Scope:** Maestro only. Implements ADR-ECO-002 **D2** (open the `AgentType` routing gate)
and **D1** (execute the model the arbiter routed to).
**Related:** `../../../../_cowork_output/decisions/2026-06-20-adr-harness-model-management.md`
(ADR-ECO-002, D1–D4), `../../../../_cowork_output/decisions/2026-07-01-adr-eco-003-agent-catalog.md`
(ADR-ECO-003 — this unblocks its Action Item #4, Maestro harness generation).

## Problem

Two coupled gaps sit on the arbiter→spawn path in `maestro/scheduler.py`:

- **D2 (closed gate).** When the arbiter routes a task, `scheduler.py:756` validates the
  harness via `AgentType(harness_of_agent_id(decision.chosen_agent))`. `AgentType` is a
  closed `StrEnum` (`claude_code / codex_cli / aider / announce / auto`), so any harness
  outside it raises `ValueError` → HOLD, and the task never runs — even if a spawner for
  it exists. This blocks "Поток 2" (a new routable harness) from ADR-ECO-003.
- **D1 (model not passed).** `spawn()` takes no model argument. Each spawner derives its
  model from `os.environ.get("MAESTRO_<H>_MODEL") or DEFAULT_<H>_MODEL`
  (`spawners/claude_code.py:72`, `spawners/codex.py:69`). The arbiter's routed model
  (the `@model` half of `routed_agent_type`) is stored but never reaches execution, so the
  executed model can silently differ from the benchmarked/routed one — the exact drift the
  R-07 correctness property forbids.

Both are small, well-bounded changes on the same code path, delivered together.

## Key facts (from code exploration)

- `SpawnerRegistry` is already string-keyed and exposes `has_spawner()` / `get_spawner()`;
  the scheduler holds a plain `self._spawners: dict[str, SpawnerProtocol]`
  (`scheduler.py:222`). The enum gate is a redundant second validation in front of a
  registry that can already answer "can I spawn this harness?".
- After line 756, `chosen` is used only for the `is AgentType.AUTO` refuse check. Actual
  spawner selection (`scheduler.py:809-814`) is already string-keyed:
  `self._spawners.get(harness_of_agent_id(task.routed_agent_type))`, with a
  `None → SchedulerError` guard at 815.
- The two failure paths have **different** semantics and must be preserved:
  - `auto` → `logger.error` "refusing to spawn" + `ARBITER_ROUTE_HOLD reason=auto_not_resolved`, `return False`.
  - unknown harness → `logger.warning` HOLD + `ARBITER_ROUTE_HOLD reason=unknown_agent`, `return False`.
- Only `harness_of_agent_id` exists (`models.py:89`); there is no model helper yet.
- `aider.py` and `announce.py` carry no model concept today.
- Existing test `tests/test_spawners.py::test_spawn_creates_process_with_correct_args`
  mocks `subprocess.Popen` and asserts `cmd[cmd.index("--model")+1] == DEFAULT_CLAUDE_MODEL`
  — the D1 tests reuse this pattern.

## Decision

### D2 — registry-driven gate (chosen: approach A)

Replace the enum conversion at `scheduler.py:~751-778` with:

```python
harness = harness_of_agent_id(decision.chosen_agent)
if harness == AgentType.AUTO.value:            # "auto" sentinel — never spawnable
    logger.error("routing returned AUTO for task %s — refusing to spawn", task_id)
    ...emit ARBITER_ROUTE_HOLD reason=auto_not_resolved...
    return False
if harness not in self._spawners:              # registry membership, not the enum
    logger.warning("arbiter chose unknown agent %r for task %s — HOLD", ...)
    ...emit ARBITER_ROUTE_HOLD reason=unknown_agent...
    return False
```

The explicit `auto` check runs **before** the registry check so both distinct semantics
(error-refuse vs warning-HOLD) and both event reasons are preserved byte-for-byte. The
downstream `self._spawners.get(...)` lookup + `is_available()` check are unchanged.

Rejected alternatives:
- **B** (delete the gate, rely on the 814 lookup): that path raises `SchedulerError`
  (task → FAILED), not HOLD (retryable), and loses the distinct AUTO/unknown events.
- **C** (dynamically extend the enum from the catalog): fights Python enums; brittle.

### D1 — explicit optional `model` param (chosen: approach A)

- Add `model_of_agent_id(agent_id: str) -> str | None` to `models.py` (the part right of the
  first `@`, else `None`), mirroring the existing `harness_of_agent_id` docstring style.
- Add `model: str | None = None` to the abstract `spawn()` in `spawners/base.py` and to
  `SpawnerProtocol.spawn` in `scheduler.py`.
- At the spawn call (`scheduler.py:877`), pass
  `model=model_of_agent_id(task.routed_agent_type)`.
- Model-aware spawners resolve **routed > env > default** and record the outcome
  (claude_code, codex):
  ```python
  if model:
      resolved, source = model, "routed"
  elif os.environ.get("MAESTRO_CLAUDE_MODEL"):
      resolved, source = os.environ["MAESTRO_CLAUDE_MODEL"], "env"
  else:
      resolved, source = DEFAULT_CLAUDE_MODEL, "default"
  _obs_log.info("agent.model_resolved", harness="claude_code",
                model=resolved, source=source)
  ```
  Because `model` is `None` when there is no routing (scheduler mode), this falls through to
  env/default exactly as before.
- `aider.py` / `announce.py` accept the param and ignore it (no model concept).

Rejected alternative **B** (spawners read `task.routed_agent_type` themselves): couples every
executor to the `@` convention and routing, duplicates parsing, and worsens boundaries.

### Observability of the executed model (closes review P1 + P3)

The point of D1 is to kill "routed ≠ executed" drift — so the executed model must be
*observable*, not merely asserted in a unit test. Model resolution happens inside the spawner,
which runs within the scheduler's active `task.spawn` obs span (propagated via
`structlog.contextvars`). Each model-aware spawner therefore emits one trace-correlated
structured event at resolution time:

```
agent.model_resolved  { harness, model, source ∈ {routed, env, default} }
```

This closes two gaps the review flagged: (a) the resolved model + its source are now logged
(ADR-ECO-002 D1 option-A Cons: *"нужна валидация неизвестной модели"* — partially satisfied
here via observability); (b) the **scheduler-mode** path — where `report_outcome` records only
`agent_used = task.agent_type.value` (harness, no model, `scheduler.py:385`) — now still has the
executed model in the log stream. Full **catalog-membership validation** of the routed model
(warn/reject a model absent from `agents-catalog.toml`) is **deferred to AI#4**, when Maestro
reads the catalog; the spawner has no catalog access in this PR and pulling it in would be
scope creep. Until then, an unsupported routed model surfaces as the CLI's own failure →
FAILED/retry, with the `agent.model_resolved` log making the culprit explicit.

## Components changed

| File | Change |
|---|---|
| `maestro/models.py` | + `model_of_agent_id(agent_id) -> str \| None` |
| `maestro/scheduler.py` (gate ~751-778) | enum conversion → explicit `auto` check + registry membership check |
| `maestro/scheduler.py` (spawn call ~877) | compute routed model, pass `model=` to `spawn()` |
| `maestro/scheduler.py` (`SpawnerProtocol.spawn`) | + `model: str \| None = None` |
| `maestro/spawners/base.py` (`spawn` ABC) | + `model: str \| None = None` + docstring |
| `maestro/spawners/claude_code.py` | resolve `routed > env > default`; emit `agent.model_resolved`; update docstring (env is now a **fallback**, not an override) |
| `maestro/spawners/codex.py` | same, CODEX vars + `-m` flag; update docstring |
| `maestro/spawners/aider.py`, `announce.py` | accept `model`, ignore |

## Data flow

```
arbiter decision.chosen_agent = "claude_code@claude-opus-4-8"
  → harness = harness_of_agent_id(...)  = "claude_code"   (D2 gate: in self._spawners? yes)
  → task.routed_agent_type = "claude_code@claude-opus-4-8"
  → routed_model = model_of_agent_id(...) = "claude-opus-4-8"
  → spawner.spawn(..., model="claude-opus-4-8")
  → claude_code: model = "claude-opus-4-8" (routed wins over env/default)
  → subprocess: claude ... --model claude-opus-4-8
```

Scheduler-mode (no arbiter): `routed_agent_type is None` → `model=None` →
spawner falls back to `env or DEFAULT` (unchanged behavior).

## Error handling / compatibility

- `spawn()` gains an **optional** kwarg defaulting to `None`; existing callers (tests,
  scheduler-mode, aider/announce) are unaffected.
- `AgentType` enum **stays** — it remains the type of `task.agent_type` (static config) and
  the home of the `AUTO`/`ANNOUNCE` sentinels. D2 only stops using it as the routing spawn
  gate. Its old typo-guard role is now covered by the registry plus the AI#3 CI-conformance
  check (`_cowork_output/devtools/check-agent-id-conformance.py`).
- A garbage harness now yields HOLD (`unknown_agent`) instead of `ValueError → HOLD` — same
  net retryable outcome, cleaner path.
- **Behaviour change — `MAESTRO_<H>_MODEL` semantics (review P2).** With `routed > env`, the
  env var goes from "override/pin" (its role in #32) to "fallback when routing supplies no
  model." An operator who set it in an arbiter deployment to *force* a model is now silently
  overridden by the routed decision. This is intentional (it is the R-07 correctness property),
  but the docstrings at `claude_code.py:28` / `codex.py:28` ("override via `MAESTRO_CLAUDE_MODEL`")
  must be corrected to "fallback," and it belongs in the PR changelog.
- **Behaviour change — built-in harness without a spawner (review P4).** A valid `AgentType`
  (e.g. `announce`) that is *not* registered in `self._spawners` previously passed the enum gate
  and failed loudly at `SchedulerError` (`scheduler.py:817`); it now takes the `unknown_agent`
  HOLD path. Net acceptable (both are config errors; HOLD is retryable and names the harness in
  the log), but `unknown_agent` now conflates two causes — a typo/unregistered custom harness and
  a mis-wired built-in. Accepted knowingly; the log message names the offending harness either
  way.

## Testing

**D2** (in `tests/test_scheduler.py`, matching existing routing tests):
1. Register a dummy spawner keyed with a non-enum harness (`"opencode"`); arbiter returns
   `"opencode@glm-5.1"` → spawn proceeds (previously HOLD). This is the proof D2 works.
2. Harness with no registered spawner → HOLD, event `unknown_agent`.
3. `chosen_agent = "auto"` → refuse, event `auto_not_resolved`.

**D1** (in `tests/test_spawners.py`, reusing `test_spawn_creates_process_with_correct_args`):
1. `spawn(..., model="claude-opus-4-8")` → argv carries `--model claude-opus-4-8`.
2. `model="X"` + `MAESTRO_CLAUDE_MODEL=Y` set → argv = `X` (routed > env).
3. `model=None` + `MAESTRO_CLAUDE_MODEL=Y` → argv = `Y` (env, scheduler mode).
4. `model=None` + no env → argv = `DEFAULT_CLAUDE_MODEL`.
5. Same matrix for codex (`-m` flag).
6. `agent.model_resolved` log carries the correct `source` for each of cases 1–4
   (`routed` / `routed` / `env` / `default`) — capture via `caplog`/structlog capture.

**Unit** (`tests/test_models.py`): `model_of_agent_id` on `"h@m"` → `"m"`, `"h"` → `None`,
`""` → `None`, `"h@m@n"` → `"m@n"` (split on first `@`, symmetric with `harness_of_agent_id`).

**Regression:** full `uv run pytest` + `uv run pyrefly check` + `uv run ruff check .` green.

## Out of scope (explicitly)

- AI#4 (generating `DEFAULT_<H>_MODEL` from the catalog) — separate; this only unblocks it.
- Adding a real new spawner (e.g. opencode) — proven via a test dummy, not shipped.
- Any change to the `AgentType` enum members or to arbiter/ATP repos.
