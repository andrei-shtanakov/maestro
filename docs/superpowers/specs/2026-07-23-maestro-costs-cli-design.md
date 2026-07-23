# `maestro costs` — read-side cost summary — design

- **Date:** 2026-07-23
- **Status:** approved (brainstorming; r1 corrections folded in)
- **Scope:** idea #25 from `../prograph-vault/authored/notes/2026-07-22-ideas-from-ai-repos-research.md`
  ("cost tracker on EventBus") — reduced to a **read-side, database-wide cost
  summary CLI**. The EventBus / CostEvent / event-driven-budget half is
  deliberately NOT built (no real-time consumer exists; building a bus without a
  consumer is scope for its own sake).

## 1. Goal & boundary

`maestro costs --db <path>` prints an aggregated, **read-only** cost summary
over the `task_costs` table.

**Hard boundary (load-bearing):**
- Read-only. **No** change to the write/terminal path, the transition
  dispatcher, or the event log. No EventBus, no `CostEvent`, no event-driven
  budget/alert.
- The database is the **scope of the report, not one run**. One `maestro.db`
  survives `--resume` and can hold several invocations. Help/README call the
  output a **"database-wide cost summary"**, never a "run total".
- **No "by run" grouping and no "by model" grouping** in this MVP — neither is
  provable read-only (§3). Both are deferred to a future write-path slice.

## 2. Cost rule — single source of truth

The known/unknown decision reuses the existing authoritative rule
`cost_tracker.effective_cost(row)`:

```
reported_cost_usd (if not None)  ->  priced estimate (if has_pricing)  ->  None
```

- **Known** cost of a row ⟺ `effective_cost(row) is not None`.
- **Unknown** ⟺ `None` — an unpriced harness (`opencode`, absent from `PRICING`)
  with no self-reported cost. `announce` is `(0.0, 0.0)` in `PRICING` → an
  **honest zero**, i.e. *known*, never *unknown*. The read side MUST NOT treat
  `estimated_cost_usd = 0.0` for an unpriced harness as known-free — that is
  exactly the collapsing bug `effective_cost` avoids.

The MVP aggregates in **Python** over `TaskCost` rows using `effective_cost` /
`has_pricing`, so the pricing rule is not duplicated in SQL.

**Tokens** are summed over **all stored `TaskCost` rows, including rows whose
dollar cost is unknown** — tokens and dollars are independent. (We cannot claim
tokens are known for every *attempt*: if a parser extracted neither tokens nor a
reported cost, no `TaskCost` row is created for that attempt at all — it is
simply absent from the accounting.)

**The aggregator returns, per group:**

- `known_cost_usd` — Σ `effective_cost(row)` over rows where it is not None.
- `input_tokens`, `output_tokens` — Σ over all rows in the group.
- `tasks` — distinct `task_id` with ≥1 stored cost row.
- `attempts` — number of cost rows.
- `unknown_attempts` — rows with `effective_cost(row) is None`.
- `unknown_tasks` — distinct `task_id` with ≥1 unknown row.

`known_cost_usd` is a **known subtotal**, never labelled `total_cost_usd` when
`unknown_attempts > 0`.

## 3. Grouping dimensions — and why "by model" is out

`task_costs` columns: `id, task_id, agent_type, input_tokens, output_tokens,
estimated_cost_usd, reported_cost_usd, attempt, created_at`.

- **`agent_type` (harness) is authoritative per attempt** — it is stored on each
  cost row. → **by harness** grouping is sound.
- **There is no model / agent_id on `task_costs`.** The only model source is
  `tasks.routed_agent_type` (a single value on the *current* task row), which:
  - is `NULL`ed on retry (`reset_for_retry_atomic` does
    `SET routed_agent_type = NULL, arbiter_* = NULL`);
  - reflects the **latest** routing, not the routing of each historical attempt;
  - is one value shared by all of a task's attempt rows.
  Joining `task_costs → tasks.routed_agent_type` therefore yields the *current*
  routed model, not the model that produced each cost row. **A historically
  correct "by effective model" grouping is unprovable read-only**, so it is
  **omitted**. We do not show `tasks.routed_agent_type` under the name "effective
  model" — that would be a misleading label.

Authoritative per-attempt model grouping requires stamping `agent_id`/`model_id`
into `task_costs` **at write time** (a future schema migration + write-path
slice, alongside or separate from the `run_id` work). Out of scope here.

