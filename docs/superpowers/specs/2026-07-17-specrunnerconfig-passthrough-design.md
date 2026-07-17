# SpecRunnerConfig passthrough: model fields + escape hatch

## Context

`SpecRunnerConfig` (`maestro/models.py:1152`) is a hand-maintained subset of
spec-runner's `ExecutorConfig` (`spec-runner/src/spec_runner/config.py`), with
no drift check. Every new `ExecutorConfig` field is invisible to Maestro until
someone manually mirrors it into `SpecRunnerConfig` and wires it into
`to_executor_config()`. Handoff note:
`prograph-vault/authored/notes/2026-07-17-maestro-specrunnerconfig-gaps-handoff.md`.

Currently proxied: `max_retries`, `task_timeout_minutes`, `claude_command`,
`auto_commit`, `spec_prefix`, `hooks.pre_start.create_git_branch`,
`hooks.post_done.{run_tests,run_lint,auto_commit}`, `commands.{test,lint}`.

Not proxied (Maestro-run silently falls back to spec-runner/CLI defaults):
models (`claude_model`, `review_command`, `review_model`), personas, parallel
review, Telegram, webhook, budgets, and several hook flags (`lint_blocking`,
`integration_pr`, `main_branch`, `sync_deps`).

## Goal

1. Proxy the three most commonly wanted fields as first-class, typed
   `SpecRunnerConfig` fields: `claude_model`, `review_command`, `review_model`.
2. Add a generic escape hatch, `extra_executor_config`, so any current or
   future `ExecutorConfig` field can reach spec-runner without Maestro
   mirroring it field-by-field — closing the drift problem systemically
   instead of one field at a time.

Explicitly out of scope: personas, parallel review, Telegram/webhook,
budgets — these remain reachable only via `extra_executor_config`. No new
typed fields beyond the three above.

## Design

### 1. New typed fields on `SpecRunnerConfig`

```python
claude_model: str = Field(default="", description="Claude model for tasks (empty = CLI default)")
review_command: str = Field(default="", description="Review CLI command (empty = claude_command)")
review_model: str = Field(default="", description="Review model (empty = claude_model)")
```

Defaults match `ExecutorConfig`'s own defaults (`""`), so emitting these keys
unconditionally is behavior-preserving for anyone who hasn't set them.

### 2. `to_executor_config()` changes

- Always emit `claude_model`, `review_command`, `review_model` in the
  `executor` dict (same convention as existing always-emitted fields like
  `claude_command`).
- After building the base dict, if `extra_executor_config` is set, deep-merge
  it on top (extra wins on conflict — it's an explicit, opt-in override).

```python
def to_executor_config(self) -> dict[str, Any]:
    result: dict[str, Any] = {
        "executor": {
            ...existing fields...,
            "claude_model": self.claude_model,
            "review_command": self.review_command,
            "review_model": self.review_model,
        },
    }
    if self.extra_executor_config:
        result = _deep_merge(result, self.extra_executor_config)
    return result
```

### 3. `extra_executor_config` field + `_deep_merge` helper

```python
extra_executor_config: dict[str, Any] | None = Field(
    default=None,
    description=(
        "Arbitrary overlay merged on top of the generated executor config, "
        "for ExecutorConfig fields SpecRunnerConfig doesn't mirror explicitly "
        "(e.g. personas, review_parallel, telegram_*, webhook_*, budgets). "
        "Deep-merged; keys here win over generated values."
    ),
)
```

`_deep_merge` is a small, pure module-level function in `models.py`:

- Recurses only where **both** `base[key]` and `override[key]` are `dict`.
- Otherwise the override value replaces the base value outright (no list
  concatenation, no partial merge of non-dict values).
- Does not mutate either input — returns a new dict (base and override are
  copied, not written into).

This lets `extra_executor_config` reach into nested `executor.hooks.*` without
clobbering sibling keys, and also add keys *outside* `executor` entirely
(e.g. a top-level `environment` block), since the merge runs on the full
`{"executor": {...}}` result, not just the inner dict.

### 4. Tests (`tests/test_spec_runner.py::TestSpecRunnerConfigContract`)

- New fields default to `""` and appear in `executor{}`.
- Explicit values for `claude_model`/`review_command`/`review_model` are
  passed through unchanged.
- `extra_executor_config` deep-merges into a nested path
  (`executor.hooks.post_done.lint_blocking`) without dropping sibling keys
  (`run_tests`, `run_lint`).
- `extra_executor_config` can add a **top-level** key not under `executor`
  (e.g. `{"metadata": {"FOO": "bar"}}`), proving the escape hatch covers the
  whole config document, not just the `executor` section. Note: this is a
  merge-mechanics test only — spec-runner only *processes* its supported
  top-level sections (`executor`; `environment` is explicitly a
  `_DEAD_SECTIONS` entry that warns "top-level key ignored", per
  `spec-runner/src/spec_runner/validate.py:383`), so an arbitrary top-level
  key isn't itself a useful passthrough example.
- `_deep_merge` is pure: calling it does not mutate the base or override
  dict arguments.

### 5. Generated schema

Regenerate `maestro/schemas/orchestrator_config.json` via
`uv run python -m maestro.schemas.generate` after the model change, so the
shipped JSON Schema reflects the new fields. No hand-editing.

## Non-goals

- No typed fields for personas/parallel-review/notifications/budgets — reach
  those via `extra_executor_config`.
- No validation of `extra_executor_config`'s shape against `ExecutorConfig` —
  it's a trusted escape hatch; unknown/unsupported keys follow spec-runner's
  existing validation/loading behavior (its config loader picks known fields
  and its `validate_config` warns on unrecognized top-level sections — it
  does not uniformly fail loud).
