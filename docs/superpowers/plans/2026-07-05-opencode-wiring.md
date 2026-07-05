# opencode Spawner Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register the existing `maestro/spawners/opencode.py` as a first-class selectable agent type (enum, entry point, CLI default set) with tokens-only cost parsing and router-honest `cost_usd=None` reporting.

**Architecture:** Purely additive wiring around the already-landed `OpencodeSpawner`. Five registration points (enum / `spawners/__init__` / pyproject entry point / CLI default dict / regenerated JSON schema), a new JSONL parser in `cost_tracker.py`, and one guarded change in `Scheduler._build_outcome` so unpriced harnesses report cost as *unknown* (None), never 0.0.

**Tech Stack:** Python 3.12+, uv, pytest (anyio for async), pyrefly, ruff. Spec: `docs/superpowers/specs/2026-07-05-opencode-wiring-design.md`.

## Global Constraints

- Package management: `uv` only, never pip. Tests: `uv run pytest`. Types: `uv run pyrefly check`. Lint: `uv run ruff format .` + `uv run ruff check .`.
- Line length 88 chars; type hints everywhere; docstrings on public APIs.
- Async tests use `@pytest.mark.anyio`, not asyncio.
- `AgentType.AUTO` must remain the LAST enum member (routing sentinel).
- opencode is deliberately absent from `PRICING` — absence is the "unpriced harness" marker. Do not add a `(0.0, 0.0)` entry.
- The enum value is the bare `"opencode"` (no `_cli` suffix) — it is the tool's real CLI name and the catalog harness id (ADR-ECO-003c).
- The full test suite (~1333 tests) must pass at every commit.

---

### Task 1: Branch setup + capture a real opencode JSONL fixture

The parser's aggregation strategy (sum per-step vs take-last-cumulative) depends on what real `step_finish` events contain. This task captures ground truth BEFORE any parser code exists. opencode 1.17.5 is installed at `/opt/homebrew/bin/opencode`.

**Files:**
- Create: `tests/fixtures/opencode_run.jsonl`

**Interfaces:**
- Produces: `tests/fixtures/opencode_run.jsonl` — raw stdout of a real multi-step `opencode run --format json` invocation. Task 3's tests load it via `Path(__file__).parent / "fixtures" / "opencode_run.jsonl"`.
- Produces: a verified answer to "are `step_finish` tokens per-step or cumulative?" recorded in the fixture-capture commit message and used by Task 3.

- [ ] **Step 1: Create the feature branch; return local master to origin**

Local master carries two spec/plan doc commits that must ride the PR branch instead:

```bash
git checkout -b feat/opencode-wiring
git branch -f master origin/master
git log --oneline -3   # expect: plan/spec commits on feat/opencode-wiring
```

- [ ] **Step 2: Run a real multi-step opencode invocation**

Use a scratch dir with two files and a prompt that forces tool use (→ multiple steps):

```bash
SCRATCH=$(mktemp -d)
cd "$SCRATCH"
printf 'alpha\n' > a.txt
printf 'beta\n' > b.txt
opencode run --format json \
  "Read the files a.txt and b.txt with your read tool, then reply with their combined contents." \
  > opencode_run.jsonl 2>opencode_run.stderr
wc -l opencode_run.jsonl
```

Expected: exit 0, several JSONL lines. If opencode fails (auth/model config), STOP and ask the user — the spec mandates a real fixture, do not substitute a synthetic one.

- [ ] **Step 3: Verify multi-step + decide per-step vs cumulative**

```bash
jq -c 'select(.type=="step_finish") | .part.tokens' opencode_run.jsonl
```

