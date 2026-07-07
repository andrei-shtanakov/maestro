# Claude cost-from-log + genuine-0.0 preservation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract Claude Code's self-reported `total_cost_usd` from its JSON log into `TaskCost.reported_cost_usd`, and stop the benchmark responder from collapsing a genuinely-reported `0.0` cost into `None`.

**Architecture:** Two localized changes: (1) `cost_tracker.py` — `_extract_usage_from_dict` also reads the top-level cost (guarded like the opencode parser), and `parse_claude_code_log`'s return-gate passes on a reported cost even with zero tokens; (2) `benchmark/spawner_responder.py` — split reported (preserve, incl. 0.0) vs estimated (0.0 → None) so a genuine free cost reaches the aggregate (`_sum_or_none` already distinguishes reported-0.0 from no-data).

**Tech Stack:** Python 3.12+, uv, pytest (anyio), pyrefly, ruff. Spec: `docs/superpowers/specs/2026-07-07-claude-cost-from-log-design.md`.

## Global Constraints

- `uv` only; `uv run pytest` / `uv run pyrefly check` / `uv run ruff format .` + `uv run ruff check .`; line length 88; async tests `@pytest.mark.anyio`; run pytest in the FOREGROUND.
- Cost-extraction guards (reuse the opencode parser's type/not-bool/isfinite checks, plus a `>= 0.0` floor opencode's parser does NOT currently apply): accept only `isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0.0`; otherwise leave `cost_usd = None`. (`bool` is an int subclass; NaN/Infinity/negative must not leak. The floor here is the safer version; opencode parity is a follow-up ticket.)
- Read `total_cost_usd` first, fall back to `cost_usd`.
- `parse_claude_code_log` must return a usage carrying a reported cost even when tokens are 0 (`... or usage.cost_usd is not None`). `parse_and_create_cost` is ALREADY cost-aware — do not change it.
- Responder: a REPORTED cost (`usage.cost_usd is not None`) is preserved verbatim, including `0.0`. Only an ESTIMATED cost collapses `0.0 → None`. Leave `tokens_used = total_tokens or None` unchanged.
- This extends the SHARED `parse_claude_code_log` parser (CODEX/AIDER route through it too) — no per-agent codex/aider flow is added. Codex cost-from-log is a separate follow-up ticket.
- `math` is already imported in `cost_tracker.py`.
- Branch: `feat/claude-cost-from-log` (exists, spec committed). Full suite green at every commit.

---

### Task 1: Extract claude's reported cost (`cost_tracker.py`)

**Files:**
- Modify: `maestro/cost_tracker.py` (`_extract_usage_from_dict`, `parse_claude_code_log` gate, `TokenUsage.cost_usd` docstring)
- Test: `tests/test_cost_tracker.py`

**Interfaces:**
- Produces: `_extract_usage_from_dict(data)` now sets `usage.cost_usd` from `total_cost_usd`/`cost_usd`; `parse_claude_code_log` returns a cost-carrying usage even at zero tokens.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cost_tracker.py` (import `parse_claude_code_log`,
`parse_and_create_cost`, `AgentType` as the file already does):

```python
def test_claude_log_extracts_total_cost_usd() -> None:
    content = json.dumps(
        {"usage": {"input_tokens": 100, "output_tokens": 50},
         "total_cost_usd": 0.0123}
    )
    usage = parse_claude_code_log(content)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cost_usd == pytest.approx(0.0123)


def test_claude_log_cost_usd_key_fallback() -> None:
    content = json.dumps(
        {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.5}
    )
    usage = parse_claude_code_log(content)
    assert usage.cost_usd == pytest.approx(0.5)


def test_claude_log_rejects_bad_cost_values() -> None:
    for bad in ("true", "NaN", "Infinity", "-1.0"):
        content = (
            '{"usage": {"input_tokens": 10, "output_tokens": 5}, '
            f'"total_cost_usd": {bad}}}'
        )
        usage = parse_claude_code_log(content)
        assert usage.cost_usd is None, f"cost {bad} must be rejected"
        assert usage.input_tokens == 10  # tokens still parsed


