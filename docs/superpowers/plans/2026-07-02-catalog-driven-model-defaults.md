# Catalog-Driven Model Defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two hardcoded `DEFAULT_<H>_MODEL` constants in Maestro's spawners with a runtime catalog loader (per ADR-ECO-003b: no baked default, `$ATP_CATALOG` user-config, fail-loud), plus a status-graded routed-path drift warning.

**Architecture:** A new `maestro/catalog.py` owns a pydantic schema, a `$ATP_CATALOG` loader, model resolution (`routed > MAESTRO_<H>_MODEL > catalog-default > fail`), and a coherence warning. Faults split by blast radius: global (`CatalogError` → halt the run) vs per-task (`HarnessModelUnresolved` → that task to `NEEDS_REVIEW`). Spawners call the loader once per spawn; the scheduler gets a three-way spawn-error handler.

**Tech Stack:** Python 3.12+, pydantic v2, `tomllib` (stdlib), structlog-based `obs`, pytest + anyio, uv.

## Global Constraints

- Package management: **uv only**, never pip. No new runtime dependency (`tomllib` is stdlib on 3.12+).
- Type hints required on all code; run `uv run pyrefly check` and fix errors.
- Line length ≤ 88; run `uv run ruff format .` and `uv run ruff check . --fix`.
- Async tests use **anyio**, not asyncio (`anyio_backend` fixture returns `"asyncio"`).
- No baked model string (`claude-sonnet-*`, `gpt-*`) may remain anywhere in `maestro/` after this work.
- Follow existing patterns: obs events via `obs.get_logger(...)`, `capture_logs()` for log assertions.
- Full suite must pass: `uv run pytest`.

**Source of truth:** `docs/superpowers/specs/2026-07-02-catalog-driven-model-defaults-design.md`.

---

### Task 1: Catalog schema, loader, and lookups (`maestro/catalog.py`)

**Files:**
- Create: `maestro/catalog.py`
- Create: `tests/fixtures/agents-catalog.toml`
- Create: `tests/fixtures/agents-catalog-ambiguous.toml`
- Create: `tests/fixtures/agents-catalog-malformed.toml`
- Test: `tests/test_catalog.py`

**Interfaces:**
- Produces:
  - `class CatalogModel(BaseModel)`: `vendor: str`, `status: Literal["active","deprecated","retired"] = "active"`, `aliases: list[str] = []`
  - `class CatalogAgent(BaseModel)`: `harness: str`, `model: str`, `tested: bool = False`, `routable: bool = False`
  - `class Catalog(BaseModel)`: `models: dict[str, CatalogModel]`, `agents: list[CatalogAgent]`; methods `default_model_for_harness(harness: str) -> str`, `status_of(model: str) -> str | None`, `nearest_models(model: str, n: int = 3) -> list[str]`
  - `class CatalogError(RuntimeError)`; `class CatalogNotConfigured(CatalogError)`; `class CatalogMalformed(CatalogError)`
  - `class HarnessModelUnresolved(RuntimeError)` (NOT a `CatalogError`)
  - `resolve_catalog_path() -> Path | None`
  - `load_catalog() -> Catalog | None`

- [ ] **Step 1: Write the fixtures**

Create `tests/fixtures/agents-catalog.toml`:

```toml
# Test fixture mirroring the SSOT schema (Plane 1 models + Plane 3 agents).
[models."claude-sonnet-4-6"]
vendor  = "anthropic"
status  = "active"
aliases = ["claude-sonnet-latest"]

[models."gpt-5.5"]
vendor = "openai"
status = "active"

[models."legacy-mini"]
vendor = "openai"
status = "deprecated"

[models."ancient-1"]
vendor = "anthropic"
status = "retired"

[[agents]]
harness  = "claude_code"
model    = "claude-sonnet-4-6"
tested   = true
routable = true

[[agents]]
harness  = "codex_cli"
model    = "gpt-5.5"
tested   = true
routable = true
```

Create `tests/fixtures/agents-catalog-ambiguous.toml` (A/B window — two routable for `claude_code`):

```toml
[models."claude-sonnet-4-6"]
vendor = "anthropic"
status = "active"

[models."claude-sonnet-5"]
vendor = "anthropic"
status = "active"

[[agents]]
harness  = "claude_code"
model    = "claude-sonnet-4-6"
tested   = true
routable = true

[[agents]]
harness  = "claude_code"
model    = "claude-sonnet-5"
tested   = true
routable = true
```

Create `tests/fixtures/agents-catalog-malformed.toml` (schema-invalid `status`):