Expected: ≥2 `step_finish` lines. Decide the semantics:
- If each event's `input`/`output` reflects only that step (e.g. later events do NOT contain the earlier events' totals folded in), tokens are **per-step** → the parser SUMS across events (spec's primary assumption).
- If each event's numbers equal the running total of all prior steps (last event ≈ sum of the others plus itself, monotonically increasing snapshots), they are **cumulative** → the parser takes the LAST `step_finish` only, and you must also fix the spec section "2. Cost tracker" accordingly.
- If ambiguous from the data, re-run with a 3-step prompt and/or check opencode's docs/source (`opencode run --format json` event semantics; opencode is built on the Vercel AI SDK whose `step_finish` is per-step). Record the verdict.

If the run produced only ONE `step_finish`, re-run Step 2 with a prompt requiring more tool calls (e.g. "read a.txt, then write its reversed content to c.txt, then read c.txt") until the fixture has ≥2.

- [ ] **Step 4: Install the fixture and record expected totals**

```bash
cp opencode_run.jsonl \
  /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/Maestro/tests/fixtures/opencode_run.jsonl
# Expected totals for Task 3's test literals (per-step verdict → sums):
jq -s '[ .[] | select(.type=="step_finish") | .part.tokens ]
       | {input: (map(.input) | add),
          output_plus_reasoning: (map(.output + (.reasoning // 0)) | add),
          steps: length}' \
  /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/Maestro/tests/fixtures/opencode_run.jsonl
```

Write down `input`, `output_plus_reasoning`, and `steps` — Task 3 hardcodes them as assertion literals (computed here by jq, independently of the Python parser, so the test is not tautological).

- [ ] **Step 5: Commit**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/Maestro
git add tests/fixtures/opencode_run.jsonl
git commit -m "test: capture real opencode run --format json fixture

step_finish verdict: <PER-STEP | CUMULATIVE> (verified via jq over the
captured stream). Expected totals: input=<N>, output+reasoning=<M>, steps=<K>."
```

---

### Task 2: `AgentType.OPENCODE` + D2 proof-test fix + schema regen

**Files:**
- Modify: `maestro/models.py:72-86` (AgentType enum)
- Modify: `tests/test_models.py:251-255` (enum value-set test)
- Modify: `tests/test_scheduler.py:1690-1703` (D2 proof test)
- Modify: `maestro/schemas/project_config.json` (regenerated, not hand-edited)

**Interfaces:**
- Produces: `AgentType.OPENCODE` with `.value == "opencode"` — used by Tasks 3, 4, 5.

- [ ] **Step 1: Update the enum value-set test to expect opencode (failing test)**

In `tests/test_models.py`, `TestAgentType.test_all_agent_types_exist`:

```python
    def test_all_agent_types_exist(self) -> None:
        """Verify all expected agent types are defined."""
        expected = {"claude_code", "codex_cli", "aider", "announce", "opencode", "auto"}
        actual = {a.value for a in AgentType}
        assert actual == expected
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_models.py::TestAgentType::test_all_agent_types_exist -v`
Expected: FAIL — actual set is missing `"opencode"`.

- [ ] **Step 3: Fix the D2 proof test BEFORE adding the enum member**

`tests/test_scheduler.py:1690-1703` currently uses `"opencode"` as its example of a harness outside the enum; adding `OPENCODE` would silently falsify that premise. Replace:

```python
    @pytest.mark.anyio
    async def test_non_enum_harness_with_spawner_spawns(
        self, temp_db_path: Path, temp_dir: Path
    ) -> None:
        """PROOF OF D2: 'fakeharness' is not an AgentType member, but has a spawner.

        (Originally used 'opencode' as the example; opencode became a real
        AgentType member when it was wired in, so the proof harness must stay
        a name that never joins the enum.)
        """
        spawner = MockSpawner("fakeharness")
        db, _ = await self._run(
            temp_db_path, temp_dir, {"fakeharness": spawner}, "fakeharness@some-model"
        )
        try:
            assert spawner.spawn_count == 1  # previously HOLD via ValueError
            assert spawner.spawned_models == ["some-model"]
        finally:
            await db.close()
```

- [ ] **Step 4: Add the enum member**

In `maestro/models.py`, inside `AgentType`, after `ANNOUNCE` and before `AUTO`:

```python
    ANNOUNCE = "announce"
    OPENCODE = "opencode"
    """Bare name on purpose (vs codex_cli / claude_code): it is the tool's
    real CLI name and the catalog harness id (ADR-ECO-003c). Do not suffix."""
    AUTO = "auto"
```

(Keep the existing `AUTO` docstring below it untouched.)

- [ ] **Step 5: Regenerate the JSON schema**

```bash
uv run python -m maestro.schemas.generate
git diff --stat maestro/schemas/
```

Expected: `project_config.json` gains `"opencode"` in the AgentType enum arrays. No hand edits.

- [ ] **Step 6: Run the affected tests, then the full suite**

Run: `uv run pytest tests/test_models.py tests/test_scheduler.py -q && uv run pytest -q`
Expected: all pass (the D2 test now proves the gate with `fakeharness`).

- [ ] **Step 7: Typecheck + lint + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/models.py maestro/schemas/project_config.json tests/test_models.py tests/test_scheduler.py
git commit -m "feat(models): AgentType.OPENCODE; D2 proof test uses fakeharness

The D2 gate test used 'opencode' as its example of a non-enum harness;
registering opencode falsifies that premise, so the proof example moves
to a name that never joins the enum."
```

---

### Task 3: `parse_opencode_log` + `has_pricing` in cost_tracker

**Files:**
- Modify: `maestro/cost_tracker.py` (new parser after `_extract_usage_from_dict`; `has_pricing` next to `PRICING`; parsers map in `parse_log`)
- Test: `tests/test_cost_tracker.py` (new `TestOpencodeLogParsing` class after `TestClaudeCodeLogParsing`; extend `TestParseLog` and `TestCostCalculation`)

**Interfaces:**
- Consumes: `AgentType.OPENCODE` (Task 2); `tests/fixtures/opencode_run.jsonl` (Task 1).
- Produces: `parse_opencode_log(log_content: str) -> TokenUsage`; `has_pricing(agent_type: AgentType) -> bool` — Task 4 imports `has_pricing` in `maestro/scheduler.py`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cost_tracker.py`. Replace `EXPECTED_INPUT` / `EXPECTED_OUTPUT` / `EXPECTED_STEPS` with the jq-derived literals from Task 1 Step 4. If Task 1's verdict was CUMULATIVE, the first test's expected values are the LAST event's numbers instead — adjust to the verdict.

```python
class TestOpencodeLogParsing:
    """Tests for opencode `run --format json` JSONL parsing."""

    FIXTURE = Path(__file__).parent / "fixtures" / "opencode_run.jsonl"

    def test_parse_real_fixture_sums_step_finish(self) -> None:
        """Real captured run: per-step tokens summed across step_finish events.

        Expected literals were computed with jq over the fixture (independent
        of this parser), so this is not a tautology. Guards the per-step (not
        cumulative) aggregation verdict from the fixture-capture task.
        """
        usage = parse_opencode_log(self.FIXTURE.read_text(encoding="utf-8"))
        assert usage.input_tokens == EXPECTED_INPUT
        assert usage.output_tokens == EXPECTED_OUTPUT

    def test_reasoning_counted_into_output(self) -> None:
        log = (
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 10, "output": 5, "reasoning": 7}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.input_tokens == 10
        assert usage.output_tokens == 12

    def test_multiple_steps_summed(self) -> None:
        log = (
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 100, "output": 20, "reasoning": 0}}}\n'
            '{"type": "tool_use", "part": {"name": "read"}}\n'
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 150, "output": 30, "reasoning": 5}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.input_tokens == 250
        assert usage.output_tokens == 55

    def test_malformed_lines_skipped(self) -> None:
        log = (
            "stderr noise: model warming up\n"
            "{not json at all\n"
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 10, "output": 5}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.input_tokens == 10
        assert usage.output_tokens == 5

    def test_missing_tokens_fields_default_zero(self) -> None:
        log = '{"type": "step_finish", "part": {"tokens": {"output": 3}}}\n'
        usage = parse_opencode_log(log)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 3

    def test_non_step_finish_events_ignored(self) -> None:
        log = (
            '{"type": "step_start", "part": {}}\n'
            '{"type": "error", "part": {"tokens": {"input": 999, "output": 999}}}\n'
        )
        assert parse_opencode_log(log) == TokenUsage()

    def test_empty_log(self) -> None:
        assert parse_opencode_log("") == TokenUsage()

    def test_nonempty_log_without_step_finish_logs_drift_canary(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Format-drift canary: non-empty log, zero step_finish → debug log,
        so a silent event rename in opencode doesn't quietly zero tracking."""
        import logging

        log = '{"type": "step_done", "part": {"tokens": {"input": 5}}}\n'
        with caplog.at_level(logging.DEBUG, logger="maestro.cost_tracker"):
            usage = parse_opencode_log(log)
        assert usage == TokenUsage()
        assert "no step_finish" in caplog.text

    def test_step_finish_without_part_tokens_skipped(self) -> None:
        log = (
            '{"type": "step_finish"}\n'
            '{"type": "step_finish", "part": {}}\n'
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 1, "output": 2}}}\n'
        )
        usage = parse_opencode_log(log)
        assert usage.input_tokens == 1
        assert usage.output_tokens == 2