def test_claude_log_cost_only_survives_zero_tokens() -> None:
    # A result with a cost but no token fields must not be dropped.
    content = json.dumps({"total_cost_usd": 0.02})
    usage = parse_claude_code_log(content)
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cost_usd == pytest.approx(0.02)


def test_parse_and_create_cost_from_cost_only_log(tmp_path) -> None:
    log = tmp_path / "c.log"
    log.write_text(json.dumps({"total_cost_usd": 0.02}))
    tc = parse_and_create_cost("t1", AgentType.CLAUDE_CODE, log)
    assert tc is not None
    assert tc.reported_cost_usd == pytest.approx(0.02)
```

(`json` and `pytest` are already imported in this test file; confirm and add if
not.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cost_tracker.py -k "cost" -q`
Expected: FAIL — `cost_usd` is never populated for claude, and the cost-only
log returns an empty usage.

- [ ] **Step 3: Refactor `_extract_usage_from_dict` to attach cost in all branches**

Replace the body of `_extract_usage_from_dict` (maestro/cost_tracker.py:141):

```python
def _extract_usage_from_dict(data: object) -> TokenUsage:
    """Extract token usage and reported cost from a parsed JSON dict.

    Handles multiple token formats:
    - Top-level: {"input_tokens": N, "output_tokens": N}
    - Nested usage: {"usage": {"input_tokens": N, "output_tokens": N}}
    - Result format: {"result": ..., "usage": {...}}
    Cost (Claude's ``total_cost_usd``, or ``cost_usd``) is read from the
    top level of the object and attached regardless of the token format.
    """
    if not isinstance(data, dict):
        return TokenUsage()

    usage = TokenUsage()

    if "input_tokens" in data and "output_tokens" in data:
        usage.input_tokens = int(data["input_tokens"])
        usage.output_tokens = int(data["output_tokens"])
    else:
        nested = data.get("usage")
        if isinstance(nested, dict):
            usage.input_tokens = int(nested.get("input_tokens", 0))
            usage.output_tokens = int(nested.get("output_tokens", 0))

    # Claude's result JSON carries the cost at the top level.
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

    return usage
```

- [ ] **Step 4: Fix the `parse_claude_code_log` return-gate**

In `parse_claude_code_log` (maestro/cost_tracker.py:91), change the loop's
accept condition to also pass on a reported cost:

```python
    for parser in (_parse_json_object, _parse_last_json_line):
        usage = parser(log_content)
        if (
            usage.input_tokens > 0
            or usage.output_tokens > 0
            or usage.cost_usd is not None
        ):
            return usage

    return TokenUsage()
```

- [ ] **Step 5: Update the `TokenUsage.cost_usd` docstring**

Replace the last sentence of the `TokenUsage.cost_usd` docstring
(maestro/cost_tracker.py:78) so it no longer claims only opencode fills it:

```python
    cost_usd: float | None = None
    """Agent-reported cost in USD (e.g. opencode's per-step ``part.cost``,
    Claude Code's result ``total_cost_usd``).

    None means the agent did not report a cost — never collapse to 0.0.
    The opencode and claude parsers fill this; codex/aider are priced from
    PRICING downstream unless their log happens to be JSON carrying a cost
    (they share ``parse_claude_code_log``).
    """
```

- [ ] **Step 6: Run tests + gates**

Run: `uv run pytest tests/test_cost_tracker.py -q`
Then: `uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS (new + existing cost_tracker tests — the token-only claude tests
still pass since cost defaults None); clean.

- [ ] **Step 7: Commit**

```bash
git add maestro/cost_tracker.py tests/test_cost_tracker.py
git commit -m "feat(cost): extract Claude Code total_cost_usd from log into reported cost"
```

---

### Task 2: Preserve a genuinely-reported 0.0 (`spawner_responder.py`)

**Files:**
- Modify: `maestro/benchmark/spawner_responder.py` (the cost computation + two return sites)
- Test: `tests/test_spawner_responder.py`

**Interfaces:**
- Consumes: `usage.cost_usd` from `parse_log` (Task 1 / existing opencode path).
- Produces: `AgentResponse.cost_usd` preserves a reported `0.0`; an estimated `0.0` stays `None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_spawner_responder.py` (reuse the `FakeSpawner` /
`FakeProcess` harness already in the file). A reported `0.0` is produced by an
opencode-format log whose `part.cost` is `0.0`:

```python
@pytest.mark.anyio
async def test_reported_zero_cost_is_preserved(tmp_path) -> None:
    # opencode step_finish with an explicit cost of 0.0 (a free model).
    log = json.dumps(
        {"type": "step_finish",
         "part": {"cost": 0.0, "tokens": {"input": 10, "output": 5}}}
    )
    responder = SpawnerResponder(
        spawner=FakeSpawner(agent_type_str="opencode", log_content=log),
        log_dir=tmp_path,
    )
    response = await responder.respond("free run")
    assert response.cost_usd == 0.0  # reported 0.0, NOT collapsed to None


