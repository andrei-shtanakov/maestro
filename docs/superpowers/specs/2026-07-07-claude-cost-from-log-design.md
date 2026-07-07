# Claude cost-from-log + genuine-0.0 preservation — design

**Date:** 2026-07-07
**Status:** approved
**Context:** "benchmark deferred" bucket. The cost-from-log mechanism landed for
opencode in PR #43 (`parse_opencode_log` → `TaskCost.reported_cost_usd`); this
extends it to claude_code and fixes a `0.0 → None` collapse in the benchmark
responder. Codex is out of scope (see below).

## Problem

Two related gaps in cost reporting through the benchmark path:

1. **Claude Code's self-reported cost is discarded.** `claude --print
   --output-format json` emits a result object carrying `total_cost_usd`, but
   `parse_claude_code_log` / `_extract_usage_from_dict` extract only
   `input_tokens` / `output_tokens`. So claude cost is always the PRICING
   token-estimate, never claude's own (more accurate, cache-correct) number —
   even though opencode already flows its reported cost via `part.cost`.

2. **A genuinely-reported `0.0` cost collapses to `None` (unknown).**
   `benchmark/spawner_responder.py` computes `cost = reported if reported is not
   None else estimate`, then sends `cost_usd=cost or None`. The `or None`
   turns a genuine reported `0.0` (a free model — a real scenario for opencode's
   open models, and possible for a fully-cached claude run) into `None`,
   conflating "free" with "unknown". Downstream `_sum_or_none`
   (`benchmark/runner.py`) is already built to distinguish "reported zero" from
   "no data", so the collapse in the responder is the single point that destroys
   that signal before it can reach the aggregate.

## Change

### 1. Extract claude's reported cost (`maestro/cost_tracker.py`)

In `_extract_usage_from_dict`, after populating tokens, also read the top-level
cost (`total_cost_usd`, falling back to `cost_usd`) into `usage.cost_usd`,
reusing the opencode parser's guards (type / not-bool / `isfinite`) and adding a
`>= 0.0` floor. NOTE: `parse_opencode_log` does NOT currently apply that floor —
so this is not literally "identical" to opencode; the floor here is the safer
version, and bringing opencode to parity is a recorded follow-up (a negative
`part.cost` there would fail `TaskCost.reported_cost_usd`'s `ge=0.0` and silently
drop the row):

```python
    cost = data.get("total_cost_usd")
    if cost is None:
        cost = data.get("cost_usd")
    # bool is an int subclass (JSON true must not read as $1.00); NaN/Infinity
    # and negatives must not leak (NaN fails TaskCost's ge=0.0 check and would
    # silently drop the whole row).
    if (
        isinstance(cost, (int, float))
        and not isinstance(cost, bool)
        and math.isfinite(cost)
        and cost >= 0.0
    ):
        usage.cost_usd = float(cost)
```

The function is restructured so the token branches populate a single `usage`
object and the cost is attached in ALL of them (today two branches `return`
early with a fresh `TokenUsage` that would drop the cost). `math` is already
imported (used by the opencode parser).

**Cost-only result must survive the token gate.** `parse_claude_code_log`
currently returns a parsed usage only when `input_tokens > 0 or output_tokens >
0`, else it discards it (`return TokenUsage()`). A claude result with a
`total_cost_usd` but zero/absent tokens would be dropped, losing the cost. Fix
the gate to also pass on a reported cost:

```python
        if usage.input_tokens > 0 or usage.output_tokens > 0 or usage.cost_usd is not None:
            return usage