class TestHasPricing:
    """PRICING membership = "this harness has a priced rate card"."""

    def test_opencode_is_unpriced(self) -> None:
        assert has_pricing(AgentType.OPENCODE) is False

    def test_announce_zero_is_an_honest_price(self) -> None:
        assert has_pricing(AgentType.ANNOUNCE) is True

    def test_priced_harnesses(self) -> None:
        assert has_pricing(AgentType.CLAUDE_CODE) is True
        assert has_pricing(AgentType.CODEX) is True
        assert has_pricing(AgentType.AIDER) is True
```

Extend `TestParseLog` (dispatch) and `TestCostCalculation` (unpriced → 0.0):

```python
    def test_parse_log_opencode(self) -> None:
        """parse_log dispatches OPENCODE to the JSONL parser."""
        log = (
            '{"type": "step_finish", "part": {"tokens": '
            '{"input": 10, "output": 5, "reasoning": 1}}}\n'
        )
        usage = parse_log(log, AgentType.OPENCODE)
        assert usage.input_tokens == 10
        assert usage.output_tokens == 6

    def test_calculate_cost_opencode_unpriced_is_zero(self) -> None:
        """No PRICING entry → calculate_cost falls back to 0.0 (TaskCost rows
        keep recording 0.0); outcome reporting turns that into None upstream."""
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        assert calculate_cost(usage, AgentType.OPENCODE) == 0.0
```

Update imports at the top of the test file to include `parse_opencode_log` and `has_pricing` (extend the existing `from maestro.cost_tracker import ...`) and add `from pathlib import Path` if not already imported.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cost_tracker.py -q`
Expected: ImportError — `parse_opencode_log` / `has_pricing` do not exist.

- [ ] **Step 3: Implement**

In `maestro/cost_tracker.py`, directly below the `PRICING` dict (note: NO opencode entry is added to `PRICING`):