@pytest.mark.anyio
async def test_estimated_zero_cost_stays_unknown(tmp_path) -> None:
    # No parseable usage → no reported cost, zero tokens → estimate 0.0 → None.
    responder = SpawnerResponder(
        spawner=FakeSpawner(agent_type_str="claude_code", log_content="{}"),
        log_dir=tmp_path,
    )
    response = await responder.respond("no usage")
    assert response.cost_usd is None
```

(Match the exact `SpawnerResponder(...)` constructor kwargs used by the file's
existing tests — e.g. `test_reported_cost_preferred_over_pricing` — including
any `timeout` arg they pass. `test_spawner_responder.py` does NOT import `json`
yet — add `import json` at the top.)

- [ ] **Step 2: Run to verify the first fails**

Run: `uv run pytest tests/test_spawner_responder.py -k "reported_zero or estimated_zero" -q`
Expected: `test_reported_zero_cost_is_preserved` FAILS (current `cost or None`
turns 0.0 into None); `test_estimated_zero_cost_stays_unknown` passes already.

- [ ] **Step 3: Split reported vs estimated**

In `respond` (maestro/benchmark/spawner_responder.py:~108), replace the cost
computation:

```python
        # Agent-reported cost wins over the PRICING estimate; the trailing
        # `cost or None` guards below keep 0.0 out of the wire format.
        cost = (
            usage.cost_usd
            if usage.cost_usd is not None
            else calculate_cost(usage, agent_enum)
        )
```

with:

```python
        # A reported cost (opencode's part.cost, claude's total_cost_usd) wins
        # over the PRICING estimate and is preserved verbatim — including a
        # genuine 0.0 (a free model). Only an *estimated* zero (no tokens / no
        # pricing) collapses to None ("unknown"), since it is not an observation.
        if usage.cost_usd is not None:
            cost_wire = usage.cost_usd
        else:
            estimate = calculate_cost(usage, agent_enum)
            cost_wire = estimate or None
```

Then change BOTH return sites — the `process.returncode != 0` branch and the
success branch — from `cost_usd=cost or None` to `cost_usd=cost_wire`.

- [ ] **Step 4: Run tests + gates**

Run: `uv run pytest tests/test_spawner_responder.py -q`
Then: `uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS; full suite green (existing responder tests — reported-cost,
timeout, nonzero-exit — still hold); clean.

- [ ] **Step 5: End-to-end responder → runner (reported 0.0 reaches the aggregate)**

Add to `tests/test_benchmark_runner.py`, reusing its existing stub-responder
harness (a fake `AgentResponder` whose `respond` returns a chosen
`AgentResponse`). Return `AgentResponse(text="ok", cost_usd=0.0)` for one task
and assert the run's aggregate keeps the zero:

```python
@pytest.mark.anyio
async def test_reported_zero_cost_reaches_total(...) -> None:
    # ... build a runner with a stub responder returning cost_usd=0.0 ...
    result = await runner.run(...)
    assert result.total_cost_usd == 0.0  # _sum_or_none keeps the reported zero
```

(Fill the `...` from the file's existing runner-test setup — the stub
responder, benchmark client/tasks, and `runner.run(...)` call it already uses.
If the file's harness makes a focused single-task run awkward, a
`_sum_or_none([0.0]) == 0.0` unit assertion in `tests/test_benchmark_runner.py`
is an acceptable lighter substitute; note which you did.)

- [ ] **Step 6: Run + gates**

