# cost-from-log + effective-harness dispatch — design

**Date:** 2026-07-05
**Status:** approved
**Context:** Follow-ups #1 and #3 from the opencode wiring (PR #42, squash
`ae72a8b`), recorded in TODO.md "opencode follow-ups (ADR-ECO-003c)". opencode
reports its own per-step `part.cost` in the `run --format json` stream; today
Maestro drops it, so opencode tasks report `cost_usd=None` to the arbiter and
show $0.00 in REST/dashboard summaries. Separately, cost parsing is keyed off
the DECLARED `task.agent_type`, so routed tasks (`agent_type: auto` →
`opencode@glm-5.1`) never reach `parse_opencode_log` at all.

## Goal

1. Surface opencode's self-reported cost into `TaskCost`, `TaskOutcome`, and
   cost summaries — real dollars instead of PRICING-based 0.
2. Dispatch the log parser by the EFFECTIVE harness (routed wins), closing the
   routed-path token-telemetry gap.
3. Honor the recorded constraint: cache reads must NOT be billed at full input
   price — satisfied **by construction**: Maestro never computes opencode cost
   from tokens; it takes opencode's own `part.cost`, which already prices
   cache correctly.

Scope: reported cost from **opencode only** this iteration. The carrying
mechanism (`TokenUsage.cost_usd` → `TaskCost.reported_cost_usd`) is generic;
claude_code (`total_cost_usd` in Claude CLI JSON) is a later iteration.

## Changes

### 1. Parser: extract `part.cost`

- `TokenUsage` (dataclass in `maestro/cost_tracker.py`) gains
  `cost_usd: float | None = None`. None means "agent did not report cost" —
  never collapse to 0.0.
- `parse_opencode_log` sums `part.cost` across `step_finish` events.
  - Per-step semantics for cost follow the same fixture argument as tokens:
    in `tests/fixtures/opencode_run.jsonl` step 2's cost (0.00359536) is
    LESS than step 1's (0.0170512) — impossible for a cumulative counter, so
    per-step summing is correct.
  - A `step_finish` without a numeric `cost` (missing / null / non-numeric)
    contributes nothing. If NO step reported a numeric cost, the total is
    None (unknown), not 0.0.
  - "Numeric" guard: `isinstance(v, (int, float)) and not isinstance(v,
    bool)` — in Python `bool` IS an `int` subclass and `float(True) == 1.0`,
    so JSON `"cost": true` would otherwise leak in as $1.00. Test required.
  - Token extraction, drift canary, and malformed-line skipping are
    unchanged.
- claude_code / codex / aider parsers are untouched; their `TokenUsage`
  carries `cost_usd=None`.

### 2. `TaskCost` + database migration

- `TaskCost` (maestro/models.py) gains
  `reported_cost_usd: float | None = Field(default=None, ge=0)` — the
  agent's own cost report. `estimated_cost_usd` keeps its meaning
  (PRICING-based estimate; 0.0 for unpriced harnesses) — estimate and ground
  truth stay separate columns so the source of every number is inspectable.
- Migration #4 in `maestro/database.py`, following the LABS-85 journal
  pattern (one line in `ordered` + one method):
  `ALTER TABLE task_costs ADD COLUMN reported_cost_usd REAL`, idempotent via
  `PRAGMA table_info` (same shape as `_migrate_tasks_arbiter_columns`). The
  `CREATE TABLE` DDL for fresh databases gains the column too.
- `save_task_cost` INSERT and the row→model mapping in `get_task_costs` /
  `get_all_task_costs` include the new column.
- `create_task_cost` fills `reported_cost_usd` from `usage.cost_usd`.
- `parse_and_create_cost` gate relaxes: return None only when tokens are
  both zero AND `usage.cost_usd is None` (a cost-only row is still a row).

### 3. Effective-harness dispatch (routed-path gap)

In `Scheduler._record_cost` (maestro/scheduler.py), compute the effective
harness before parsing:

- `harness = harness_of_agent_id(task.routed_agent_type) if
  task.routed_agent_type else task.agent_type.value`
- If `harness` is an `AgentType` value → use that enum member for BOTH the
  parser dispatch and the `TaskCost.agent_type` row field. The row then
  truthfully records the agent that actually ran, which also makes the
  per-row `has_pricing` check in `_build_outcome` operate on the right
  harness.
- If `harness` is NOT an `AgentType` member (D2 custom spawner, e.g. a
  plugin harness) → fall back to the declared `task.agent_type` exactly as
  today: no parser match → empty usage → no row. No behavior change for
  that path.
- `AgentType.AUTO` can only appear as declared-and-routed (AUTO never
  spawns), so the routed branch always covers it.

### 4. Outcome semantics (`Scheduler._build_outcome`)

Per-row effective cost:

```
effective(row) = row.reported_cost_usd        if not None
               = row.estimated_cost_usd       if has_pricing(row.agent_type)
               = unknown                      otherwise
```

`cost_usd = sum(effective(r))` when EVERY matching row is known; None if any
row is unknown.