```

`parse_and_create_cost` already handles the cost-only case correctly (its guard
is `input_tokens == 0 and output_tokens == 0 and usage.cost_usd is None → return
None`), so no change is needed there — but it only ever sees a cost-only usage
once `parse_claude_code_log`'s gate is fixed.

Update the `TokenUsage.cost_usd` docstring, which currently says "Only the
opencode parser fills this today; claude/codex/aider logs are priced from
PRICING downstream" — now claude fills it too.

**Scope note — shared parser, not a per-agent flow.** This change extends the
shared `parse_claude_code_log` JSON parser; it does NOT add a dedicated
codex/aider cost flow. Because `parse_log` routes CODEX and AIDER through
`parse_claude_code_log` too (`cost_tracker.py`), those agents will
*opportunistically* pick up a `total_cost_usd` / `cost_usd` IF their log ever
happens to be JSON carrying one — but no per-agent parsing is added here.
Codex's current plain-text output matches nothing, so it gains nothing today
(its dedicated path is the separate follow-up ticket).

Effect: claude_code's reported cost flows into `TaskCost.reported_cost_usd` and
wins over the estimate in `effective_cost` / `build_summary` (both already
prefer `reported_cost_usd` when present).

### 2. Preserve a genuinely-reported 0.0 (`benchmark/spawner_responder.py`)

Split the reported vs estimated cases so a reported cost (including `0.0`)
survives, while an *estimated* zero (no tokens / no pricing) stays "unknown":

```python
        if usage.cost_usd is not None:
            cost_wire = usage.cost_usd  # reported — keep, including a genuine 0.0
        else:
            estimate = calculate_cost(usage, agent_enum)
            cost_wire = estimate or None  # estimated 0.0 = no observation → unknown
```

Compute `cost_wire` once and use it at both return sites (the `returncode != 0`
branch and the success branch), replacing `cost_usd=cost or None`. The
`tokens_used=total_tokens or None` collapse is left unchanged: 0 tokens is a
genuine "no observation", unlike a reported 0.0 cost.

## Accepted tradeoff: estimated 0.0 → unknown

The estimated branch keeps `estimate or None`, so an agent that is genuinely
free but does NOT report a cost is shown as `unknown` (None), not `0.0`, in
benchmark aggregates. This is deliberate: a token-priced `0.0` means either "no
tokens were observed" or "no pricing entry" — genuinely unknown, and a
free-but-unreported agent is indistinguishable at the estimate level from one
that did not run. A *reported* 0.0 (the agent's own number) IS preserved.
Surfacing free-but-unreported agents as `0.0` would require an explicit signal
that the agent ran and produced no billable work, which no current agent emits;
out of scope.

## Codex — out of scope (follow-up ticket)

`codex exec -m <model> …` writes plain text to the log (no `--output-format
json`); `parse_log` routes CODEX to `parse_claude_code_log`, which extracts
nothing from non-JSON. Codex has neither tokens nor cost in its current log
format, so cost-from-log is not actionable without first establishing whether
codex can emit structured usage/cost output. Recorded as a follow-up
(research) ticket in TODO.

## Testing

- **`_extract_usage_from_dict` / `parse_claude_code_log` cost extraction:**
  - a claude result dict with nested `usage` tokens AND top-level
    `total_cost_usd` → both tokens and `cost_usd` extracted.
  - `cost_usd` key used when `total_cost_usd` is absent.
  - guards reject and leave `cost_usd=None`: `true` (bool), `NaN`, `Infinity`,
    a negative cost — each asserted individually.
- **Cost-only result survives the gate (the reviewer's gap):** a claude JSON
  with `total_cost_usd` set but zero/absent token fields →
  `parse_claude_code_log` returns a usage with `cost_usd` set (not an empty
  `TokenUsage`), AND `parse_and_create_cost` on that log produces a `TaskCost`
  with `reported_cost_usd` set (not None).
- **`spawner_responder` 0.0 preservation:**
  - `usage.cost_usd == 0.0` (reported free) → `AgentResponse.cost_usd == 0.0`
    (NOT None), on both the success and `returncode != 0` paths.
  - `usage.cost_usd is None` with zero tokens → `cost_usd is None` (estimated
    zero stays unknown).
  - `usage.cost_usd == 1.23` (reported) → `1.23` passes through.
- **End-to-end responder → runner:** a task whose responder reports `0.0` →
  `BenchmarkResult.total_cost_usd == 0.0` (not None), proving `_sum_or_none`
  receives the genuine zero.
- Regression: existing cost_tracker / benchmark tests stay green; the opencode
  cost path is unchanged.

## Documentation

- `TokenUsage.cost_usd` docstring updated (claude now fills it).
- TODO.md: add the codex cost-from-log research follow-up.

## Out of scope

- Codex cost-from-log (follow-up).
- Changing `tokens_used` 0 → None (a genuine no-observation).
- Surfacing free-but-unreported agents as 0.0 in aggregates (needs a
  ran-with-no-billable-work signal no agent emits).
- aider (routes to `parse_claude_code_log`; would opportunistically pick up a
  `total_cost_usd`/`cost_usd` if aider ever emitted one, but no aider-specific
  work here).
