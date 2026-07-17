# SpecRunnerConfig Passthrough Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Proxy `claude_model`/`review_command`/`review_model` as typed `SpecRunnerConfig` fields, and add a generic `extra_executor_config` deep-merge escape hatch, so Maestro-run spec-runner invocations can use a different model/CLI for review and reach any other `ExecutorConfig` field without Maestro mirroring it field-by-field.

**Architecture:** Two additive changes to `maestro/models.py`'s `SpecRunnerConfig` class and its `to_executor_config()` method, backed by a new pure `_deep_merge()` module function. No other module changes — the JSON Schema is regenerated (not hand-edited) at the end.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, ruff, pyrefly, uv.

## Global Constraints

- Type hints required for all code; run `uv run pyrefly check` after each change.
- Line length: 88 chars max.
- `uv run ruff format .` and `uv run ruff check .` must pass before each commit.
- New fields' defaults (`""` for the three model fields, `None` for `extra_executor_config`) must be behavior-preserving: `to_executor_config()` output for a default-constructed `SpecRunnerConfig()` must gain exactly the three new always-`""` keys and nothing else changes.
- `_deep_merge` must not mutate either input dict (spec requirement, confirmed in review).
- Follow existing test conventions in `tests/test_spec_runner.py::TestSpecRunnerConfigContract` (plain `assert`, no fixtures needed for this class).
- Spec: `docs/superpowers/specs/2026-07-17-specrunnerconfig-passthrough-design.md`.

---

## File Structure

- Modify: `maestro/models.py` — add `_deep_merge()` module function (placed directly above `class SpecRunnerConfig`), add 3 fields + `extra_executor_config` field to `SpecRunnerConfig`, update `to_executor_config()`.
- Modify: `tests/test_spec_runner.py` — extend `TestSpecRunnerConfigContract` with new test methods.
- Modify: `maestro/schemas/orchestrator_config.json` — regenerated output, not hand-edited.

---

### Task 1: `_deep_merge()` helper

**Files:**
- Modify: `maestro/models.py` (add function above `class SpecRunnerConfig`, currently at `maestro/models.py:1152`)
- Test: `tests/test_spec_runner.py`