```toml
[models."broken"]
vendor = "anthropic"
status = "not-a-valid-status"

[[agents]]
harness  = "claude_code"
model    = "broken"
routable = true
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_catalog.py`:

```python
"""Tests for the model catalog loader (ADR-ECO-003b)."""

import os
from pathlib import Path

import pytest

from maestro.catalog import (
    Catalog,
    CatalogMalformed,
    CatalogNotConfigured,
    HarnessModelUnresolved,
    load_catalog,
    resolve_catalog_path,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _use_catalog(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("ATP_CATALOG", str(FIXTURES / name))


def test_path_unset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATP_CATALOG", raising=False)
    assert resolve_catalog_path() is None
    assert load_catalog() is None


def test_path_absent_file_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATP_CATALOG", str(FIXTURES / "does-not-exist.toml"))
    # A path typo must not crash — it is "no catalog", not a fatal error.
    assert load_catalog() is None


def test_malformed_raises_catalog_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_catalog(monkeypatch, "agents-catalog-malformed.toml")
    with pytest.raises(CatalogMalformed):
        load_catalog()


def test_default_model_for_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_catalog(monkeypatch, "agents-catalog.toml")
    cat = load_catalog()
    assert cat is not None
    assert cat.default_model_for_harness("claude_code") == "claude-sonnet-4-6"
    assert cat.default_model_for_harness("codex_cli") == "gpt-5.5"


def test_default_no_routable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_catalog(monkeypatch, "agents-catalog.toml")
    cat = load_catalog()
    assert cat is not None
    with pytest.raises(HarnessModelUnresolved):
        cat.default_model_for_harness("aider")


def test_default_ambiguous_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_catalog(monkeypatch, "agents-catalog-ambiguous.toml")
    cat = load_catalog()
    assert cat is not None
    with pytest.raises(HarnessModelUnresolved):
        cat.default_model_for_harness("claude_code")


def test_per_task_error_is_not_a_catalog_error() -> None:
    # Guards the blast-radius split: per-task must never be caught by the
    # scheduler's `except CatalogError` halt arm.
    assert not issubclass(HarnessModelUnresolved, CatalogError)  # noqa: F821


def test_status_of(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_catalog(monkeypatch, "agents-catalog.toml")
    cat = load_catalog()
    assert cat is not None
    assert cat.status_of("claude-sonnet-4-6") == "active"
    assert cat.status_of("legacy-mini") == "deprecated"
    assert cat.status_of("ancient-1") == "retired"
    assert cat.status_of("claude-sonnet-latest") == "active"  # alias resolves
    assert cat.status_of("never-heard-of-it") is None  # unknown


def test_nearest_models(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_catalog(monkeypatch, "agents-catalog.toml")
    cat = load_catalog()
    assert cat is not None
    near = cat.nearest_models("claude-sonnet-4-7")
    assert "claude-sonnet-4-6" in near
```

Also add `from maestro.catalog import CatalogError` to the import block (used by `test_per_task_error_is_not_a_catalog_error`); remove the `# noqa: F821`.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.catalog'`.

- [ ] **Step 4: Implement `maestro/catalog.py`**

