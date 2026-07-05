# opencode spawner wiring — design

**Date:** 2026-07-05
**Status:** approved
**Context:** ADR-ECO-003c (opencode = first routable open-model harness).
`maestro/spawners/opencode.py` landed on master inside the PR #41 squash
(`7277700`) with no registration wiring and no tests. This spec wires it in.

## Goal

Make `opencode` a first-class selectable agent type in Mode 1:

- YAML: `agent_type: opencode` validates and spawns.
- Arbiter routing: `opencode@<model>` resolves through the D2 registry gate.
- Entry-point discovery: `create_default_registry()` finds it.
- Token usage is parsed from its JSONL output (tokens-only; see Cost tracker).

## Changes

### 1. Registration (mechanical)

- `maestro/models.py` — add `OPENCODE = "opencode"` to `AgentType`, placed
  before `AUTO` so the routing sentinel stays last.
- `maestro/spawners/__init__.py` — import + `__all__` entry for
  `OpencodeSpawner`. NOT re-exported from `maestro/__init__.py` (top level
  only exports `ClaudeCodeSpawner`; follow that pattern).
- `pyproject.toml` — `opencode = "maestro.spawners.opencode:OpencodeSpawner"`
  under `[project.entry-points."maestro.spawners"]`.
- `maestro/cli.py:399` — add `"opencode": OpencodeSpawner()` to the default
  spawner set; update the "all four built-ins" comment to five.
- Regenerate `maestro/schemas/project_config.json` via
  `uv run python -m maestro.schemas.generate` (enum picked up automatically).

### 2. Cost tracker (variant A: tokens-only)

`opencode run --format json` writes JSONL to stdout: one JSON object per
event, with `type` and `part` keys. `step_finish` events carry per-step
usage in `part.tokens`:

```json
{"type": "step_finish",
 "part": {"reason": "stop",
          "tokens": {"input": 22443, "output": 118, "reasoning": 0,
                     "cache_read": 21415, "cache_write": 0},
          "cost": 0.001}}
```

- New `parse_opencode_log(log_content) -> TokenUsage` in
  `maestro/cost_tracker.py`: iterate lines, JSON-parse each, and for
  `type == "step_finish"` accumulate `input_tokens += part.tokens.input`,
  `output_tokens += part.tokens.output + part.tokens.reasoning`.
  - `step_finish` carries **per-step** usage (Vercel AI SDK stream
    semantics), so summing across events is the correct aggregation.
  - `cache_read` / `cache_write` and `part.cost` are ignored (cost-from-log
    is a separate follow-up; note it in a comment).
  - Malformed / non-JSON lines are skipped silently — the log file may
    contain stderr noise (stderr is redirected to the same fd).
- Register `AgentType.OPENCODE: parse_opencode_log` in the `parse_log`
  parsers map.
- `PRICING["opencode"] = (0.0, 0.0)` with a comment: open-model harness,
  price depends on the routed model, so cost is recorded as 0 until
  cost-from-log lands.

### 3. D2 proof test fix

`tests/test_scheduler.py:1694` proves the open D2 gate using `"opencode"`
as its example of "a harness that is NOT an AgentType member but has a
spawner". Step 1 falsifies that premise. Replace the fake harness name with
`"fakeharness"` (stays outside the enum) and fix the docstring. Test
semantics (open D2 gate) are unchanged.

### 4. Tests (new)

- `TestOpencodeSpawner` in `tests/test_spawners.py`, mirroring
  `TestCodexSpawner`:
  - `agent_type == "opencode"`; `is_available()` via `shutil.which`.
  - Spawn command shape:
    `opencode run --format json -m opencode/<model> <prompt>`.
  - `_qualify`: bare id gets the `opencode/` prefix; an already
    provider-qualified id (`provider/model`) passes through unchanged.
  - Model precedence: routed > `MAESTRO_OPENCODE_MODEL` > catalog default;
    `agent.model_resolved` obs event carries the right `source`.
- Parser tests in `tests/test_cost_tracker.py`:
  - Multi-step fixture (2+ `step_finish`) — sums across events.
  - `reasoning` tokens counted into `output_tokens`.
  - Malformed lines / stderr noise skipped; empty log → zero usage;
    non-`step_finish` events ignored.
- Entry-point discovery: extend the existing pattern in
  `tests/test_spawner_registry.py` so `opencode` is asserted discovered.

### 5. Documentation

`CLAUDE.md` spawners bullet: drop "exists but is not yet registered as a
selectable agent type"; list the five spawners.

## Error handling / edge behavior (already implemented, not touched)

- opencode CLI not in PATH → `is_available() == False`; the scheduler
  already handles unavailable spawners.
- No catalog entry for the `opencode` harness and no
  `MAESTRO_OPENCODE_MODEL` → `HarnessModelUnresolved` → task goes to
  `NEEDS_REVIEW` (existing `resolve_model` fail-loud path).

## Out of scope

- Cost-from-log (`part.cost`) — follow-up ticket.
- Arbiter policy tree changes (arbiter-side config).
- Mode 2 (orchestrate) routing.
- Catalog data entry for opencode — lives in `$ATP_CATALOG` (ATP repo);
  `MAESTRO_OPENCODE_MODEL` is the env fallback meanwhile.
- Example YAML additions.