**Interfaces:**
- Produces: `_deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]` — module-level function in `maestro/models.py`. Recurses only where both `base[key]` and `override[key]` are `dict`; otherwise `override`'s value wins outright. Returns a new dict; never mutates `base` or `override` (including nested dicts it doesn't touch — those are shared by reference only where safe, i.e. this can do a shallow copy at each level since it never mutates in place).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_spec_runner.py`, near the top of `TestSpecRunnerConfigContract` (or as a new class `TestDeepMerge` right above it — use a new class since this tests a standalone helper, not the config contract):

```python
class TestDeepMerge:
    """`_deep_merge` is the merge primitive behind `extra_executor_config`."""

    def test_override_wins_on_scalar_conflict(self) -> None:
        from maestro.models import _deep_merge

        result = _deep_merge({"a": 1, "b": 2}, {"b": 3})
        assert result == {"a": 1, "b": 3}

    def test_recurses_into_nested_dicts(self) -> None:
        from maestro.models import _deep_merge

        base = {"executor": {"hooks": {"post_done": {"run_tests": True, "run_lint": True}}}}
        override = {"executor": {"hooks": {"post_done": {"lint_blocking": True}}}}
        result = _deep_merge(base, override)
        assert result == {
            "executor": {
                "hooks": {
                    "post_done": {
                        "run_tests": True,
                        "run_lint": True,
                        "lint_blocking": True,
                    },
                },
            },
        }

    def test_does_not_mutate_inputs(self) -> None:
        from maestro.models import _deep_merge

        base = {"executor": {"hooks": {"post_done": {"run_tests": True}}}}
        override = {"executor": {"hooks": {"post_done": {"lint_blocking": True}}}}
        base_copy = {"executor": {"hooks": {"post_done": {"run_tests": True}}}}
        override_copy = {"executor": {"hooks": {"post_done": {"lint_blocking": True}}}}

        _deep_merge(base, override)

        assert base == base_copy
        assert override == override_copy

    def test_dict_override_replaces_non_dict_base(self) -> None:
        from maestro.models import _deep_merge

        result = _deep_merge({"a": 1}, {"a": {"nested": True}})
        assert result == {"a": {"nested": True}}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_spec_runner.py::TestDeepMerge -v`
Expected: FAIL with `ImportError: cannot import name '_deep_merge'`

- [ ] **Step 3: Implement `_deep_merge`**

Add to `maestro/models.py` directly above `class SpecRunnerConfig(BaseModel):` (currently `maestro/models.py:1152`):

```python
def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base``, without mutating either.

    Keys present in both are merged recursively when both values are dicts;
    otherwise ``override``'s value replaces ``base``'s outright.
    """
    result = dict(base)
    for key, override_value in override.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(override_value, dict):
            result[key] = _deep_merge(base_value, override_value)
        else:
            result[key] = override_value
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_spec_runner.py::TestDeepMerge -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Type check**

Run: `uv run pyrefly check`
Expected: no new errors

- [ ] **Step 6: Commit**

```bash
git add maestro/models.py tests/test_spec_runner.py
git commit -m "feat: add pure _deep_merge helper for executor-config overlays"
```

---

### Task 2: Typed `claude_model`/`review_command`/`review_model` fields

**Files:**
- Modify: `maestro/models.py:1152-1208` (`SpecRunnerConfig` class and `to_executor_config()`)
- Test: `tests/test_spec_runner.py`

**Interfaces:**
- Consumes: nothing new from Task 1.
- Produces: `SpecRunnerConfig.claude_model: str`, `SpecRunnerConfig.review_command: str`, `SpecRunnerConfig.review_model: str` (all default `""`), reflected in `to_executor_config()["executor"]["claude_model"|"review_command"|"review_model"]`.

- [ ] **Step 1: Write the failing tests**

Add to `TestSpecRunnerConfigContract` in `tests/test_spec_runner.py`:

```python
    def test_model_fields_default_empty(self) -> None:
        from maestro.models import SpecRunnerConfig

        executor = SpecRunnerConfig().to_executor_config()["executor"]
        assert executor["claude_model"] == ""
        assert executor["review_command"] == ""
        assert executor["review_model"] == ""

    def test_model_fields_pass_through_explicit_values(self) -> None:
        from maestro.models import SpecRunnerConfig

        cfg = SpecRunnerConfig(
            claude_model="claude-opus-4-8",
            review_command="codex",
            review_model="claude-sonnet-5",
        )
        executor = cfg.to_executor_config()["executor"]
        assert executor["claude_model"] == "claude-opus-4-8"
        assert executor["review_command"] == "codex"
        assert executor["review_model"] == "claude-sonnet-5"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_spec_runner.py::TestSpecRunnerConfigContract -v`
Expected: FAIL with `KeyError: 'claude_model'`

- [ ] **Step 3: Add the fields and wire them into `to_executor_config()`**

In `maestro/models.py`, inside `class SpecRunnerConfig(BaseModel):`, add after the existing `spec_gen_budget_usd` field (currently ending at line 1182):

```python
    claude_model: str = Field(
        default="", description="Claude model for tasks (empty = CLI default)"
    )
    review_command: str = Field(
        default="", description="Review CLI command (empty = claude_command)"
    )
    review_model: str = Field(
        default="", description="Review model (empty = claude_model)"
    )
```

Then in `to_executor_config()`, add the three keys to the `"executor"` dict, right after `"auto_commit": self.auto_commit,`:

```python
                "auto_commit": self.auto_commit,
                "claude_model": self.claude_model,
                "review_command": self.review_command,
                "review_model": self.review_model,
                "spec_prefix": SPEC_PREFIX,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_spec_runner.py::TestSpecRunnerConfigContract -v`
Expected: PASS

- [ ] **Step 5: Run the existing contract test to confirm no regression**

Run: `uv run pytest tests/test_spec_runner.py::TestSpecRunnerConfigContract::test_top_level_shape -v`
Expected: PASS (this test only asserts required keys are present via `assert key in executor`, so adding new keys doesn't break it)

- [ ] **Step 6: Type check**

Run: `uv run pyrefly check`
Expected: no new errors

- [ ] **Step 7: Commit**

```bash
git add maestro/models.py tests/test_spec_runner.py
git commit -m "feat: proxy claude_model/review_command/review_model to spec-runner"
```

---

### Task 3: `extra_executor_config` escape hatch

**Files:**
- Modify: `maestro/models.py` (`SpecRunnerConfig` class and `to_executor_config()`)
- Test: `tests/test_spec_runner.py`

**Interfaces:**
- Consumes: `_deep_merge` from Task 1.
- Produces: `SpecRunnerConfig.extra_executor_config: dict[str, Any] | None` (default `None`); when set, `to_executor_config()`'s return value is deep-merged with it (extra wins).

- [ ] **Step 1: Write the failing tests**

Add to `TestSpecRunnerConfigContract` in `tests/test_spec_runner.py`:

```python
    def test_extra_executor_config_merges_nested_without_dropping_siblings(self) -> None:
        from maestro.models import SpecRunnerConfig

        cfg = SpecRunnerConfig(
            extra_executor_config={
                "executor": {"hooks": {"post_done": {"lint_blocking": True}}},
            },
        )
        post_done = cfg.to_executor_config()["executor"]["hooks"]["post_done"]
        assert post_done["lint_blocking"] is True
        assert post_done["run_tests"] is True  # sibling key survives the merge
        assert post_done["run_lint"] is True

    def test_extra_executor_config_can_add_top_level_key(self) -> None:
        """Merge-mechanics check: the overlay isn't confined to `executor`.

        `metadata` is not a section spec-runner processes — this only proves
        `_deep_merge` runs on the whole config document, not on `executor`
        specifically.
        """
        from maestro.models import SpecRunnerConfig

        cfg = SpecRunnerConfig(extra_executor_config={"metadata": {"owner": "team-x"}})
        result = cfg.to_executor_config()
        assert result["metadata"] == {"owner": "team-x"}
        assert "executor" in result  # untouched sibling section survives

    def test_extra_executor_config_wins_on_scalar_conflict(self) -> None:
        from maestro.models import SpecRunnerConfig

        cfg = SpecRunnerConfig(
            claude_model="claude-sonnet-5",
            extra_executor_config={"executor": {"claude_model": "claude-opus-4-8"}},
        )
        assert cfg.to_executor_config()["executor"]["claude_model"] == "claude-opus-4-8"

    def test_extra_executor_config_none_is_noop(self) -> None:
        from maestro.models import SpecRunnerConfig

        assert SpecRunnerConfig().to_executor_config() == SpecRunnerConfig(
            extra_executor_config=None
        ).to_executor_config()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_spec_runner.py::TestSpecRunnerConfigContract -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'extra_executor_config'`

- [ ] **Step 3: Add the field and wire the merge**

In `maestro/models.py`, inside `class SpecRunnerConfig(BaseModel):`, add after the three model fields from Task 2:

```python
    extra_executor_config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Arbitrary overlay merged on top of the generated executor "
            "config, for ExecutorConfig fields SpecRunnerConfig doesn't "
            "mirror explicitly (e.g. personas, review_parallel, "
            "telegram_*, webhook_*, budgets). Deep-merged; keys here win "
            "over generated values."
        ),
    )
```

Update `to_executor_config()` to apply the merge at the end:

```python
    def to_executor_config(self) -> dict[str, Any]:
        """Convert to executor.config.yaml format."""
        result: dict[str, Any] = {
            "executor": {
                "max_retries": self.max_retries,
                "task_timeout_minutes": self.task_timeout_minutes,
                "claude_command": self.claude_command,
                "auto_commit": self.auto_commit,
                "claude_model": self.claude_model,
                "review_command": self.review_command,
                "review_model": self.review_model,
                "spec_prefix": SPEC_PREFIX,
                "hooks": {
                    "pre_start": {
                        "create_git_branch": self.create_git_branch,
                    },
                    "post_done": {
                        "run_tests": self.run_tests_on_done,
                        "run_lint": self.run_lint_on_done,
                        "auto_commit": self.auto_commit,
                    },
                },
                "commands": {
                    "test": self.test_command,
                    "lint": self.lint_command,
                },
            },
        }
        if self.extra_executor_config:
            result = _deep_merge(result, self.extra_executor_config)
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_spec_runner.py::TestSpecRunnerConfigContract -v`
Expected: PASS

- [ ] **Step 5: Run the full spec-runner test file**

Run: `uv run pytest tests/test_spec_runner.py -v`
Expected: PASS (all tests, including `TestDeepMerge` from Task 1)

- [ ] **Step 6: Type check**

Run: `uv run pyrefly check`
Expected: no new errors

- [ ] **Step 7: Commit**

```bash
git add maestro/models.py tests/test_spec_runner.py
git commit -m "feat: add extra_executor_config deep-merge escape hatch"
```

---

### Task 4: Regenerate JSON Schema + full verification

**Files:**
- Modify: `maestro/schemas/orchestrator_config.json` (regenerated, not hand-edited)

**Interfaces:**
- Consumes: the updated `SpecRunnerConfig` from Tasks 2 and 3 (via `OrchestratorConfig.spec_runner`).
- Produces: nothing new — this task just keeps the shipped schema artifact in sync.

- [ ] **Step 1: Regenerate the schema**

Run: `uv run python -m maestro.schemas.generate`
Expected output: `Written .../maestro/schemas/project_config.json` and `Written .../maestro/schemas/orchestrator_config.json`

- [ ] **Step 2: Confirm the diff only adds the new fields**

Run: `git diff maestro/schemas/orchestrator_config.json`
Expected: new `claude_model`, `review_command`, `review_model`, `extra_executor_config` properties appear under the `SpecRunnerConfig` definition; no unrelated fields change.

- [ ] **Step 3: Run ruff format and check**

Run: `uv run ruff format . && uv run ruff check .`
Expected: no changes needed / no errors (fix any reported issues in `maestro/models.py` or `tests/test_spec_runner.py` before proceeding)

- [ ] **Step 4: Run pyrefly**

Run: `uv run pyrefly check`
Expected: no errors

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest`
Expected: all tests PASS (no regressions in `test_models.py`, `test_config.py`, `test_examples_smoke.py`, `test_cli.py`, `test_no_unreferenced_modules.py` — these were flagged in earlier exploration as schema-adjacent)

- [ ] **Step 6: Commit**

```bash
git add maestro/schemas/orchestrator_config.json
git commit -m "chore: regenerate orchestrator_config.json schema"
```

---

## Self-Review Notes

- **Spec coverage:** Design's §1 (typed fields) → Task 2. §2 (`to_executor_config` wiring + merge) → Tasks 2/3. §3 (`extra_executor_config` + `_deep_merge`) → Tasks 1/3. §4 (tests, including nested-merge and top-level-key cases per review feedback) → Tasks 1/2/3. §5 (schema regen) → Task 4. Non-goals (no personas/budgets typed fields) — not implemented, matches spec.
- **Placeholder scan:** no TBD/TODO; every step has literal code or an exact command with expected output.
- **Type consistency:** `_deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]` signature is identical everywhere it's referenced (Task 1 definition, Task 3 usage). Field names (`claude_model`, `review_command`, `review_model`, `extra_executor_config`) match between Task 2/3 definitions and all test usages.