```python
"""Model catalog loader and resolution (ADR-ECO-003b).

The catalog is user configuration, not shipped in the package and not vendored
for runtime use. It is resolved from $ATP_CATALOG (XDG default path is a
follow-up). There is no baked default model: when no catalog and no override
supply a model, resolution fails loud.

Fault taxonomy is split by blast radius:
  * CatalogError (CatalogNotConfigured / CatalogMalformed) — the catalog is
    unusable for everyone; the scheduler halts the whole run.
  * HarnessModelUnresolved — this one harness cannot resolve a default; the
    scheduler sends only that task to NEEDS_REVIEW and keeps running. It is
    deliberately NOT a CatalogError.
"""

from __future__ import annotations

import difflib
import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from maestro._vendor import obs

_obs_log = obs.get_logger("maestro.catalog")

_NOT_CONFIGURED_MSG = (
    "модельный каталог не настроен: задай $ATP_CATALOG (или 'atp models init')"
)


class CatalogError(RuntimeError):
    """Global catalog fault — the catalog is unusable for everyone. Halts the run."""


class CatalogNotConfigured(CatalogError):
    """No catalog configured and a default is needed."""


class CatalogMalformed(CatalogError):
    """$ATP_CATALOG set, file present, but corrupt / schema-invalid."""


class HarnessModelUnresolved(RuntimeError):
    """No routable model, or >1, for this harness. Per-task; NOT a CatalogError."""


class CatalogModel(BaseModel):
    """Plane 1 model entry."""

    vendor: str
    status: Literal["active", "deprecated", "retired"] = "active"
    aliases: list[str] = []


class CatalogAgent(BaseModel):
    """Plane 3 enrollment entry (harness, model) pair."""

    harness: str
    model: str
    tested: bool = False
    routable: bool = False


class Catalog(BaseModel):
    """Parsed catalog. Plane 2 (harnesses) is ignored — Maestro does not need it."""

    models: dict[str, CatalogModel]
    agents: list[CatalogAgent] = []

    def default_model_for_harness(self, harness: str) -> str:
        """Model of the single routable [[agents]] entry for this harness.

        Raises HarnessModelUnresolved (per-task) when there is no routable entry,
        or more than one (the ADR-003a A/B window).
        """
        routable = [a.model for a in self.agents if a.harness == harness and a.routable]
        if len(routable) == 1:
            return routable[0]
        if not routable:
            raise HarnessModelUnresolved(
                f"каталог не содержит routable-модели для harness '{harness}'; "
                f"задай MAESTRO_{harness.upper()}_MODEL"
            )
        raise HarnessModelUnresolved(
            f"неоднозначный default для harness '{harness}': routable-моделей "
            f"{len(routable)} ({', '.join(routable)}); задай "
            f"MAESTRO_{harness.upper()}_MODEL"
        )

    def status_of(self, model: str) -> str | None:
        """Status of a model id, resolving aliases. None means unknown."""
        entry = self.models.get(model)
        if entry is not None:
            return entry.status
        for m in self.models.values():
            if model in m.aliases:
                return m.status
        return None

    def nearest_models(self, model: str, n: int = 3) -> list[str]:
        """Closest known model ids, for warning payloads."""
        return difflib.get_close_matches(model, list(self.models), n=n, cutoff=0.3)


def resolve_catalog_path() -> Path | None:
    """Resolve the catalog file path. $ATP_CATALOG only for now.

    XDG default path ($XDG_CONFIG_HOME/<eco>/agents-catalog.toml) is a follow-up
    gated on the ratified <eco> namespace.
    """
    env_path = os.environ.get("ATP_CATALOG")
    return Path(env_path) if env_path else None


def load_catalog() -> Catalog | None:
    """Load and validate the catalog.

    Returns None for both "no catalog" cases: $ATP_CATALOG unset, or set but the
    file is absent (a path typo must not crash a routed run). Raises
    CatalogMalformed when the file is present but corrupt / schema-invalid.
    """
    path = resolve_catalog_path()
    if path is None:
        return None
    if not path.is_file():
        _obs_log.info("catalog.path_absent", path=str(path))
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return Catalog.model_validate(data)
    except (tomllib.TOMLDecodeError, ValidationError, OSError) as exc:
        raise CatalogMalformed(f"каталог повреждён ({path}): {exc}") from exc
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_catalog.py -v`
Expected: PASS (all cases in Step 2).

- [ ] **Step 6: Add the optional contract test**

Append to `tests/test_catalog.py`:

```python
def test_fixture_matches_sibling_ssot() -> None:
    """When the sibling dev/ops SSOT exists, the fixture's routable defaults must
    still match it. Skipped in isolation (CI without the sibling repo). Seed of
    the ADR-003b cross-reader conformance test (shape only, not behavior)."""
    ssot = (
        Path(__file__).parents[3]
        / "atp-platform"
        / "method"
        / "agents-catalog.toml"
    )
    if not ssot.is_file():
        pytest.skip("sibling atp-platform SSOT not present")
    import tomllib

    data = tomllib.loads(ssot.read_text(encoding="utf-8"))
    cat = Catalog.model_validate(data)
    assert cat.default_model_for_harness("claude_code") == "claude-sonnet-4-6"
    assert cat.default_model_for_harness("codex_cli") == "gpt-5.5"
```

- [ ] **Step 7: Lint, type-check, run**

Run: `uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check && uv run pytest tests/test_catalog.py -v`
Expected: clean; all pass (the sibling-SSOT test may `SKIP`).

- [ ] **Step 8: Commit**

```bash
git add maestro/catalog.py tests/test_catalog.py tests/fixtures/agents-catalog*.toml
git commit -m "feat(catalog): loader, schema, and lookups with blast-radius fault taxonomy"
```

---

### Task 2: Model resolution and coherence warning (`maestro/catalog.py`)

**Files:**
- Modify: `maestro/catalog.py` (add two functions)
- Test: `tests/test_catalog.py` (add cases)