**Groupings in this MVP:** TOTAL (database-wide), **by harness**, **by task**.

## 4. CLI — `maestro costs --db maestro.db`

Rich tables. Every aggregation shows the mixed-known columns so a partially
known task is never misrepresented:

```
KNOWN COST | TOKENS (in/out) | TASKS | ATTEMPTS | UNKNOWN ATTEMPTS | UNKNOWN TASKS
```

- **TOTAL** (database-wide) — one summary row.
- **By harness** (`agent_type`) — one row per harness.
- **By task** (`task_id`) — one row per task; a task with two attempts
  ($0.20 known + 1 unknown) reads as **`$0.20 known · 1 unknown attempt`**, not
  as an exact `$0.20` and not as only-unknown.

Definitions (as in §2): `tasks` = distinct task_ids with ≥1 stored cost row;
`attempts` = cost-row count; `unknown_attempts` = rows with
`effective_cost is None`; `unknown_tasks` = distinct tasks with ≥1 unknown row;
retries are fully counted in tokens and the known subtotal.

- Empty DB (no cost rows) → exit `0`, a clear **"No cost records."** message /
  zeroed tables.
- Exit codes: `0` success (incl. empty); `2` invalid input (unreadable DB).
- `--json` is **deferred** — this MVP is explicitly human-only output.
- The command uses the DB path option (`--db`, default `DEFAULT_DB_PATH`),
  matching `maestro status` / `maestro workstreams` conventions.

## 5. Non-goals & the COALESCE follow-up

Out of scope:
- "By run" grouping + a persistent `run_id`/`pipeline_id` (schema migration +
  write-path).
- "By model" grouping + per-attempt `agent_id`/`model_id` on `task_costs`.
- Dashboard / REST surface; event-driven budget/alert; EventBus / `CostEvent`.

**The legacy COALESCE landmine is a separate, wider follow-up — not this PR.**
`database.get_cost_summary()` sums `COALESCE(reported_cost_usd,
estimated_cost_usd)`, collapsing an unpriced-unreported row to `$0` (known-free)
— the exact bug this MVP avoids. But the REST `/costs/summary` path does **not**
call `get_cost_summary()`; it calls `cost_tracker.build_summary()`, which repeats
the same collapsing semantics. So "fix `get_cost_summary` too" would not fix the
whole problem. The real fix revises the shared summary/REST **contract** to carry
unknown counts. Tracked as a follow-up: **"Unify legacy DB and REST cost
summaries on unknown-aware aggregation."** This PR touches neither; the new CLI
uses only the new, correct aggregator.

## 6. Testing

**Aggregator (pure, over `TaskCost` lists + harness):**
- Rule: `announce` (0.0,0.0) → known-$0 (not unknown); `opencode` unpriced +
  `reported=None` → unknown; a priced harness → known priced-estimate; a row
  with `reported_cost_usd` set → known-reported.
- Read side does **not** use `estimated_cost_usd` for an unpriced harness
  (opencode's stored `estimated=0.0` must not be summed as known).
- One task, one **known** + one **unknown** attempt → `known_cost_usd` preserved
  (the known part), `unknown_attempts = 1`, `unknown_tasks = 1`, and the unknown
  row still contributes its tokens.
- Two tasks, same harness → `tasks = 2`, `attempts` = actual row count.
- Retry with a **different** harness across attempts → each attempt lands in its
  own harness group.
- A task with **no** `TaskCost` row is not counted.
- Mixed known/unknown: `known_cost_usd` is never labelled `total`.

**CLI:**
- exit `0` with populated tables on a seeded DB; the mixed-known row renders
  "$X known · N unknown".
- empty DB → exit `0`, "No cost records."
- unreadable DB → exit `2`.
- **Documented-boundary test:** the output has **no** "by model" and **no**
  "by run" table (guards against a future regression re-introducing the
  unprovable groupings).

## 7. Architecture summary

- `maestro/cost_tracker.py`: add a pure aggregator, e.g.
  `summarize_costs(costs: list[TaskCost]) -> CostReport` producing the TOTAL +
  per-harness + per-task breakdown with the §2 fields (reusing `effective_cost`).
- `maestro/database.py`: a read-only fetch of all cost rows (reuse
  `get_all_costs()`; no model join needed since "by model" is out).
- `maestro/cli.py`: a `costs` command that fetches rows, calls the aggregator,
  renders the Rich tables.
- No writes, no schema change, no touch to scheduler/orchestrator/event-log.