```python
# opencode is deliberately absent: it is an open-model harness whose price
# depends on the routed model, so Maestro cannot price it from a static
# per-harness table. Absence from PRICING == "unpriced" (see has_pricing);
# outcome reporting turns the resulting 0.0 into cost_usd=None so
# cost-aware routing reads it as *unknown*, never as *free*.


def has_pricing(agent_type: AgentType) -> bool:
    """True if the harness has a rate card in PRICING.

    announce's (0.0, 0.0) is an honest zero (it runs no model); a harness
    absent from PRICING (opencode) has UNKNOWN cost — callers reporting
    cost to the arbiter must send None, not 0.0.
    """
    return agent_type.value in PRICING
```

After `_extract_usage_from_dict`, the parser:

```python
def parse_opencode_log(log_content: str) -> TokenUsage:
    """Parse opencode ``run --format json`` JSONL output for token usage.

    opencode emits one JSON event per line; ``step_finish`` events carry
    per-step usage in ``part.tokens`` (verified against a captured real run:
    values are per-step increments, so they are summed across events).

    ``part.tokens.cache_read`` / ``cache_write`` and ``part.cost`` are
    intentionally dropped (tokens-only, spec variant A). The cost-from-log
    follow-up must NOT bill cache_read at full input price — in real runs
    cache_read is on the order of input itself.

    Args:
        log_content: Raw log file content (stderr shares the fd, so
            non-JSON noise lines are expected and skipped).

    Returns:
        TokenUsage with input and output (+ reasoning) token sums.
    """
    usage = TokenUsage()
    saw_step_finish = False
    for raw_line in log_content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "step_finish":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            continue
        saw_step_finish = True
        usage.input_tokens += int(tokens.get("input", 0))
        usage.output_tokens += int(tokens.get("output", 0)) + int(
            tokens.get("reasoning", 0)
        )
    if log_content.strip() and not saw_step_finish:
        # Format-drift canary: opencode renaming/removing step_finish would
        # otherwise zero out token tracking with no signal at all.
        logger.debug("opencode log had no step_finish events — format drift?")
    return usage
```