**Interfaces:**
- Consumes: `Catalog`, `CatalogNotConfigured`, `HarnessModelUnresolved`, `load_catalog` (Task 1).
- Produces:
  - `resolve_model(routed: str | None, env_var: str, harness: str, catalog: Catalog | None) -> tuple[str, str]` — returns `(model, source)`, `source ∈ {"routed","env","catalog"}`.
  - `warn_on_model_status(model: str, source: str, catalog: Catalog | None) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_catalog.py`:

```python
from structlog.testing import capture_logs

from maestro.catalog import resolve_model, warn_on_model_status


def _catalog(monkeypatch: pytest.MonkeyPatch, name: str = "agents-catalog.toml"):
    _use_catalog(monkeypatch, name)
    return load_catalog()


def test_resolve_precedence_routed_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _catalog(monkeypatch)
    monkeypatch.setenv("MAESTRO_CLAUDE_MODEL", "env-x")
    assert resolve_model("routed-x", "MAESTRO_CLAUDE_MODEL", "claude_code", cat) == (
        "routed-x",
        "routed",
    )


def test_resolve_env_then_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _catalog(monkeypatch)
    monkeypatch.setenv("MAESTRO_CLAUDE_MODEL", "env-x")
    assert resolve_model(None, "MAESTRO_CLAUDE_MODEL", "claude_code", cat) == (
        "env-x",
        "env",
    )
    monkeypatch.delenv("MAESTRO_CLAUDE_MODEL", raising=False)
    assert resolve_model(None, "MAESTRO_CLAUDE_MODEL", "claude_code", cat) == (
        "claude-sonnet-4-6",
        "catalog",
    )


def test_resolve_empty_routed_treated_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _catalog(monkeypatch)
    monkeypatch.delenv("MAESTRO_CLAUDE_MODEL", raising=False)
    assert resolve_model("", "MAESTRO_CLAUDE_MODEL", "claude_code", cat) == (
        "claude-sonnet-4-6",
        "catalog",
    )


def test_resolve_no_catalog_default_path_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAESTRO_CLAUDE_MODEL", raising=False)
    with pytest.raises(CatalogNotConfigured):
        resolve_model(None, "MAESTRO_CLAUDE_MODEL", "claude_code", None)


def test_resolve_routed_selfsufficient_without_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Routed does not need a catalog — no raise even when catalog is None.
    assert resolve_model("routed-x", "MAESTRO_CLAUDE_MODEL", "claude_code", None) == (
        "routed-x",
        "routed",
    )


def test_warn_retired_fires_even_for_catalog_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cat = _catalog(monkeypatch)
    with capture_logs() as logs:
        warn_on_model_status("ancient-1", "catalog", cat)
    assert any(e["event"] == "agent.model_retired" for e in logs)


def test_warn_deprecated_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _catalog(monkeypatch)
    with capture_logs() as logs:
        warn_on_model_status("legacy-mini", "routed", cat)
    assert any(e["event"] == "agent.model_deprecated" for e in logs)


def test_warn_unknown_soft_for_routed_but_skipped_for_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cat = _catalog(monkeypatch)
    with capture_logs() as logs:
        warn_on_model_status("mystery", "routed", cat)
    assert any(e["event"] == "agent.model_unknown" for e in logs)

    with capture_logs() as logs:
        warn_on_model_status("mystery", "catalog", cat)  # tautological → skip
    assert not any(e["event"] == "agent.model_unknown" for e in logs)


def test_warn_active_silent_and_no_catalog_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cat = _catalog(monkeypatch)
    with capture_logs() as logs:
        warn_on_model_status("claude-sonnet-4-6", "routed", cat)
    assert not [e for e in logs if e["event"].startswith("agent.model_")]

    with capture_logs() as logs:
        warn_on_model_status("anything", "routed", None)  # no catalog → no-op
    assert not [e for e in logs if e["event"].startswith("agent.model_")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_catalog.py -k "resolve or warn" -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_model'`.

- [ ] **Step 3: Implement the two functions**

Append to `maestro/catalog.py`:

```python
def resolve_model(
    routed: str | None,
    env_var: str,
    harness: str,
    catalog: Catalog | None,
) -> tuple[str, str]:
    """Resolve the model to run and its source. Precedence: routed > env >
    catalog-default. An empty ``routed`` is treated as absent (guards against a
    degenerate ``"<harness>@"`` id producing an empty ``--model``).

    Raises CatalogNotConfigured (GLOBAL → halt) when the default path is reached
    with no catalog. Propagates HarnessModelUnresolved (PER-TASK) from
    default_model_for_harness for no-routable / ambiguous harnesses.
    """
    if routed:
        return routed, "routed"
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val, "env"
    if catalog is None:
        raise CatalogNotConfigured(_NOT_CONFIGURED_MSG)
    return catalog.default_model_for_harness(harness), "catalog"


def warn_on_model_status(model: str, source: str, catalog: Catalog | None) -> None:
    """Coherence check against the catalog only (never provider reality — that is
    the CLI's job). No-op when catalog is None. Grades by status: retired → loud,
    deprecated → light, active → silent — for every source. The unknown → soft
    branch is the only source-gated one (skipped for source == 'catalog', where
    membership is tautological). Never blocks the spawn.
    """
    if catalog is None:
        return
    status = catalog.status_of(model)
    if status == "retired":
        _obs_log.warning(
            "agent.model_retired",
            model=model,
            source=source,
            nearest=catalog.nearest_models(model),
        )
    elif status == "deprecated":
        _obs_log.warning("agent.model_deprecated", model=model, source=source)
    elif status is None and source != "catalog":
        _obs_log.info(
            "agent.model_unknown",
            model=model,
            source=source,
            nearest=catalog.nearest_models(model),
        )
    # active → silent
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_catalog.py -v`
Expected: PASS (all Task 1 + Task 2 cases).

- [ ] **Step 5: Lint, type-check**

Run: `uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/catalog.py tests/test_catalog.py
git commit -m "feat(catalog): resolve_model + status-graded coherence warning"
```

---

### Task 3: Wire spawners to the catalog; remove baked defaults

**Files:**
- Modify: `maestro/spawners/base.py` (delete `resolve_model`)
- Modify: `maestro/spawners/claude_code.py` (delete `DEFAULT_CLAUDE_MODEL`; call loader)
- Modify: `maestro/spawners/codex.py` (delete `DEFAULT_CODEX_MODEL`; call loader)
- Modify: `tests/conftest.py` (clean `ATP_CATALOG`; add `catalog_env` fixture)
- Modify: `tests/test_spawners.py` (drop `DEFAULT_*` imports; relocate `resolve_model` unit tests; assert from loaded catalog)

**Interfaces:**
- Consumes: `load_catalog`, `resolve_model`, `warn_on_model_status` (Tasks 1–2).
- Produces: spawners with no module-level default model constant; `base.py` no longer exports `resolve_model`.

- [ ] **Step 1: Update the conftest**

In `tests/conftest.py`, extend the autouse cleanup to also clear `ATP_CATALOG`, and add a fixture that points it at the Task 1 fixture. Replace the `cleanup_environment` fixture body and append `catalog_env`:

```python
@pytest.fixture(autouse=True)
def cleanup_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clean env vars that could affect tests."""
    import os

    for key in list(os.environ.keys()):
        if key.startswith("MAESTRO_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("ATP_CATALOG", raising=False)


@pytest.fixture
def catalog_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point $ATP_CATALOG at the test fixture catalog; return its default models."""
    fixture = Path(__file__).parent / "fixtures" / "agents-catalog.toml"
    monkeypatch.setenv("ATP_CATALOG", str(fixture))
    return fixture
```

- [ ] **Step 2: Update the failing spawner tests**

In `tests/test_spawners.py`:

1. Delete the two imports (lines 18–19):
   ```python
   from maestro.spawners.claude_code import DEFAULT_CLAUDE_MODEL
   from maestro.spawners.codex import DEFAULT_CODEX_MODEL
   ```
2. Relocate the `resolve_model` unit tests (the block near lines 59–94 that does `from maestro.spawners.base import resolve_model`): **delete them here** — the equivalent, updated-signature tests already live in `tests/test_catalog.py` (Task 2). Remove the now-empty test class/section header if one is left behind.
3. In the claude default-model command test (around line 322), make it use the catalog fixture and assert the model from the loaded catalog:
   ```python
   def test_...(self, ..., catalog_env: Path) -> None:
       ...
       from maestro.catalog import load_catalog
       expected = load_catalog().default_model_for_harness("claude_code")
       assert cmd[cmd.index("--model") + 1] == expected
   ```
   Add `catalog_env` to that test method's parameters so `$ATP_CATALOG` is set.
4. In the claude `agent.model_resolved` test (lines 453–457), the env-cleared case now resolves from the catalog:
   ```python
   with capture_logs() as logs, patch.dict(os.environ, {}, clear=True):
       os.environ["ATP_CATALOG"] = str(catalog_env)
       claude_spawner.spawn(sample_task, "", workdir, temp_dir / "t.log")
   ev = next(e for e in logs if e["event"] == "agent.model_resolved")
   assert ev["source"] == "catalog"
   assert ev["model"] == load_catalog().default_model_for_harness("claude_code")
   ```
   (Add `catalog_env: Path` to the method params; add `from maestro.catalog import load_catalog`.)