Run: `uv run pytest tests/test_benchmark_runner.py tests/test_spawner_responder.py -q`
Then: `uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS; clean.

- [ ] **Step 7: Commit**

```bash
git add maestro/benchmark/spawner_responder.py tests/test_spawner_responder.py tests/test_benchmark_runner.py
git commit -m "fix(benchmark): preserve a genuinely-reported 0.0 cost through the responder"
```

---

### Task 3: Codex follow-up ticket, final gates, PR

**Files:**
- Modify: `TODO.md`

- [ ] **Step 1: TODO.md — codex cost-from-log research follow-up**

Add:

```markdown
- [ ] Codex cost-from-log (research): `codex exec` writes plain text (no
      `--output-format json`); `parse_log` routes CODEX through the Claude JSON
      parser, which extracts nothing. Investigate whether codex can emit
      structured usage/cost (tokens + cost) and, if so, add a dedicated codex
      parser + `parse_log` route. (Deferred from the claude cost-from-log spec.)
```

- [ ] **Step 2: Final gates**

```bash
uv run pytest -q
uv run pyrefly check
uv run ruff format . && uv run ruff check .
```

Expected: full suite green; pyrefly 0; ruff clean.

- [ ] **Step 3: Commit docs**

```bash
git add TODO.md
git commit -m "docs: track codex cost-from-log research follow-up"
```

- [ ] **Step 4: Push and open the PR** (controller may defer until after the final review)

```bash
git push -u origin feat/claude-cost-from-log
gh pr create --title "feat(cost): Claude cost-from-log + preserve genuine 0.0 (benchmark)" --body "$(cat <<'EOF'
## Summary
- Extract Claude Code's self-reported `total_cost_usd` (fallback `cost_usd`) from its `--output-format json` log into `TaskCost.reported_cost_usd`, so claude cost is its own number (cache-correct) instead of the PRICING token-estimate — matching how opencode already flows `part.cost`
- The cost is read from the top level of the result object and attached in every token branch of `_extract_usage_from_dict`, with the opencode parser's exact guards (rejects `true`/NaN/Infinity/negative); `parse_claude_code_log`'s return-gate now passes on a reported cost even with zero tokens (a cost-only result is no longer dropped)
- Fix a `0.0 → None` collapse in `benchmark/spawner_responder.py`: a genuinely-*reported* cost (including a free `0.0`) is preserved verbatim; only an *estimated* zero (no tokens / no pricing) stays `None` (unknown). Downstream `_sum_or_none` already distinguishes reported-zero from no-data, so this was the single collapse point
- Extends the shared `parse_claude_code_log` parser — no per-agent codex/aider flow added. Codex (plain-text output, no cost) is a separate research follow-up (in TODO)

## Accepted tradeoff
An agent that is genuinely free but does NOT report a cost shows as `unknown` (None), not `0.0`, in aggregates — an estimated zero is treated as "no observation", not "free". A *reported* 0.0 is preserved.

## Test plan
- [ ] Full suite green; pyrefly + ruff clean
- [ ] claude log → `total_cost_usd`/`cost_usd` extracted; guards reject bool/NaN/Infinity/negative; cost-only log (zero tokens) survives the gate and yields a `TaskCost`
- [ ] responder preserves a reported 0.0 (not None) on success and error paths; estimated zero stays None; reported >0 passes through
- [ ] end-to-end: a responder-reported 0.0 reaches `BenchmarkResult.total_cost_usd == 0.0`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Spec coverage: claude cost extraction + guards → Task 1 Steps 3/1; cost-only gate fix → Task 1 Step 4 + test; docstring → Task 1 Step 5; reported-vs-estimated split (preserve 0.0) → Task 2 Step 3; end-to-end 0.0 → Task 2 Step 5; codex follow-up + tradeoff doc → Task 3 / PR body.
- Type consistency: `usage.cost_usd: float | None`; `cost_wire: float | None`; guards identical to the opencode parser. Consistent across tasks.
- `parse_and_create_cost` and `_sum_or_none` are already correct (spec says so) — no task touches them; Task 1/2 tests exercise them to prove the end-to-end path.
- Three tasks: Task 1 (parser, cost_tracker tests), Task 2 (responder + runner e2e), Task 3 (docs+PR) — each an independent reviewer gate.