(If Task 1's verdict was CUMULATIVE: instead of `+=`, overwrite `usage` fields on every `step_finish` so the last event wins, keep the same skip/canary logic, and update the docstring + spec section 2 to match.)

In `parse_log`, extend the parsers map:

```python
    parsers = {
        AgentType.CLAUDE_CODE: parse_claude_code_log,
        AgentType.CODEX: parse_claude_code_log,
        AgentType.AIDER: parse_claude_code_log,
        AgentType.OPENCODE: parse_opencode_log,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cost_tracker.py -q`
Expected: PASS, including the real-fixture literals.

- [ ] **Step 5: Typecheck + lint + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/cost_tracker.py tests/test_cost_tracker.py
git commit -m "feat(cost): parse_opencode_log (JSONL step_finish) + has_pricing

Tokens-only (spec variant A): cache_read/cache_write and part.cost are
intentionally dropped; drift canary logs when a non-empty log has no
step_finish. opencode stays OUT of PRICING — absence marks the harness
as unpriced so outcome reporting can send cost=None (unknown), not 0."
```

---

### Task 4: Router honesty — `_build_outcome` reports `cost_usd=None` for unpriced harnesses

**Files:**
- Modify: `maestro/scheduler.py:364-372` (`_build_outcome` cost aggregation)
- Test: `tests/test_scheduler_cost_recording.py`

**Interfaces:**
- Consumes: `has_pricing` from `maestro.cost_tracker` (Task 3); `AgentType.OPENCODE` (Task 2).
- Produces: `TaskOutcome.cost_usd is None` for opencode tasks (arbiter client omits None from the payload — already implemented at `arbiter_client.py:382`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_scheduler_cost_recording.py` (module already imports `Database`, `DAG`, `Scheduler`, `SchedulerConfig`, `AgentType`, `Task`, `TaskStatus`):

```python
@pytest.mark.anyio
async def test_build_outcome_unpriced_harness_reports_cost_none(tmp_path) -> None:
    """opencode (no PRICING entry): cost 0.0 would read as 'free' to
    cost-aware routing (R-07 'route cheapest sufficient'), so _build_outcome
    must report cost_usd=None (unknown) while still reporting real tokens."""
    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.OPENCODE,
            status=TaskStatus.DONE,
        )
        await db.create_task(task)

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )

        from maestro.models import TaskCost

        await db.save_task_cost(
            TaskCost(
                task_id="t1",
                agent_type=AgentType.OPENCODE,
                input_tokens=250,
                output_tokens=55,
                estimated_cost_usd=0.0,  # unpriced harness records 0.0
                attempt=1,
            )
        )

        outcome = await scheduler._build_outcome(task, exit_code=0)
        assert outcome.tokens_used == 305  # tokens are real and reported
        assert outcome.cost_usd is None  # cost is UNKNOWN, not free
    finally:
        await db.close()


@pytest.mark.anyio
async def test_build_outcome_announce_zero_cost_stays_zero(tmp_path) -> None:
    """announce IS in PRICING at (0.0, 0.0) — an honest zero, not unknown."""
    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.ANNOUNCE,
            status=TaskStatus.DONE,
        )
        await db.create_task(task)

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )

        from maestro.models import TaskCost

        await db.save_task_cost(
            TaskCost(
                task_id="t1",
                agent_type=AgentType.ANNOUNCE,
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0.0,
                attempt=1,
            )
        )

        outcome = await scheduler._build_outcome(task, exit_code=0)
        assert outcome.cost_usd == 0.0
    finally:
        await db.close()
```

- [ ] **Step 2: Run to verify the first fails**

Run: `uv run pytest tests/test_scheduler_cost_recording.py -v`
Expected: `test_build_outcome_unpriced_harness_reports_cost_none` FAILS (`cost_usd == 0.0`); the announce test passes (current behavior).

- [ ] **Step 3: Implement the guard**

In `maestro/scheduler.py`, `_build_outcome` (currently lines 369-372), change:

```python
        matching = [r for r in rows if r.attempt == attempt]
        if matching:
            tokens_used = sum(r.input_tokens + r.output_tokens for r in matching)
            if all(has_pricing(r.agent_type) for r in matching):
                cost_usd = sum(r.estimated_cost_usd for r in matching)
            # else: an unpriced harness (opencode) is in the mix — its cost
            # is UNKNOWN, not 0.0. cost_usd stays None; the arbiter client
            # omits None from the payload, so cost-aware routing reads
            # "unknown" instead of "free".
```

Add `has_pricing` to the existing `from maestro.cost_tracker import ...` line in `maestro/scheduler.py` (grep for `cost_tracker` in the imports; extend, don't duplicate).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_scheduler_cost_recording.py -v`
Expected: all pass, including the three pre-existing tests (priced-path behavior unchanged).

- [ ] **Step 5: Full suite + typecheck + lint + commit**

```bash
uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/scheduler.py tests/test_scheduler_cost_recording.py
git commit -m "feat(scheduler): unpriced harness reports cost_usd=None to arbiter

Real tokens + cost 0.0 would win every cost tiebreaker in cost-aware
routing — 0 must read as unknown, not cheapest. announce keeps its
honest 0.0 (it IS priced, at zero)."
```

---

### Task 5: `TestOpencodeSpawner` + fixture-catalog entry

The spawner code already exists; these are characterization tests locking its contract (command shape, `_qualify`, model precedence). They need an opencode entry in the TEST fixture catalog (the real SSOT lives in atp-platform — out of scope).

**Files:**
- Modify: `tests/fixtures/agents-catalog.toml` (add glm-5.1 model + opencode agent)
- Test: `tests/test_spawners.py` (new fixture + `TestOpencodeSpawner` after `TestCodexSpawner`)

**Interfaces:**
- Consumes: `OpencodeSpawner` from `maestro.spawners.opencode` (already on master); `catalog_env` fixture (`tests/conftest.py:217`).
- Produces: locked spawner contract: argv `["opencode", "run", "--format", "json", "-m", "opencode/<model>", <prompt>]`.

- [ ] **Step 1: Add opencode to the fixture catalog**

Append to `tests/fixtures/agents-catalog.toml`:

```toml
[models."glm-5.1"]
vendor = "zai"
status = "active"

[[agents]]
harness  = "opencode"
model    = "glm-5.1"
tested   = true
routable = true
```

(Safe: `tests/test_catalog.py` asserts only claude_code/codex_cli defaults and that *aider* raises; the sibling-SSOT check pins only those two harnesses.)

- [ ] **Step 2: Write the spawner tests**

In `tests/test_spawners.py`: extend the import at the top —

```python
from maestro.spawners.opencode import OpencodeSpawner, _qualify
```

(NOT from the `maestro.spawners` package — its `__init__` export arrives in Task 6; the direct module import keeps this task independently green.)

Add after `TestCodexSpawner` (which ends before `TestAnnounceSpawner` / the next section):

```python
@pytest.fixture
def opencode_spawner() -> OpencodeSpawner:
    """Provide an opencode spawner instance."""
    return OpencodeSpawner()


class TestQualify:
    """_qualify: bare model ids get opencode's provider prefix."""

    def test_bare_id_gets_prefix(self) -> None:
        assert _qualify("glm-5.1") == "opencode/glm-5.1"

    def test_provider_qualified_id_passes_through(self) -> None:
        assert _qualify("zai/glm-5.1") == "zai/glm-5.1"


class TestOpencodeSpawner:
    """Tests for OpencodeSpawner."""

    def test_agent_type(self, opencode_spawner: OpencodeSpawner) -> None:
        """Test that agent_type returns correct value."""
        assert opencode_spawner.agent_type == "opencode"

    def test_opencode_available_when_in_path(
        self,
        opencode_spawner: OpencodeSpawner,
    ) -> None:
        """Test is_available returns True when opencode is in PATH."""
        with patch(
            "maestro.spawners.opencode.shutil.which",
            return_value="/opt/homebrew/bin/opencode",
        ):
            assert opencode_spawner.is_available() is True

    def test_opencode_unavailable_when_not_in_path(
        self,
        opencode_spawner: OpencodeSpawner,
    ) -> None:
        """Test is_available returns False when opencode is not in PATH."""
        with patch(
            "maestro.spawners.opencode.shutil.which",
            return_value=None,
        ):
            assert opencode_spawner.is_available() is False

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_creates_process_with_correct_args(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        opencode_spawner: OpencodeSpawner,
        sample_task: Task,
        temp_dir: Path,
        catalog_env: Path,
    ) -> None:
        """Catalog default resolves and is provider-qualified on the argv."""
        mock_process = MagicMock()
        mock_popen.return_value = mock_process
        mock_os_open.return_value = 42

        log_file = temp_dir / "task.log"
        workdir = Path(sample_task.workdir)

        result = opencode_spawner.spawn(sample_task, "ctx", workdir, log_file)

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]

        assert cmd[0] == "opencode"
        assert cmd[1] == "run"
        assert "--format" in cmd
        assert cmd[cmd.index("--format") + 1] == "json"
        # Fixture catalog default glm-5.1, prefixed for the CLI:
        assert cmd[cmd.index("-m") + 1] == "opencode/glm-5.1"
        assert call_args[1]["cwd"] == workdir
        assert call_args[1]["stdout"] == 42
        assert call_args[1]["stderr"] == subprocess.STDOUT
        mock_os_close.assert_called_once_with(42)
        assert result == mock_process

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_model_override_from_env(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        opencode_spawner: OpencodeSpawner,
        sample_task: Task,
        temp_dir: Path,
        catalog_env: Path,
    ) -> None:
        """MAESTRO_OPENCODE_MODEL overrides the catalog default."""
        mock_popen.return_value = MagicMock()
        mock_os_open.return_value = 42
        workdir = Path(sample_task.workdir)

        with patch.dict(os.environ, {"MAESTRO_OPENCODE_MODEL": "qwen3.6"}):
            opencode_spawner.spawn(sample_task, "", workdir, temp_dir / "t.log")

        cmd = mock_popen.call_args[0][0]
        assert cmd[cmd.index("-m") + 1] == "opencode/qwen3.6"

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_routed_model_beats_env(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        opencode_spawner: OpencodeSpawner,
        sample_task: Task,
        temp_dir: Path,
        catalog_env: Path,
    ) -> None:
        """Precedence routed > env: routed model overrides MAESTRO_OPENCODE_MODEL."""
        mock_popen.return_value = MagicMock()
        mock_os_open.return_value = 42
        workdir = Path(sample_task.workdir)

        with patch.dict(os.environ, {"MAESTRO_OPENCODE_MODEL": "env-model"}):
            opencode_spawner.spawn(
                sample_task, "", workdir, temp_dir / "t.log", model="glm-5.1"
            )

        cmd = mock_popen.call_args[0][0]
        assert cmd[cmd.index("-m") + 1] == "opencode/glm-5.1"

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_provider_qualified_routed_model_not_double_prefixed(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        opencode_spawner: OpencodeSpawner,
        sample_task: Task,
        temp_dir: Path,
        catalog_env: Path,
    ) -> None:
        """A routed 'provider/model' id passes through _qualify unchanged."""
        mock_popen.return_value = MagicMock()
        mock_os_open.return_value = 42
        workdir = Path(sample_task.workdir)

        opencode_spawner.spawn(
            sample_task, "", workdir, temp_dir / "t.log", model="zai/glm-5.1"
        )

        cmd = mock_popen.call_args[0][0]
        assert cmd[cmd.index("-m") + 1] == "zai/glm-5.1"
```

Note on env-override tests: `resolve_model` reads `MAESTRO_OPENCODE_MODEL` before consulting the catalog, and `load_catalog()` requires `$ATP_CATALOG` — the `catalog_env` fixture provides it (spawn calls `load_catalog()` unconditionally).

- [ ] **Step 3: Run the new tests**

Run: `uv run pytest tests/test_spawners.py -q && uv run pytest tests/test_catalog.py -q`
Expected: PASS (spawner code already exists — these are characterization tests; test_catalog still green with the extended fixture).

- [ ] **Step 4: Typecheck + lint + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add tests/fixtures/agents-catalog.toml tests/test_spawners.py
git commit -m "test(spawners): lock OpencodeSpawner contract; opencode in fixture catalog

Command shape (opencode run --format json -m opencode/<model>),
_qualify prefixing, precedence routed > MAESTRO_OPENCODE_MODEL > catalog."
```

---

### Task 6: Registration — entry point, package export, CLI default set

**Files:**
- Modify: `pyproject.toml:44-48` (entry points)
- Modify: `maestro/spawners/__init__.py`
- Modify: `maestro/cli.py:29,56,395-404`
- Test: `tests/test_spawner_registry.py` (extend `test_discover_from_directory_finds_all_spawners`; new installed-entry-points test)

**Interfaces:**
- Consumes: `OpencodeSpawner` (existing module), Task 5's tests stay green.
- Produces: `from maestro.spawners import OpencodeSpawner`; entry point `opencode`; `"opencode"` key in `maestro run`'s spawner dict.

- [ ] **Step 1: Write the failing tests**

In `tests/test_spawner_registry.py` — extend the existing directory-discovery test (`test_discover_from_directory_finds_all_spawners`, line 452):

```python
    def test_discover_from_directory_finds_all_spawners(
        self,
        registry: SpawnerRegistry,
    ) -> None:
        """Test that directory discovery finds all built-in spawners."""
        count = registry.discover_from_directory()

        assert count >= 5
        assert "claude_code" in registry
        assert "codex_cli" in registry
        assert "aider" in registry
        assert "announce" in registry
        assert "opencode" in registry
        assert isinstance(registry.get_spawner("codex_cli"), CodexSpawner)
        assert isinstance(registry.get_spawner("aider"), AiderSpawner)
        assert isinstance(registry.get_spawner("announce"), AnnounceSpawner)
        assert isinstance(registry.get_spawner("opencode"), OpencodeSpawner)
```

Add `OpencodeSpawner` to this test file's imports **from the package** (this is the failing part):

```python
from maestro.spawners import (
    ...existing names...,
    OpencodeSpawner,
)
```

Add a new test to `TestEntryPointDiscovery` asserting the INSTALLED dist metadata (not a mock) registers all five built-ins:

```python
    def test_installed_entry_points_include_all_builtins(self) -> None:
        """The installed distribution registers all five built-in spawners
        under the maestro.spawners entry-point group (real metadata, no mock).
        Guards the pyproject wiring — the cli.py default dict and this group
        are dual registration sources that must not diverge."""
        eps = importlib.metadata.entry_points(group="maestro.spawners")
        names = {ep.name for ep in eps}
        assert {"claude_code", "codex_cli", "aider", "announce", "opencode"} <= names
```

(`import importlib.metadata` at the top of the test file if not present.)

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_spawner_registry.py -q`
Expected: ImportError on `OpencodeSpawner` from `maestro.spawners`; after fixing only the import, the entry-points test would still fail (no `opencode` entry point yet). Note: directory discovery already finds opencode today (it scans modules), so `count >= 5` passes once the import resolves — the real gate is the entry-points test.

- [ ] **Step 3: Wire the three registration points**

`maestro/spawners/__init__.py` — full new content:

```python
"""Agent spawners for different AI coding assistants."""

from maestro.spawners.aider import AiderSpawner
from maestro.spawners.announce import AnnounceSpawner
from maestro.spawners.base import AgentSpawner
from maestro.spawners.claude_code import ClaudeCodeSpawner
from maestro.spawners.codex import CodexSpawner
from maestro.spawners.opencode import OpencodeSpawner
from maestro.spawners.registry import (
    SpawnerNotFoundError,
    SpawnerRegistry,
    create_default_registry,
)


__all__ = [
    "AgentSpawner",
    "AiderSpawner",
    "AnnounceSpawner",
    "ClaudeCodeSpawner",
    "CodexSpawner",
    "OpencodeSpawner",
    "SpawnerNotFoundError",
    "SpawnerRegistry",
    "create_default_registry",
]
```

`pyproject.toml` — add to `[project.entry-points."maestro.spawners"]`:

```toml
[project.entry-points."maestro.spawners"]
claude_code = "maestro.spawners.claude_code:ClaudeCodeSpawner"
codex_cli = "maestro.spawners.codex:CodexSpawner"
aider = "maestro.spawners.aider:AiderSpawner"
announce = "maestro.spawners.announce:AnnounceSpawner"
opencode = "maestro.spawners.opencode:OpencodeSpawner"
```

`maestro/cli.py` — line 56 import gains `OpencodeSpawner`:

```python
from maestro.spawners import AiderSpawner, AnnounceSpawner, CodexSpawner, OpencodeSpawner
```

(if that exceeds 88 chars, split into a parenthesized multi-line import), and the default set at lines 395-404 becomes:

```python
        # Setup spawners — all five built-ins so YAML configs with
        # agent_type: codex_cli / aider / announce / opencode work out of
        # the box, matching what examples/hello.yaml, examples/tasks.yaml,
        # and the arbiter policy tree's agent set expect.
        spawners: dict[str, AgentSpawner] = {
            "claude_code": ClaudeCodeSpawner(),
            "codex_cli": CodexSpawner(),
            "aider": AiderSpawner(),
            "announce": AnnounceSpawner(),
            "opencode": OpencodeSpawner(),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_spawner_registry.py tests/test_spawners.py tests/test_cli.py -q
```

Expected: PASS. (`uv run` re-syncs the project on pyproject change, refreshing entry-point metadata; if the entry-points test still misses `opencode`, run `uv sync` once and re-run.)

- [ ] **Step 5: Full suite + typecheck + lint + commit**

```bash
uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add pyproject.toml maestro/spawners/__init__.py maestro/cli.py tests/test_spawner_registry.py
git commit -m "feat(spawners): register opencode — entry point, package export, CLI default set

opencode joins the selectable agent set (ADR-ECO-003c): YAML
agent_type: opencode, arbiter routing opencode@<model> through the D2
gate, and entry-point discovery all resolve to OpencodeSpawner."
```

---

### Task 7: Docs, follow-up ticket, final verification

**Files:**
- Modify: `CLAUDE.md` (spawners bullet in Architecture)
- Modify: `TODO.md` (cost-from-log follow-up)

**Interfaces:**
- Consumes: everything above, merged view.
- Produces: honest docs; recorded follow-up constraint for cost-from-log.

- [ ] **Step 1: Update CLAUDE.md**

In the Architecture section, replace the spawners bullet:

```markdown
- **spawners/**: AgentSpawner ABC + implementations (claude_code, codex_cli, aider, announce, opencode) + registry. opencode (`opencode run --format json -m opencode/<model>`, ADR-ECO-003c) is the first open-model agentic harness: open models (glm-5.1, qwen3.6, …) reach routing as `opencode@<model>`. Its cost is reported to the arbiter as unknown (`cost_usd=None`, not 0.0) until cost-from-log lands
```

- [ ] **Step 2: Add the follow-up to TODO.md**

Under `## Catalog distribution follow-ups (ADR-ECO-003b)` (or a new `## opencode follow-ups` section at the same level — pick whichever reads better in context):

```markdown
## opencode follow-ups (ADR-ECO-003c)

- [ ] Cost-from-log: surface `part.cost` (and optionally cache_read/cache_write)
      from opencode JSONL into TaskCost/TaskOutcome instead of PRICING-based 0.
      Constraint (recorded in parse_opencode_log docstring): cache_read must
      NOT be billed at full input price — in real runs cache_read ~= input.
      Until then opencode reports cost_usd=None (unknown) to the arbiter.
- [ ] opencode entry in the ecosystem SSOT catalog (atp-platform/method/
      agents-catalog.toml) — cross-repo; the test fixture already carries
      harness=opencode / glm-5.1.
```

- [ ] **Step 3: Final verification — full gates**

```bash
uv run pytest -q
uv run pyrefly check
uv run ruff format . && uv run ruff check .
git status --short   # only intended files
```

Expected: suite green (1333 pre-existing + ~25 new), pyrefly clean, ruff clean.

- [ ] **Step 4: Smoke: opencode selectable end-to-end (config layer)**

```bash
uv run python -c "
from maestro.models import AgentType, Task
t = Task(id='s', title='s', prompt='p', workdir='.', agent_type='opencode')
print('enum ok:', t.agent_type is AgentType.OPENCODE)
from maestro.spawners import create_default_registry
r = create_default_registry()
print('registry ok:', 'opencode' in r)
"
```

Expected: `enum ok: True`, `registry ok: True`.

- [ ] **Step 5: Commit docs**

```bash
git add CLAUDE.md TODO.md
git commit -m "docs: opencode is a registered selectable agent type; cost-from-log follow-up"
```

- [ ] **Step 6: Push and open the PR**

```bash
git push -u origin feat/opencode-wiring
gh pr create --title "feat(spawners): wire opencode as selectable agent type (ADR-ECO-003c)" --body "$(cat <<'EOF'
## Summary
- Register the existing OpencodeSpawner at all five points: AgentType enum, spawners package export, pyproject entry point, CLI default set, regenerated JSON schema
- parse_opencode_log: tokens-only JSONL parsing (step_finish events; per-step semantics verified against a real captured run in tests/fixtures/opencode_run.jsonl)
- Router honesty: opencode stays OUT of PRICING; Scheduler._build_outcome reports cost_usd=None (unknown) instead of 0.0 so cost-aware routing never reads opencode as "free"
- D2 proof test moves its non-enum example from "opencode" to "fakeharness" (premise stays true)

Spec: docs/superpowers/specs/2026-07-05-opencode-wiring-design.md

## Test plan
- [ ] Full suite green (pre-existing + new spawner/parser/outcome tests)
- [ ] pyrefly + ruff clean
- [ ] Real-fixture parser test locks per-step aggregation

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Spec coverage: registration §1 → Tasks 2/6; parser §2 → Tasks 1/3; router honesty §2b → Task 4; D2 fix §3 → Task 2; tests §4 → Tasks 3/4/5/6; docs §5 → Task 7; "known accepted risks" (dual registration) → guarded by Task 6's installed-entry-points test docstring.
- Task 1 gates Task 3's aggregation strategy; both carry the CUMULATIVE fallback instruction so the plan survives either verdict.
- Type consistency: `has_pricing(agent_type: AgentType) -> bool` (Tasks 3→4); `parse_opencode_log(log_content: str) -> TokenUsage` (Tasks 3); fixture path literal identical in Tasks 1/3.