5. Apply the same two edits to the codex tests (command test around line 847; `agent.model_resolved` test around lines 943–947), using harness `"codex_cli"` and flag `-m`.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_spawners.py -v`
Expected: FAIL — `ImportError` for `DEFAULT_CLAUDE_MODEL` (constants still exist but imports were removed → actually collection error referencing removed names), confirming the tests now depend on the new wiring.

- [ ] **Step 4: Delete `resolve_model` from `base.py`**

In `maestro/spawners/base.py`, remove the entire `resolve_model` function (lines 27–46). Keep `spawn_env` and `AgentSpawner`. Leave the `import os` (still used by `spawn_env`).

- [ ] **Step 5: Rewire `claude_code.py`**

In `maestro/spawners/claude_code.py`:
1. Delete the `DEFAULT_CLAUDE_MODEL` constant (line 21) and its preceding comment block (lines 17–21).
2. Change the import line 14 to:
   ```python
   from maestro.spawners.base import AgentSpawner, spawn_env
   from maestro.catalog import load_catalog, resolve_model, warn_on_model_status
   ```
3. Replace the resolve block in `spawn` (lines 80–89) with:
   ```python
   prompt = self.build_prompt(task, context, retry_context)
   catalog = load_catalog()
   resolved, source = resolve_model(
       model, "MAESTRO_CLAUDE_MODEL", "claude_code", catalog
   )
   _obs_log.info(
       "agent.model_resolved",
       harness="claude_code",
       model=resolved,
       source=source,
   )
   warn_on_model_status(resolved, source, catalog)
   ```
4. Update the class docstring line that referenced `DEFAULT_CLAUDE_MODEL` (around line 31) to: "The model is resolved from the catalog; routed model wins, then `MAESTRO_CLAUDE_MODEL`, then the catalog default."

- [ ] **Step 6: Rewire `codex.py`**

Apply the same four edits to `maestro/spawners/codex.py`, using `"MAESTRO_CODEX_MODEL"`, `"codex_cli"`, and `_obs_log` (already defined there). Delete `DEFAULT_CODEX_MODEL` (line 21) and its comment.

- [ ] **Step 7: Verify no baked model string remains**

Run: `grep -rn "claude-sonnet\|gpt-5" maestro/`
Expected: no matches (empty output).

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_spawners.py tests/test_catalog.py -v`
Expected: PASS.

- [ ] **Step 9: Lint, type-check**

Run: `uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add maestro/spawners/base.py maestro/spawners/claude_code.py maestro/spawners/codex.py tests/conftest.py tests/test_spawners.py
git commit -m "feat(spawners): resolve model from catalog; drop baked DEFAULT_<H>_MODEL"
```

---

### Task 4: Add `RUNNING → NEEDS_REVIEW` transition

**Files:**
- Modify: `maestro/models.py` (`valid_transitions`)
- Modify: `CLAUDE.md` (task state-machine diagram)
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `TaskStatus.RUNNING.can_transition_to(TaskStatus.NEEDS_REVIEW) is True`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_models.py`:

```python
def test_running_can_transition_to_needs_review() -> None:
    from maestro.models import TaskStatus

    assert TaskStatus.RUNNING.can_transition_to(TaskStatus.NEEDS_REVIEW)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py::test_running_can_transition_to_needs_review -v`
Expected: FAIL (assert False — `NEEDS_REVIEW` not in `RUNNING`'s set).

- [ ] **Step 3: Add the edge**

In `maestro/models.py`, in `valid_transitions`, change the `RUNNING` row:

```python
cls.RUNNING: {cls.VALIDATING, cls.FAILED, cls.NEEDS_REVIEW},
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS.

- [ ] **Step 5: Update the CLAUDE.md diagram**

In `CLAUDE.md`, in the "Task State Machine (scheduler mode)" diagram, add a `RUNNING -> NEEDS_REVIEW` edge with the note "(catalog default unresolved for harness)". Keep the existing arrows intact.

- [ ] **Step 6: Commit**

```bash
git add maestro/models.py tests/test_models.py CLAUDE.md
git commit -m "feat(models): allow RUNNING -> NEEDS_REVIEW (unresolvable harness default)"
```

---

### Task 5: Scheduler — three-way spawn-error handling + degenerate-id warn