Attempt scoping (review clarification): `matching` is filtered by
`r.attempt == attempt` (scheduler.py:369) — the set covers ONE attempt, so an
unknown-cost attempt never zeroes out a different attempt's known cost; each
outcome reports its own attempt. The any-unknown→None rule is therefore a
defensive guard for the theoretical multiple-rows-per-attempt case, not a
practical cross-attempt interaction.

Consequences:

- Routed/declared opencode with `part.cost` in the log → the arbiter gets
  real dollars.
- opencode without a reported cost → None (today's honest-unknown, kept).
- announce → honest 0.0 via PRICING, unchanged.
- `tokens_used` reporting is unchanged.

### 5. Summaries (REST API / dashboard / CLI)

- `Database.get_cost_summary` SQL: `SUM(COALESCE(reported_cost_usd,
  estimated_cost_usd))` (and the same for any per-agent grouping in that
  query).
- `build_summary` (cost_tracker.py) Python equivalent: prefer
  `reported_cost_usd` when not None, else `estimated_cost_usd`.
- Summaries stay non-nullable floats: an unpriced+unreported row contributes
  0.0 there, as today. The None-vs-0 distinction is enforced only at the
  arbiter-outcome boundary (§4), where routing decisions are made.

### 6. Benchmark path

`spawner_responder.py`: after `parse_log`, prefer `usage.cost_usd` over
`calculate_cost(usage, agent_enum)`; the existing `cost or None` coercion
stays as the final guard.

### 7. Tests

- Parser: real-fixture literal for summed cost — computed with jq
  independently of the parser: 0.0170512 + 0.00359536 = **0.02064656**
  (assert with `pytest.approx`). Edge cases: no cost in any step → None;
  explicit `"cost": null` → skipped; `"cost": true` → ignored (bool is not
  numeric); cost present in one step only → partial sum with that value;
  tokens still parsed when cost absent.
- Migration: a database created with the pre-#4 schema gains the column on
  connect; `schema_migrations` gets the (4, name) row; fresh DBs work; both
  paths round-trip a `TaskCost` with `reported_cost_usd` set and None.
- `_record_cost` dispatch: routed `auto` → `opencode@glm-5.1` parses the
  opencode JSONL log and persists a row with `agent_type=OPENCODE` and
  `reported_cost_usd` filled; declared claude_code overridden to opencode
  behaves the same; a routed non-enum harness (`fakeharness@x`) falls back
  to declared dispatch (no row from an opencode-format log).
- `_build_outcome`: opencode row WITH reported cost → `cost_usd` equals the
  reported sum; WITHOUT → None (regression of PR #42 behavior); mixed rows
  where one is unknown → None (closes the deferred minor from PR #42's
  final review); announce → 0.0 (regression).
- Summary: `get_cost_summary` and `build_summary` prefer reported over
  estimated (row with estimated=0.0, reported=0.02 → summary shows 0.02).
- Responder: reported cost preferred over PRICING-computed value.

### 8. Documentation

- TODO.md: tick follow-ups #1 (cost-from-log) and #3 (routed-path token
  telemetry) with the commit hash; leave #2 (SSOT catalog entry) open.
- CLAUDE.md spawners bullet: replace "Its cost is reported to the arbiter
  as unknown (`cost_usd=None`, not 0.0) until cost-from-log lands" with
  "Its cost comes from opencode's own per-step `part.cost`
  (`reported_cost_usd`); unknown (`cost_usd=None`, never 0.0) when the log
  reports none".

## Error handling / edges

- Non-numeric `cost` values (bool/str) are ignored per event — same
  lossy-but-logged philosophy as token parsing; no crash paths outside
  `parse_log`'s existing blanket except.
- Old DB rows have `reported_cost_usd IS NULL` → COALESCE falls back to the
  estimate everywhere; no backfill needed.
- A cost-only row (zero tokens, reported cost) makes the outcome carry
  `tokens_used=0` rather than None. Accepted: opencode in practice always
  reports tokens alongside cost, so this occurs only for hypothetical
  log shapes; not worth a special case.

## Known limitation: `TaskCost.agent_type` semantics change at the migration
boundary

For routed tasks the column's meaning changes from "declared agent_type" to
"effective harness" — WITHOUT backfill, so the column is heterogeneous
across the boundary: pre-migration routed rows carry the declared type,
post-migration rows carry who actually ran. Reader audit (grep performed
2026-07-05, before implementation):

- `scheduler.py:372` `has_pricing(r.agent_type)` in `_build_outcome` —
  WANTS effective (that is the point of the change); old heterogeneous rows
  only affect outcomes of already-terminal tasks, which are never rebuilt.
- `database.py:1544` — INSERT (writer, not a reader).
- `coordination/rest_api.py:136` — per-row display serialization; a
  pre-migration routed row shows the declared type. Cosmetic.
- No `GROUP BY agent_type` exists anywhere (database.py, rest_api.py), and
  `CostSummary`/`build_summary` do not read `agent_type` at all — there are
  NO per-agent historical aggregates to contaminate.

Recorded as a known limitation, accepted without backfill.

## Out of scope

- Reported cost for claude_code / codex (mechanism is ready; separate
  iteration).
- Mode 2 (orchestrate) cost tracking.
- SSOT catalog entry for opencode (follow-up #2, cross-repo).
- Backfilling historical task_costs rows.
