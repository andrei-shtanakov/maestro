# LABS-90 per-example YAML smoke test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A CI smoke test that loads/validates every `examples/*` config so a shipped example can't silently drift from the schema.

**Architecture:** A parametrized pytest test (`tests/test_examples_smoke.py`, runs in the existing CI test job — no workflow change). Per `examples/*.yaml`: dummy-set any `${VAR}` env refs (loaders are strict on unset vars), detect the mode (top-level `repo_url`/`workstreams` → Mode-2), and dispatch — Mode-1 → `load_config` (raises on drift), Mode-2 → `load_orchestrator_config` + `validate_project(check_fs=False)` (schema + graph, no filesystem). Plus `observed-models.json` via `parse_observed_manifest`, and a discrimination test that a broken config is rejected.

**Tech Stack:** Python 3.12+, uv, pytest, PyYAML, pydantic, tomllib. No new dependencies.

## Global Constraints

- `uv` only; `uv run pytest` / `uv run pyrefly check` / `uv run ruff format .` + `uv run ruff check .`; line length 88; run pytest in the FOREGROUND.
- The smoke validates STRUCTURE/SCHEMA with dummy `${VAR}` env placeholders and `check_fs=False` — it does NOT run the examples or check that env vars resolve to real paths. Scope: catch schema/graph drift.
- Mode discriminator: a top-level `repo_url` OR `workstreams` key ⇒ Mode-2 (`OrchestratorConfig`); else Mode-1 (`ProjectConfig`). `repo_url`/`workstreams` are Mode-2-only (ProjectConfig has neither).
- Mode-2 pass = `load_orchestrator_config` returns AND `validate_project(config, check_fs=False).ok` (no `error`-severity issues; warnings allowed). Mode-1 pass = `load_config` returns (raises `ConfigError` on drift — it wraps pydantic `ValidationError` into `ConfigError`).
- `resolve_env_vars` is strict: an unset `${VAR}` raises `ConfigError`. The smoke `monkeypatch.setenv`s each `${VAR}` found in the example text to `"x"` before loading.
- Guard against a vacuous pass: assert `examples/*.yaml` is non-empty (an empty/moved dir must fail loudly, not silently collect zero cases).
- Signatures (verbatim): `load_config(path: Path | str) -> ProjectConfig`; `load_orchestrator_config(path) -> OrchestratorConfig`; `load_config_from_string(content, path=None) -> ProjectConfig`; `validate_project(config, *, check_fs=True) -> ValidationReport` with `.ok` / `.errors`; `parse_observed_manifest(data: object) -> dict[str, list[str]]`; `ConfigError` in `maestro.config` (load_config surfaces this, wrapping pydantic `ValidationError`).
- Branch: `feat/labs-90-example-smoke` (create it). Full suite green at every commit.

---

### Task 1: The per-example smoke test

**Files:**
- Create: `tests/test_examples_smoke.py`

**Interfaces:**
- Consumes: `maestro.config.{load_config, load_orchestrator_config, load_config_from_string, ConfigError}`, `maestro.preflight.validate_project`, `maestro.catalog_discovery.parse_observed_manifest`, `pydantic.ValidationError`.

- [ ] **Step 1: Write the test file**

Create `tests/test_examples_smoke.py`:

```python
"""LABS-90: per-example smoke test — every examples/* config still loads and
validates, so a shipped example cannot silently drift from the schema.

Scope: schema/graph drift. `${VAR}` env refs get dummy placeholders (the config
loaders are strict on unset vars) and Mode-2 preflight runs with check_fs=False
— the smoke does not run the examples or resolve env vars to real paths.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from maestro.catalog_discovery import parse_observed_manifest
from maestro.config import (
    ConfigError,
    load_config,
    load_config_from_string,
    load_orchestrator_config,
)
from maestro.preflight import validate_project

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
_ENV_VAR = re.compile(r"\$\{(\w+)\}")


def _example_yamls() -> list[Path]:
    return sorted(_EXAMPLES.glob("*.yaml"))


def _set_dummy_env(text: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in set(_ENV_VAR.findall(text)):
        monkeypatch.setenv(var, "x")


def _is_mode2(raw: dict) -> bool:
    return "repo_url" in raw or "workstreams" in raw


def test_examples_dir_has_yaml_configs() -> None:
    # A moved/empty examples dir must fail loudly, not collect zero cases.
    assert _example_yamls(), "no examples/*.yaml found — smoke would be vacuous"


@pytest.mark.parametrize("path", _example_yamls(), ids=lambda p: p.name)
def test_example_yaml_loads_and_validates(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = path.read_text(encoding="utf-8")
    _set_dummy_env(text, monkeypatch)
    raw = yaml.safe_load(text)
    assert isinstance(raw, dict), f"{path.name}: top-level YAML is not a mapping"

    if _is_mode2(raw):
        config = load_orchestrator_config(path)
        report = validate_project(config, check_fs=False)
        assert report.ok, (
            f"{path.name}: Mode-2 preflight errors: "
            f"{[i.message for i in report.errors]}"
        )
    else:
        load_config(path)  # raises ConfigError / ValidationError on schema drift


def test_observed_models_json_parses() -> None:
    data = json.loads(
        (_EXAMPLES / "observed-models.json").read_text(encoding="utf-8")
    )
    parse_observed_manifest(data)  # raises on a malformed manifest


def test_smoke_rejects_a_broken_config() -> None:
    # Discrimination: an invalid config must be rejected, so the per-example
    # smoke genuinely catches drift rather than passing vacuously. A ProjectConfig
    # requires `tasks`; a mapping without it must fail.
    with pytest.raises((ConfigError, ValidationError)):
        load_config_from_string("project: broken\n")
```