**Files:**
- Modify: `maestro/scheduler.py` (`_spawn_ready_tasks`, new `_handle_unresolvable_task`, `_spawn_task` routed-model warn)
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `CatalogError`, `HarnessModelUnresolved` (Task 1); `RUNNING → NEEDS_REVIEW` (Task 4); `_handle_spawn_error`, `_report_status_change` (existing).
- Produces: `_handle_unresolvable_task(self, task_id: str, error: Exception) -> None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_scheduler.py` (reuse the existing `Scheduler`, `SchedulerConfig`, DB, and spawner-double patterns near the top; add a spawner double that raises a supplied exception from `spawn`). Add:

```python
class RaisingSpawner(BaseSpawner):
    """Spawner whose spawn() raises a supplied exception."""

    def __init__(self, exc: Exception, agent_type_name: str = "claude_code") -> None:
        self._exc = exc
        self._agent_type = agent_type_name

    @property
    def agent_type(self) -> str:
        return self._agent_type

    def is_available(self) -> bool:
        return True

    def spawn(self, *args: object, **kwargs: object) -> object:
        raise self._exc


@pytest.mark.anyio
async def test_global_catalog_error_halts_run(temp_db_path: Path) -> None:
    from maestro.catalog import CatalogNotConfigured
    # ... build scheduler with one READY claude_code task and
    # spawners={"claude_code": RaisingSpawner(CatalogNotConfigured("x"))} ...
    with anyio.fail_after(5):
        with pytest.raises(CatalogNotConfigured):
            await scheduler.run()


@pytest.mark.anyio
async def test_malformed_catalog_halts_run(temp_db_path: Path) -> None:
    from maestro.catalog import CatalogMalformed
    # spawners={"claude_code": RaisingSpawner(CatalogMalformed("bad"))}
    with anyio.fail_after(5):
        with pytest.raises(CatalogMalformed):
            await scheduler.run()


@pytest.mark.anyio
async def test_unresolvable_harness_marks_needs_review_and_continues(
    temp_db_path: Path,
) -> None:
    from maestro.catalog import HarnessModelUnresolved
    # Two READY tasks: task A on a healthy MockSpawner, task B on
    # RaisingSpawner(HarnessModelUnresolved("no routable")).
    with anyio.fail_after(5):
        await scheduler.run()
    task_b = await db.get_task("task-b")
    assert task_b.status == TaskStatus.NEEDS_REVIEW
    task_a = await db.get_task("task-a")
    assert task_a.status in (TaskStatus.DONE, TaskStatus.VALIDATING)


@pytest.mark.anyio
async def test_halt_still_runs_cleanup_no_orphans(temp_db_path: Path) -> None:
    from maestro.catalog import CatalogNotConfigured
    # One task already tracked as running (delayed MockSpawner) + a second task
    # whose spawn raises CatalogNotConfigured. After run() raises, assert the
    # tracked process.terminate/kill was called (cleanup ran) and _running_tasks
    # is empty.
    with anyio.fail_after(5):
        with pytest.raises(CatalogNotConfigured):
            await scheduler.run()
    assert scheduler._running_tasks == {}


@pytest.mark.anyio
async def test_degenerate_routed_id_warns(temp_db_path: Path) -> None:
    from structlog.testing import capture_logs
    # Task with routed_agent_type set to a trailing-'@' id so model_of_agent_id
    # returns "". Use a MockSpawner. Assert a warn event was emitted.
    with capture_logs() as logs, anyio.fail_after(5):
        await scheduler.run()
    assert any(e["event"] == "agent.routed_model_empty" for e in logs)
```

Fill each `# ...` using the construction pattern already in `tests/test_scheduler.py` (build `Database`, insert tasks via the DB API, `Scheduler(db=..., spawners={...}, config=SchedulerConfig())`). Import `anyio` at the top of the test module if not already present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_scheduler.py -k "catalog or unresolvable or orphans or degenerate" -v`
Expected: FAIL — global/malformed cases currently become per-task FAILED (no raise); `_handle_unresolvable_task` and the warn do not exist yet.

- [ ] **Step 3: Narrow the catch-all in `_spawn_ready_tasks`**

In `maestro/scheduler.py`, add the import to the `maestro.catalog` names near the other imports:

```python
from maestro.catalog import CatalogError, HarnessModelUnresolved
```

Replace the try/except in `_spawn_ready_tasks` (lines 670–674):

```python
try:
    launched = await self._spawn_task(task_id)
except CatalogError:  # GLOBAL (NotConfigured / Malformed) → halt the run
    raise
except HarnessModelUnresolved as e:  # PER-TASK, deterministic → NEEDS_REVIEW, no retry
    await self._handle_unresolvable_task(task_id, e)
except Exception as e:  # everything else → FAILED + bounded retry
    await self._handle_spawn_error(task_id, e)