- [ ] **Step 2: Run it — confirm green baseline + discrimination**

Run: `uv run pytest tests/test_examples_smoke.py -v`
Expected: PASS — one `test_example_yaml_loads_and_validates[<name>]` per example
(7 Mode-1 + 1 Mode-2 `project.yaml`), plus the dir-has-configs, observed-json,
and broken-config tests. If `test_smoke_rejects_a_broken_config` does NOT raise
(i.e. `project: broken` is somehow schema-valid), make the broken input a
non-mapping instead — `load_config_from_string("[]\n")` — and confirm it raises;
note which you used.

If a real example FAILS (genuine drift), STOP and report — that's a real broken
example to fix (either fix the example or, if the failure is an env/fs artifact
the smoke should tolerate, report it for a design tweak; do NOT loosen the smoke
to hide a real drift).

- [ ] **Step 3: Gates**

Run: `uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Then: `uv run pytest -q`
Expected: pyrefly 0; ruff clean; full suite green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_examples_smoke.py
git commit -m "test(ci): per-example config smoke test (LABS-90)

Loads/validates every examples/*.yaml (Mode-1 load_config; Mode-2
load_orchestrator_config + validate_project check_fs=False) plus
observed-models.json, with dummy \${VAR} env placeholders, so a shipped
example cannot silently drift from the schema. Runs in the existing pytest
CI job (no workflow change)."
```

---

### Task 2: TODO tick, final gates, PR

**Files:**
- Modify: `TODO.md`

- [ ] **Step 1: TODO.md — tick LABS-90**

Change the LABS-90 line to `[x]` with the commit hash and a one-line summary:

```markdown
- [x] **LABS-90** (Medium): per-example YAML smoke test в CI (commit `<sha>`) — `tests/test_examples_smoke.py`, parametrized over `examples/*.yaml` (Mode-1 `load_config`; Mode-2 `load_orchestrator_config` + `validate_project(check_fs=False)`) + `observed-models.json`; dummy `${VAR}` env, no filesystem.
```

(Use the Task 1 commit sha.)

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
git commit -m "docs: tick LABS-90 per-example config smoke test"
```

- [ ] **Step 4: Push and open the PR** (controller may defer until after the final review)

```bash
git push -u origin feat/labs-90-example-smoke
gh pr create --title "test(ci): per-example config smoke test (LABS-90)" --body "$(cat <<'EOF'
## Summary
LABS-90 (v0.2.0 dogfood): `examples/*` configs can silently drift from the schema. This adds a CI smoke test so a broken example fails CI.

- `tests/test_examples_smoke.py` — a parametrized pytest test (runs in the existing CI test job, no workflow change; one case per example → clear failure attribution). Per `examples/*.yaml`: dummy-sets any `${VAR}` env refs (the loaders are strict on unset vars), detects the mode (top-level `repo_url`/`workstreams` → Mode-2), and dispatches:
  - **Mode-1** (`tasks:`) → `load_config` (raises `ConfigError`/`ValidationError` on drift)
  - **Mode-2** (`project.yaml`) → `load_orchestrator_config` + `validate_project(check_fs=False)` — schema + graph checks (dangling deps / cycles / scope), no filesystem
- Also smoke-checks `observed-models.json` via `parse_observed_manifest`
- Guards: a `test_examples_dir_has_yaml_configs` assertion (an empty/moved dir fails loudly, not a vacuous pass) and a discrimination test that a broken config is rejected (the smoke genuinely catches drift)
- Scope: schema/graph drift only — dummy env placeholders + `check_fs=False`; does not run the examples. No new dependencies

## Test plan
- [x] Full suite green; pyrefly 0; ruff clean
- [x] One passing case per `examples/*.yaml` (7 Mode-1 + 1 Mode-2) + observed-json + discrimination
- [x] Discrimination: a config missing a required field is rejected

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Coverage: per-example dispatch (Mode-1/Mode-2) → Task 1 Step 1; observed-models.json → same; discrimination + non-empty guard → same; docs/TODO → Task 2.
- Type consistency: `load_config`/`load_orchestrator_config`/`load_config_from_string`/`validate_project(check_fs=)`/`parse_observed_manifest` used with their verified signatures.
- The env-var strictness (`${VAR}` → dummy) and `check_fs=False` are the two load-bearing "why it doesn't false-fail in CI" points — both in Global Constraints and Task 1.
- Two tasks: the smoke test (one deliverable, one reviewer gate) and docs/PR.