```

- [ ] **Step 4: Add `_handle_unresolvable_task`**

In `maestro/scheduler.py`, next to `_handle_spawn_error` (after line 978), add:

```python
async def _handle_unresolvable_task(self, task_id: str, error: Exception) -> None:
    """Send a task to NEEDS_REVIEW without retry.

    Used for deterministic per-task faults (HarnessModelUnresolved) where a retry
    cannot help — the operator must fix the catalog or set MAESTRO_<H>_MODEL.
    """
    await self._db.update_task_status(
        task_id,
        TaskStatus.NEEDS_REVIEW,
        error_message=str(error),
    )
    self._report_status_change(task_id, "running", "needs_review")
```

- [ ] **Step 5: Add the degenerate-id warn in `_spawn_task`**

In `maestro/scheduler.py`, right after the `routed_model = ...` block (lines 889–893):

```python
if task.routed_agent_type and not routed_model:
    _obs_log.warning(
        "agent.routed_model_empty",
        task_id=task_id,
        agent_id=task.routed_agent_type,
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_scheduler.py -v`
Expected: PASS.

- [ ] **Step 7: Lint, type-check**

Run: `uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add maestro/scheduler.py tests/test_scheduler.py
git commit -m "feat(scheduler): halt on global catalog fault, NEEDS_REVIEW on per-task, warn on empty routed model"
```

---

### Task 6: Record follow-ups and run the full suite

**Files:**
- Modify: `TODO.md`

**Interfaces:** none.

- [ ] **Step 1: Append the follow-ups to `TODO.md`**

Add a section:

```markdown
## Catalog distribution follow-ups (ADR-ECO-003b)

- [ ] XDG default catalog path ($XDG_CONFIG_HOME/<eco>/agents-catalog.toml) once the
      <eco> namespace is ratified; extend `resolve_catalog_path`.
- [ ] `maestro models init | list | discover | update` CLI (ADR-003b D3).
- [ ] Shared `CLAUDE_MODEL` / `CODEX_MODEL` cross-tool override layer.
- [ ] `default = true` field in the catalog `[[agents]]` schema to disambiguate the
      A/B window (cross-repo, PM-owned) — removes the `HarnessModelUnresolved`
      ambiguity raise.
- [ ] Extract the loader to a shared PyPI lib with a cross-reader behavioral
      conformance test (precedence + alias resolution across Maestro / ATP / arbiter).
```

- [ ] **Step 2: Run the full suite**

Run: `uv run pytest`
Expected: all pass (the sibling-SSOT contract test may `SKIP`).

- [ ] **Step 3: Final lint + type-check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add TODO.md
git commit -m "docs(todo): record ADR-003b catalog-distribution follow-ups"
```

---

## Self-Review

**Spec coverage:**
- No baked default / runtime loader / `$ATP_CATALOG` → Task 1 (`load_catalog`, `resolve_catalog_path`), Task 3 (removes constants).
- Precedence `routed > MAESTRO_<H>_MODEL > catalog > fail` → Task 2 (`resolve_model`), Task 3 (wiring).
- Absent vs malformed asymmetry → Task 1 (`load_catalog`: None vs `CatalogMalformed`).
- Exception taxonomy split by blast radius → Task 1 (classes), Task 5 (three-way handler).
- Per-task → `NEEDS_REVIEW`, no retry → Task 4 (transition), Task 5 (`_handle_unresolvable_task`).
- Global/malformed → halt; `_cleanup` runs → Task 5 (tests: halt, malformed, orphans).
- Status-graded warn, source-gated unknown, coherence-only, retired-not-block → Task 2.
- Degenerate routed id warn → Task 5.
- De-hardcode test model strings → Task 3 (assert from loaded catalog).
- Contract test (shape-only, skip in isolation) → Task 1 Step 6.
- Follow-ups recorded → Task 6.

**Placeholder scan:** The `# ...` markers in Task 5 Step 1 are deliberate test-scaffold pointers to the existing `tests/test_scheduler.py` construction pattern (Database + task insert + `Scheduler(...)`); each has an explicit assertion and the surrounding real code. All implementation steps contain complete code.

**Type consistency:** `resolve_model(routed, env_var, harness, catalog)` and `warn_on_model_status(model, source, catalog)` signatures match between Task 2's definition and Task 3's call sites. `default_model_for_harness -> str` (raises, never `None`) is consistent between Task 1 and its use in `resolve_model`. `HarnessModelUnresolved` is not a `CatalogError` (asserted in Task 1 Step 2, relied on by Task 5's handler ordering).
