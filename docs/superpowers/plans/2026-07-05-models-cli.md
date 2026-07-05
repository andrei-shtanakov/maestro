# `maestro models` CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `maestro models init|list|discover|update` — user-runtime catalog management per ADR-ECO-003b D3 (first shipped implementation in the ecosystem).

**Architecture:** Pure diff logic in a new `maestro/catalog_discovery.py`; four Typer commands in a new `maestro/catalog_cli.py` registered as a sub-app (`app.add_typer(models_app, name="models")`); an inert shipped template under `maestro/resources/` with explicit setuptools package-data. `update` uses validate-then-replace (full new content validated in memory, temp file + fsync, sha256 fingerprint re-check, `os.replace`).

**Tech Stack:** Python 3.12+, uv, Typer + Rich (existing CLI stack), tomllib (stdlib), pytest CliRunner. Spec: `docs/superpowers/specs/2026-07-05-models-cli-design.md`.

## Global Constraints

- Package management: `uv` only. Tests: `uv run pytest`. Types: `uv run pyrefly check`. Lint: `uv run ruff format .` + `uv run ruff check .`. Line length 88.
- Shipped code must NEVER reference `../_cowork_output/` — the discover logic is a reimplementation of the prototype's semantics, not an import.
- Plane 2/3 are never touched by discover/update: no `[[agents]]` writes, no routable changes — routing promotion stays benchmark-gated upstream.
- `CHECKABLE_VENDORS = {"anthropic", "openai", "deepseek", "xiaomi", "alibaba", "zhipu"}` — verbatim from the prototype.
- Deprecation candidates ONLY for vendors present as manifest keys (missing key = "not observed" = zero candidates; empty list = "observed, none offered" = candidates produced).
- Exit codes are a public CLI contract: discover 0 = up to date, 2 = new models found (CI signal, not an error), 1 = error; init/list/update use 0/1.
- The template must parse and validate into an EMPTY `Catalog`. `Catalog.models` is a REQUIRED field, so the template carries one uncommented bare `[models]` table header — and TOML forbids a bare `[models]` AFTER `[models."x"]` subtables, so the bare header comes FIRST, commented examples after it.
- Branch: `feat/models-cli` (exists, spec committed). Full suite green at every commit (~1386 pre-existing).

---

### Task 1: `maestro/catalog_discovery.py` — pure diff logic

**Files:**
- Create: `maestro/catalog_discovery.py`
- Test: `tests/test_catalog_discovery.py`

**Interfaces:**
- Consumes: `Catalog`, `CatalogModel` from `maestro.catalog` (existing).
- Produces (Task 4 relies on these exact names):
  - `class ManifestInvalid(ValueError)`
  - `parse_observed_manifest(data: object) -> dict[str, list[str]]`
  - `@dataclass(frozen=True) NewModel(model_id: str, vendor: str)`
  - `@dataclass(frozen=True) AlreadyPresent(model_id: str, matched: str, via_alias: bool)`
  - `@dataclass(frozen=True) VendorConflict(model_id: str, catalog_vendor: str, observed_vendor: str)`
  - `@dataclass(frozen=True) DeprecationCandidate(model_id: str, vendor: str)`
  - `@dataclass DiscoveryReport(new_models, deprecation_candidates, already_present, vendor_conflicts)`
  - `diff_catalog(catalog: Catalog | None, observed: dict[str, list[str]]) -> DiscoveryReport`
  - `render_plane1_block(new_models: Sequence[NewModel]) -> str`
  - `CHECKABLE_VENDORS: frozenset[str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_catalog_discovery.py`:

```python
"""Tests for the pure catalog-discovery diff logic (ADR-ECO-003b D3)."""

import tomllib

import pytest

from maestro.catalog import Catalog, CatalogAgent, CatalogModel
from maestro.catalog_discovery import (
    CHECKABLE_VENDORS,
    DeprecationCandidate,
    ManifestInvalid,
    NewModel,
    diff_catalog,
    parse_observed_manifest,
    render_plane1_block,
)


def _catalog(**models: CatalogModel) -> Catalog:
    return Catalog(models=dict(models), agents=[])


class TestParseObservedManifest:
    def test_valid_manifest(self) -> None:
        data = {"anthropic": ["a", "b"], "openai": []}
        assert parse_observed_manifest(data) == {
            "anthropic": ["a", "b"],
            "openai": [],
        }

    def test_meta_ignored_any_type(self) -> None:
        for meta in ({"x": 1}, "text", 3, None, [1]):
            data = {"_meta": meta, "openai": ["gpt-5.5"]}
            assert parse_observed_manifest(data) == {"openai": ["gpt-5.5"]}

    def test_top_level_not_dict_rejected(self) -> None:
        with pytest.raises(ManifestInvalid):
            parse_observed_manifest(["not", "a", "dict"])

    def test_vendor_value_not_list_rejected(self) -> None:
        with pytest.raises(ManifestInvalid, match="openai"):
            parse_observed_manifest({"openai": "gpt-5.5"})

    def test_non_string_id_rejected(self) -> None:
        with pytest.raises(ManifestInvalid, match="anthropic"):
            parse_observed_manifest({"anthropic": ["ok", 42]})

    def test_empty_string_id_rejected(self) -> None:
        with pytest.raises(ManifestInvalid, match="anthropic"):
            parse_observed_manifest({"anthropic": [""]})

    def test_duplicates_deduped_order_preserved(self) -> None:
        data = {"openai": ["a", "b", "a", "c", "b"]}
        assert parse_observed_manifest(data) == {"openai": ["a", "b", "c"]}


class TestDiffCatalog:
    def test_new_model_detected(self) -> None:
        cat = _catalog(**{"gpt-5.5": CatalogModel(vendor="openai")})
        report = diff_catalog(cat, {"openai": ["gpt-5.5", "gpt-6"]})
        assert report.new_models == [NewModel("gpt-6", "openai")]

    def test_none_catalog_everything_new(self) -> None:
        report = diff_catalog(None, {"openai": ["gpt-6"]})
        assert report.new_models == [NewModel("gpt-6", "openai")]

    def test_alias_hit_is_not_new(self) -> None:
        cat = _catalog(
            **{
                "claude-sonnet-4-6": CatalogModel(
                    vendor="anthropic", aliases=["claude-sonnet-latest"]
                )
            }
        )
        report = diff_catalog(cat, {"anthropic": ["claude-sonnet-latest"]})
        assert report.new_models == []
        assert len(report.already_present) == 1
        hit = report.already_present[0]
        assert hit.model_id == "claude-sonnet-latest"
        assert hit.matched == "claude-sonnet-4-6"
        assert hit.via_alias is True

    def test_exact_hit_already_present(self) -> None:
        cat = _catalog(**{"gpt-5.5": CatalogModel(vendor="openai")})
        report = diff_catalog(cat, {"openai": ["gpt-5.5"]})
        assert report.new_models == []
        assert report.already_present[0].via_alias is False

    def test_vendor_conflict_detected_never_proposed(self) -> None:
        cat = _catalog(**{"glm-5.1": CatalogModel(vendor="zai")})
        report = diff_catalog(cat, {"zhipu": ["glm-5.1"]})
        assert report.new_models == []
        assert report.already_present == []
        assert len(report.vendor_conflicts) == 1
        conflict = report.vendor_conflicts[0]
        assert conflict.catalog_vendor == "zai"
        assert conflict.observed_vendor == "zhipu"

    def test_deprecation_needs_vendor_key_present(self) -> None:
        """Missing vendor key = 'not observed' — a partial manifest must
        never mass-flag an entire vendor."""
        cat = _catalog(**{"gpt-5.5": CatalogModel(vendor="openai")})
        report = diff_catalog(cat, {"anthropic": ["claude-sonnet-5"]})
        assert report.deprecation_candidates == []

    def test_deprecation_on_observed_empty_vendor(self) -> None:
        """Empty list = 'observed, offers none' — candidates ARE produced."""
        cat = _catalog(**{"gpt-5.5": CatalogModel(vendor="openai")})
        report = diff_catalog(cat, {"openai": []})
        assert report.deprecation_candidates == [
            DeprecationCandidate("gpt-5.5", "openai")
        ]

    def test_deprecation_only_checkable_vendors(self) -> None:
        cat = _catalog(**{"llama-4": CatalogModel(vendor="meta")})
        report = diff_catalog(cat, {"meta": []})
        assert "meta" not in CHECKABLE_VENDORS
        assert report.deprecation_candidates == []

    def test_deprecation_skips_non_active(self) -> None:
        cat = _catalog(
            **{"legacy-mini": CatalogModel(vendor="openai", status="deprecated")}
        )
        report = diff_catalog(cat, {"openai": []})
        assert report.deprecation_candidates == []

    def test_deprecation_alias_observed_counts_as_offered(self) -> None:
        """An observed alias keeps the canonical entry off the candidate list."""
        cat = _catalog(
            **{
                "claude-sonnet-4-6": CatalogModel(
                    vendor="anthropic", aliases=["claude-sonnet-latest"]
                )
            }
        )
        report = diff_catalog(cat, {"anthropic": ["claude-sonnet-latest"]})
        assert report.deprecation_candidates == []

    def test_deprecated_entry_not_reproposed_as_new(self) -> None:
        cat = _catalog(
            **{"legacy-mini": CatalogModel(vendor="openai", status="deprecated")}
        )
        report = diff_catalog(cat, {"openai": ["legacy-mini"]})
        assert report.new_models == []


class TestRenderPlane1Block:
    def test_block_parses_and_validates(self) -> None:
        block = render_plane1_block(
            [NewModel("gpt-6", "openai"), NewModel("claude-6", "anthropic")]
        )
        data = tomllib.loads(block)
        cat = Catalog.model_validate(data)
        assert set(cat.models) == {"gpt-6", "claude-6"}
        assert cat.models["gpt-6"].vendor == "openai"
        assert cat.models["gpt-6"].status == "active"

    def test_stable_ordering_vendor_then_id(self) -> None:
        block = render_plane1_block(
            [
                NewModel("z-model", "openai"),
                NewModel("a-model", "openai"),
                NewModel("m-model", "anthropic"),
            ]
        )
        positions = [block.index(name) for name in ("m-model", "a-model", "z-model")]
        assert positions == sorted(positions)

    @pytest.mark.parametrize(
        "weird_id",
        [
            'quote"inside',
            "back\\slash",
            "юникод-模型",
            "ctrl\x01char",
            "tab\tand\nnewline",
        ],
    )
    def test_escaping_round_trips(self, weird_id: str) -> None:
        block = render_plane1_block([NewModel(weird_id, 'v"end\\or')])
        data = tomllib.loads(block)
        cat = Catalog.model_validate(data)
        assert weird_id in cat.models
        assert cat.models[weird_id].vendor == 'v"end\\or'
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_catalog_discovery.py -q`
Expected: ImportError — `maestro.catalog_discovery` does not exist.

- [ ] **Step 3: Implement `maestro/catalog_discovery.py`**

```python
"""Pure diff logic for `maestro models discover|update` (ADR-ECO-003b D3).

Reimplements the semantics of the dev prototype
(`devtools/discover_models.py` in the coordination workspace) for the
USER-runtime catalog. Shipped code: no references to the workspace.

Planes: discovery reads/writes Plane 1 only (model existence). Plane 2/3
(harnesses, enrollment, routable) are never touched — routing promotion is
a separate benchmark-gated process upstream.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from maestro.catalog import Catalog


# Vendors whose provider listings mean something for deprecation. Local /
# baseline models (ollama, meta llama) are never deprecation candidates —
# there is no "provider offering" for them to fall out of.
CHECKABLE_VENDORS: frozenset[str] = frozenset(
    {"anthropic", "openai", "deepseek", "xiaomi", "alibaba", "zhipu"}
)


class ManifestInvalid(ValueError):
    """The observed-models manifest violates its contract."""


def parse_observed_manifest(data: object) -> dict[str, list[str]]:
    """Validate and normalize an observed-models manifest.

    Contract:
      * top level must be a JSON object;
      * ``_meta`` (any type) is ignored entirely;
      * every other key is a vendor whose value must be a list of
        non-empty strings — an EMPTY list is valid and means "vendor
        observed, offers no models";
      * duplicate ids within a vendor are deduplicated, first occurrence
        wins, order preserved; no case normalization.

    Raises:
        ManifestInvalid: naming the offending key.
    """
    if not isinstance(data, dict):
        raise ManifestInvalid("manifest top level must be a JSON object")
    observed: dict[str, list[str]] = {}
    for vendor, raw in data.items():
        if vendor == "_meta":
            continue
        if not isinstance(raw, list):
            raise ManifestInvalid(
                f"vendor {vendor!r}: value must be a list of model ids"
            )
        seen: dict[str, None] = {}
        for item in raw:
            if not isinstance(item, str) or not item:
                raise ManifestInvalid(
                    f"vendor {vendor!r}: model ids must be non-empty strings"
                )
            seen.setdefault(item)
        observed[vendor] = list(seen)
    return observed


@dataclass(frozen=True)
class NewModel:
    model_id: str
    vendor: str


@dataclass(frozen=True)
class AlreadyPresent:
    model_id: str
    matched: str
    via_alias: bool


@dataclass(frozen=True)
class VendorConflict:
    model_id: str
    catalog_vendor: str
    observed_vendor: str


@dataclass(frozen=True)
class DeprecationCandidate:
    model_id: str
    vendor: str


@dataclass
class DiscoveryReport:
    """Outcome of diffing an observed manifest against the user catalog."""

    new_models: list[NewModel] = field(default_factory=list)
    deprecation_candidates: list[DeprecationCandidate] = field(default_factory=list)
    already_present: list[AlreadyPresent] = field(default_factory=list)
    vendor_conflicts: list[VendorConflict] = field(default_factory=list)


def diff_catalog(
    catalog: Catalog | None, observed: dict[str, list[str]]
) -> DiscoveryReport:
    """Diff observed provider offerings against catalog Plane 1.

    Deprecation candidates require the vendor to be PRESENT as a manifest
    key (missing key = "vendor not observed this run" — a partial manifest
    must never mass-flag a vendor), checkable, the entry active, and
    neither the id nor any of its aliases observed for that vendor.
    """
    models = catalog.models if catalog is not None else {}
    alias_owner: dict[str, str] = {}
    for key, entry in models.items():
        for alias in entry.aliases:
            alias_owner.setdefault(alias, key)

    report = DiscoveryReport()
    for vendor in sorted(observed):
        for model_id in observed[vendor]:
            entry = models.get(model_id)
            if entry is not None:
                if entry.vendor != vendor:
                    report.vendor_conflicts.append(
                        VendorConflict(model_id, entry.vendor, vendor)
                    )
                else:
                    report.already_present.append(
                        AlreadyPresent(model_id, model_id, via_alias=False)
                    )
            elif model_id in alias_owner:
                report.already_present.append(
                    AlreadyPresent(model_id, alias_owner[model_id], via_alias=True)
                )
            else:
                report.new_models.append(NewModel(model_id, vendor))

    for key in sorted(models):
        entry = models[key]
        if (
            entry.vendor in CHECKABLE_VENDORS
            and entry.vendor in observed
            and entry.status == "active"
            and key not in observed[entry.vendor]
            and not any(a in observed[entry.vendor] for a in entry.aliases)
        ):
            report.deprecation_candidates.append(
                DeprecationCandidate(key, entry.vendor)
            )
    return report


def _toml_basic_string(value: str) -> str:
    """Render a TOML basic string (double-quoted, fully escaped).

    TOML requires escaping of quote, backslash, and all control characters
    (U+0000..U+001F, U+007F) inside basic strings.
    """
    short = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
    out = ['"']
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch in short:
            out.append(short[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def render_plane1_block(new_models: Sequence[NewModel]) -> str:
    """Ready-to-append Plane-1 TOML for the given new models.

    Stable ordering (vendor, then id); ids and vendors escaped as TOML
    basic strings so arbitrary model ids survive a round-trip.
    """
    lines: list[str] = []
    for nm in sorted(new_models, key=lambda n: (n.vendor, n.model_id)):
        lines.append(f"[models.{_toml_basic_string(nm.model_id)}]")
        lines.append(f"vendor = {_toml_basic_string(nm.vendor)}")
        lines.append('status = "active"')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_catalog_discovery.py -q`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/catalog_discovery.py tests/test_catalog_discovery.py
git commit -m "feat(catalog): pure discovery diff logic for maestro models CLI

Partial-manifest contract: deprecation candidates only for vendors
present as manifest keys; alias hits are not new; vendor conflicts are
reported, never proposed. Plane-1 renderer escapes arbitrary ids."
```

---

### Task 2: Template resource + packaging

**Files:**
- Create: `maestro/resources/__init__.py` (empty), `maestro/resources/agents-catalog-template.toml`
- Modify: `pyproject.toml` (package-data after `[tool.setuptools.packages.find]`)
- Test: `tests/test_catalog_cli.py` (template invariant + slow wheel test — new file, extended in Tasks 3-4)

**Interfaces:**
- Produces: importable resource `maestro.resources / agents-catalog-template.toml`; Task 3's `_load_template()` reads it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_catalog_cli.py`:

```python
"""Tests for the `maestro models` CLI (ADR-ECO-003b D3)."""

import subprocess
import tomllib
import zipfile
from importlib import resources
from pathlib import Path

import pytest

from maestro.catalog import Catalog


class TestTemplate:
    def test_template_is_inert_and_valid(self) -> None:
        """The shipped template parses and validates into an EMPTY Catalog:
        a fresh init resolves nothing (fail-loud preserved; no active-model
        endorsement ships in the wheel)."""
        text = (
            resources.files("maestro.resources")
            .joinpath("agents-catalog-template.toml")
            .read_text(encoding="utf-8")
        )
        cat = Catalog.model_validate(tomllib.loads(text))
        assert cat.models == {}
        assert cat.agents == []

    @pytest.mark.slow
    def test_template_ships_in_wheel(self, tmp_path: Path) -> None:
        """The only proof the resource actually lands in the built wheel."""
        repo_root = Path(__file__).parents[1]
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
            check=True,
            cwd=repo_root,
            capture_output=True,
        )
        wheels = list(tmp_path.glob("*.whl"))
        assert len(wheels) == 1
        with zipfile.ZipFile(wheels[0]) as zf:
            assert "maestro/resources/agents-catalog-template.toml" in zf.namelist()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_catalog_cli.py -q -m "not slow"`
Expected: FAIL — `maestro.resources` does not exist.

- [ ] **Step 3: Create the resource package and template**

`maestro/resources/__init__.py`:

```python
"""Shipped data resources (templates); loaded via importlib.resources."""
```

`maestro/resources/agents-catalog-template.toml`:

```toml
# Maestro model catalog (user-runtime; ADR-ECO-003b).
#
# One file, three readers: Maestro, ATP, and arbiter all resolve this file
# via $ATP_CATALOG. Edit it to declare the models YOUR instance may use —
# nothing here is vendored into any tool; you own this file.
#
# Plane 1 — models this instance knows about:
#   [models."<model-id>"]
#   vendor  = "<vendor>"        # anthropic / openai / zai / ...
#   status  = "active"          # active | deprecated | retired
#   aliases = ["<other-id>"]    # optional
#
# Plane 3 — which (harness, model) pairs are enrolled:
#   [[agents]]
#   harness  = "claude_code"    # claude_code | codex_cli | aider | opencode
#   model    = "<model-id>"
#   tested   = true
#   routable = true             # exactly ONE routable entry per harness
#
# `maestro models discover --observed <manifest.json>` proposes additions;
# `maestro models update` applies them (Plane 1 only — enrolling a model
# for routing is your editorial decision).

# Required schema scaffolding — keep this table header even when empty.
# (TOML note: this bare header must stay ABOVE any [models."..."] entry.)
[models]

# Examples — uncomment and adapt:
#
# [models."claude-sonnet-4-6"]
# vendor = "anthropic"
# status = "active"
#
# [[agents]]
# harness  = "claude_code"
# model    = "claude-sonnet-4-6"
# tested   = true
# routable = true
```

`pyproject.toml` — directly after the `[tool.setuptools.packages.find]` table:

```toml
[tool.setuptools.package-data]
"maestro.resources" = ["*.toml"]
```

- [ ] **Step 4: Run tests (incl. the slow wheel test once)**

Run: `uv run pytest tests/test_catalog_cli.py -q`
Expected: both PASS (run WITHOUT `-m "not slow"` here — the wheel test must be proven once in this task).

- [ ] **Step 5: Gates + commit**

```bash
uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/resources/ pyproject.toml tests/test_catalog_cli.py
git commit -m "feat(catalog): inert catalog template resource + explicit package-data

Template validates to an EMPTY Catalog (bare [models] header is required
schema scaffolding and must precede any subtable). Slow wheel test proves
the resource actually ships."
```

---

### Task 3: `catalog_cli.py` — init + list, registration, message fix

**Files:**
- Create: `maestro/catalog_cli.py`
- Modify: `maestro/cli.py` (import + `add_typer` after the `app = typer.Typer(...)` block ~line 76)
- Modify: `maestro/catalog.py:31-33` (`_NOT_CONFIGURED_MSG`)
- Test: `tests/test_catalog_cli.py` (extend)

**Interfaces:**
- Consumes: template resource (Task 2); `resolve_catalog_path`, `load_catalog`, `CatalogMalformed` from `maestro.catalog`.
- Produces: `models_app: typer.Typer` (Task 4 adds discover/update to it); helpers `_resolved_catalog_or_exit() -> tuple[Path, Catalog]` and `_load_template() -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_catalog_cli.py`:

```python
from typer.testing import CliRunner

from maestro.cli import app


runner = CliRunner()


def _write_catalog(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


MINIMAL_CATALOG = """
[models."gpt-5.5"]
vendor = "openai"
status = "active"

[[agents]]
harness  = "codex_cli"
model    = "gpt-5.5"
tested   = true
routable = true
"""


class TestModelsInit:
    def test_init_writes_template_to_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATP_CATALOG", raising=False)
        target = tmp_path / "cat" / "agents-catalog.toml"
        result = runner.invoke(app, ["models", "init", "--path", str(target)])
        assert result.exit_code == 0
        assert "export ATP_CATALOG=" in result.output
        cat = Catalog.model_validate(
            tomllib.loads(target.read_text(encoding="utf-8"))
        )
        assert cat.models == {}

    def test_init_uses_atp_catalog_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "agents-catalog.toml"
        monkeypatch.setenv("ATP_CATALOG", str(target))
        result = runner.invoke(app, ["models", "init"])
        assert result.exit_code == 0
        assert target.is_file()

    def test_init_refuses_existing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "agents-catalog.toml"
        target.write_text("# already here\n[models]\n", encoding="utf-8")
        monkeypatch.setenv("ATP_CATALOG", str(target))
        result = runner.invoke(app, ["models", "init"])
        assert result.exit_code == 1
        assert "refusing to overwrite" in result.output
        assert target.read_text(encoding="utf-8").startswith("# already here")

    def test_init_no_target_errors_with_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATP_CATALOG", raising=False)
        result = runner.invoke(app, ["models", "init"])
        assert result.exit_code == 1
        assert "--path" in result.output
        assert "ATP_CATALOG" in result.output


class TestModelsList:
    def test_list_renders_models_and_agents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "agents-catalog.toml"
        _write_catalog(target, MINIMAL_CATALOG)
        monkeypatch.setenv("ATP_CATALOG", str(target))
        result = runner.invoke(app, ["models", "list"])
        assert result.exit_code == 0
        assert "gpt-5.5" in result.output
        assert "codex_cli" in result.output

    def test_list_unset_env_hints_init(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATP_CATALOG", raising=False)
        result = runner.invoke(app, ["models", "list"])
        assert result.exit_code == 1
        assert "maestro models init" in result.output

    def test_list_configured_but_missing_names_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "nope" / "agents-catalog.toml"
        monkeypatch.setenv("ATP_CATALOG", str(missing))
        result = runner.invoke(app, ["models", "list"])
        assert result.exit_code == 1
        assert str(missing) in result.output

    def test_list_empty_catalog_notes_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "agents-catalog.toml"
        _write_catalog(target, "[models]\n")
        monkeypatch.setenv("ATP_CATALOG", str(target))
        result = runner.invoke(app, ["models", "list"])
        assert result.exit_code == 0
        assert "empty" in result.output

    def test_list_malformed_catalog_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "agents-catalog.toml"
        target.write_text("this is not toml [", encoding="utf-8")
        monkeypatch.setenv("ATP_CATALOG", str(target))
        result = runner.invoke(app, ["models", "list"])
        assert result.exit_code == 1
        assert "corrupt" in result.output
```

Also update the message assertion in the existing catalog tests: grep `tests/test_catalog.py` for `atp models init` and change the expected text to `maestro models init`.

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_catalog_cli.py -q -m "not slow"`
Expected: FAIL — no `models` sub-app registered.

- [ ] **Step 3: Implement**

Create `maestro/catalog_cli.py`:

```python
"""`maestro models` — user-runtime catalog management (ADR-ECO-003b D3).

init scaffolds a user catalog from the shipped inert template; list shows
the resolved catalog; discover/update (added alongside) compare an observed
provider manifest and propose/apply Plane-1 additions. Plane 2/3 are never
written by any command here.
"""

import os
from importlib import resources
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from maestro.catalog import (
    Catalog,
    CatalogMalformed,
    load_catalog,
    resolve_catalog_path,
)


models_app = typer.Typer(help="Manage the model catalog (ADR-ECO-003b D3).")
console = Console()
err_console = Console(stderr=True)


def _load_template() -> str:
    """Read the shipped inert catalog template."""
    return (
        resources.files("maestro.resources")
        .joinpath("agents-catalog-template.toml")
        .read_text(encoding="utf-8")
    )


def _resolved_catalog_or_exit() -> tuple[Path, Catalog]:
    """Resolve + load the catalog, or exit 1 with an actionable message.

    load_catalog() returns None for BOTH "env unset" and "file missing";
    the CLI distinguishes them so the user gets the right next step.
    """
    path = resolve_catalog_path()
    if path is None:
        err_console.print(
            "[red]no catalog configured[/red] — run 'maestro models init' "
            "(and set $ATP_CATALOG)"
        )
        raise typer.Exit(1)
    if not path.is_file():
        err_console.print(
            f"[red]catalog configured at {path} but the file is missing[/red]"
            f" — run 'maestro models init --path {path}' or fix $ATP_CATALOG"
        )
        raise typer.Exit(1)
    try:
        catalog = load_catalog()
    except CatalogMalformed as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    assert catalog is not None  # path existence checked above
    return path, catalog


@models_app.command("init")
def models_init(
    path: Path | None = typer.Option(
        None,
        "--path",
        help="Target file. Defaults to $ATP_CATALOG when set.",
    ),
) -> None:
    """Scaffold a starter user catalog from the shipped inert template."""
    target = path or resolve_catalog_path()
    if target is None:
        err_console.print(
            "[red]no target[/red]: pass --path or set $ATP_CATALOG"
        )
        raise typer.Exit(1)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Exclusive create: existence check and write are one atomic
        # operation — no TOCTOU window.
        with open(target, "x", encoding="utf-8") as fh:
            fh.write(_load_template())
    except FileExistsError:
        err_console.print(
            f"[red]{target} already exists[/red] — refusing to overwrite"
        )
        raise typer.Exit(1) from None
    console.print(f"[green]Catalog scaffolded:[/green] {target}")
    console.print("Next steps: edit the file (uncomment / add your models).")
    if os.environ.get("ATP_CATALOG") != str(target):
        console.print(
            f"Point all readers at it:\n  export ATP_CATALOG={target}"
        )


@models_app.command("list")
def models_list() -> None:
    """Show the resolved catalog: models (Plane 1) and enrollments (Plane 3)."""
    path, catalog = _resolved_catalog_or_exit()
    console.print(f"Catalog: {path} (source: $ATP_CATALOG)")
    if not catalog.models and not catalog.agents:
        console.print(
            f"[yellow]catalog is empty[/yellow] — edit {path} or run "
            "'maestro models discover'"
        )
        return
    models_table = Table(title="Models (Plane 1)")
    models_table.add_column("model id")
    models_table.add_column("vendor")
    models_table.add_column("status")
    for model_id in sorted(catalog.models):
        entry = catalog.models[model_id]
        status = entry.status
        if status != "active":
            status = f"[yellow]{status}[/yellow]"
        models_table.add_row(model_id, entry.vendor, status)
    console.print(models_table)
    if catalog.agents:
        agents_table = Table(title="Agents (Plane 3)")
        agents_table.add_column("harness")
        agents_table.add_column("model")
        agents_table.add_column("tested")
        agents_table.add_column("routable")
        for agent in catalog.agents:
            agents_table.add_row(
                agent.harness,
                agent.model,
                str(agent.tested),
                str(agent.routable),
            )
        console.print(agents_table)
```

`maestro/catalog.py` — fix the hint (line ~31):

```python
_NOT_CONFIGURED_MSG = (
    "model catalog not configured: set $ATP_CATALOG "
    "(or run 'maestro models init')"
)
```

`maestro/cli.py` — with the other maestro imports:

```python
from maestro.catalog_cli import models_app
```

and directly after the `app = typer.Typer(...)` block:

```python
app.add_typer(models_app, name="models")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_catalog_cli.py -q -m "not slow" && uv run pytest tests/test_catalog.py tests/test_cli.py -q`
Expected: PASS (including the updated `maestro models init` message assertion).

- [ ] **Step 5: Gates + commit**

```bash
uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/catalog_cli.py maestro/cli.py maestro/catalog.py tests/test_catalog_cli.py tests/test_catalog.py
git commit -m "feat(cli): maestro models init + list

init creates exclusively (open 'x' — no TOCTOU) from the shipped inert
template; list distinguishes env-unset from configured-but-missing and
renders Plane 1/3 tables. Not-configured hint now names maestro's own
command instead of 'atp models init'."
```

---

### Task 4: discover + update

**Files:**
- Modify: `maestro/catalog_cli.py` (two commands + report/write helpers)
- Create: `examples/observed-models.json`
- Test: `tests/test_catalog_cli.py` (extend)

**Interfaces:**
- Consumes: Task 1's `parse_observed_manifest`, `diff_catalog`, `render_plane1_block`, `ManifestInvalid`, `DiscoveryReport`; Task 3's `_resolved_catalog_or_exit`, `models_app`, consoles.
- Produces: `maestro models discover|update` with the public exit-code contract (0/2/1).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_catalog_cli.py`:

```python
import json


OBSERVED_UP_TO_DATE = {"openai": ["gpt-5.5"]}
OBSERVED_WITH_NEW = {"openai": ["gpt-5.5", "gpt-6"]}


def _write_manifest(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestModelsDiscover:
    def _setup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Path:
        target = tmp_path / "agents-catalog.toml"
        _write_catalog(target, MINIMAL_CATALOG)
        monkeypatch.setenv("ATP_CATALOG", str(target))
        return target

    def test_up_to_date_exit_0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup(tmp_path, monkeypatch)
        manifest = _write_manifest(tmp_path / "obs.json", OBSERVED_UP_TO_DATE)
        result = runner.invoke(
            app, ["models", "discover", "--observed", str(manifest)]
        )
        assert result.exit_code == 0
        assert "up to date" in result.output

    def test_new_models_exit_2_with_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog_path = self._setup(tmp_path, monkeypatch)
        before = catalog_path.read_text(encoding="utf-8")
        manifest = _write_manifest(tmp_path / "obs.json", OBSERVED_WITH_NEW)
        result = runner.invoke(
            app, ["models", "discover", "--observed", str(manifest)]
        )
        assert result.exit_code == 2
        assert '[models."gpt-6"]' in result.output
        # discover never edits the catalog
        assert catalog_path.read_text(encoding="utf-8") == before

    def test_out_writes_block_and_refuses_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup(tmp_path, monkeypatch)
        manifest = _write_manifest(tmp_path / "obs.json", OBSERVED_WITH_NEW)
        out = tmp_path / "block.toml"
        result = runner.invoke(
            app,
            ["models", "discover", "--observed", str(manifest), "--out", str(out)],
        )
        assert result.exit_code == 2
        assert '[models."gpt-6"]' in out.read_text(encoding="utf-8")
        result2 = runner.invoke(
            app,
            ["models", "discover", "--observed", str(manifest), "--out", str(out)],
        )
        assert result2.exit_code == 1
        assert "refusing to overwrite" in result2.output

    def test_malformed_manifest_exit_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup(tmp_path, monkeypatch)
        manifest = _write_manifest(tmp_path / "obs.json", {"openai": "gpt"})
        result = runner.invoke(
            app, ["models", "discover", "--observed", str(manifest)]
        )
        assert result.exit_code == 1
        assert "openai" in result.output

    def test_vendor_conflict_warned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup(tmp_path, monkeypatch)
        manifest = _write_manifest(
            tmp_path / "obs.json", {"someone-else": ["gpt-5.5"]}
        )
        result = runner.invoke(
            app, ["models", "discover", "--observed", str(manifest)]
        )
        assert result.exit_code == 0  # conflict alone does not change exit
        assert "conflict" in result.output.lower()


class TestModelsUpdate:
    def _setup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path]:
        target = tmp_path / "agents-catalog.toml"
        _write_catalog(target, MINIMAL_CATALOG)
        monkeypatch.setenv("ATP_CATALOG", str(target))
        manifest = _write_manifest(tmp_path / "obs.json", OBSERVED_WITH_NEW)
        return target, manifest

    def test_dry_run_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target, manifest = self._setup(tmp_path, monkeypatch)
        before = target.read_text(encoding="utf-8")
        result = runner.invoke(
            app,
            ["models", "update", "--observed", str(manifest), "--dry-run"],
        )
        assert result.exit_code == 0
        assert '[models."gpt-6"]' in result.output
        assert target.read_text(encoding="utf-8") == before

    def test_yes_appends_and_second_run_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target, manifest = self._setup(tmp_path, monkeypatch)
        before = target.read_text(encoding="utf-8")
        result = runner.invoke(
            app, ["models", "update", "--observed", str(manifest), "--yes"]
        )
        assert result.exit_code == 0
        after = target.read_text(encoding="utf-8")
        # existing bytes preserved verbatim as prefix
        assert after.startswith(before)
        assert '[models."gpt-6"]' in after
        # result still loads as a valid catalog
        cat = Catalog.model_validate(tomllib.loads(after))
        assert "gpt-6" in cat.models
        # idempotent: second run applies nothing
        result2 = runner.invoke(
            app, ["models", "update", "--observed", str(manifest), "--yes"]
        )
        assert result2.exit_code == 0
        assert "nothing to apply" in result2.output
        assert target.read_text(encoding="utf-8") == after

    def test_declined_confirm_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target, manifest = self._setup(tmp_path, monkeypatch)
        before = target.read_text(encoding="utf-8")
        result = runner.invoke(
            app, ["models", "update", "--observed", str(manifest)], input="n\n"
        )
        assert result.exit_code == 1
        assert target.read_text(encoding="utf-8") == before

    def test_invalid_result_refused_file_untouched(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the composed content would not validate, nothing touches disk.
        Simulated by corrupting the renderer's output."""
        target, manifest = self._setup(tmp_path, monkeypatch)
        before = target.read_bytes()
        monkeypatch.setattr(
            "maestro.catalog_cli.render_plane1_block",
            lambda new_models: "[models.unclosed\n",
        )
        result = runner.invoke(
            app, ["models", "update", "--observed", str(manifest), "--yes"]
        )
        assert result.exit_code == 1
        assert target.read_bytes() == before

    def test_fingerprint_mismatch_aborts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A concurrent writer between read and replace is detected."""
        target, manifest = self._setup(tmp_path, monkeypatch)
        import maestro.catalog_cli as cli_mod

        original_render = cli_mod.render_plane1_block

        def mutate_then_render(new_models):  # noqa: ANN001, ANN202
            target.write_text(
                target.read_text(encoding="utf-8") + "\n# sneaky edit\n",
                encoding="utf-8",
            )
            return original_render(new_models)

        monkeypatch.setattr(cli_mod, "render_plane1_block", mutate_then_render)
        result = runner.invoke(
            app, ["models", "update", "--observed", str(manifest), "--yes"]
        )
        assert result.exit_code == 1
        assert "changed underneath" in result.output
        assert "# sneaky edit" in target.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_catalog_cli.py -q -m "not slow"`
Expected: new classes FAIL — no `discover`/`update` commands.

- [ ] **Step 3: Implement**

Add to `maestro/catalog_cli.py` (imports grow: `hashlib`, `json`, `tempfile`, `tomllib`, `datetime`, and the Task 1 names):

```python
import hashlib
import json
import tempfile
import tomllib
from datetime import UTC, datetime

from maestro.catalog_discovery import (
    DiscoveryReport,
    ManifestInvalid,
    diff_catalog,
    parse_observed_manifest,
    render_plane1_block,
)
```

Helpers:

```python
def _parse_manifest_or_exit(observed: Path) -> dict[str, list[str]]:
    try:
        raw = json.loads(observed.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        err_console.print(f"[red]cannot read manifest {observed}[/red]: {exc}")
        raise typer.Exit(1) from exc
    try:
        return parse_observed_manifest(raw)
    except ManifestInvalid as exc:
        err_console.print(f"[red]invalid manifest[/red]: {exc}")
        raise typer.Exit(1) from exc


def _print_report(report: DiscoveryReport) -> None:
    for conflict in report.vendor_conflicts:
        err_console.print(
            f"[bold yellow]WARNING vendor conflict:[/bold yellow] "
            f"{conflict.model_id!r} is cataloged under "
            f"{conflict.catalog_vendor!r} but observed under "
            f"{conflict.observed_vendor!r} — not touching it"
        )
    if report.already_present:
        for hit in report.already_present:
            via = f" (alias of {hit.matched})" if hit.via_alias else ""
            console.print(f"already present: {hit.model_id}{via}")
    if report.deprecation_candidates:
        console.print(
            "[yellow]deprecation candidates[/yellow] "
            "(review by hand — this tool never edits existing entries):"
        )
        for cand in report.deprecation_candidates:
            console.print(f"  {cand.model_id} ({cand.vendor})")
    if report.new_models:
        console.print(f"new models: {len(report.new_models)}")


def _atomic_write_new_file(target: Path, content: str) -> None:
    """Atomically create `target`; refuses an existing file."""
    if target.exists():
        err_console.print(
            f"[red]{target} already exists[/red] — refusing to overwrite"
        )
        raise typer.Exit(1)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
```

Commands:

```python
@models_app.command("discover")
def models_discover(
    observed: Path = typer.Option(
        ...,
        "--observed",
        help="Observed-models manifest: JSON {vendor: [model_id, ...]}.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Write the proposed Plane-1 TOML block here (never the catalog).",
    ),
) -> None:
    """Compare observed provider offerings with the user catalog. Read-only.

    Exit codes (public contract): 0 = catalog up to date, 2 = new models
    found (a CI signal, not an error), 1 = error.
    """
    _path, catalog = _resolved_catalog_or_exit()
    manifest = _parse_manifest_or_exit(observed)
    report = diff_catalog(catalog, manifest)
    _print_report(report)
    if not report.new_models:
        console.print("[green]catalog is up to date[/green]")
        return
    block = render_plane1_block(report.new_models)
    console.print(block)
    if out is not None:
        _atomic_write_new_file(out, block)
        console.print(f"proposed block written to {out}")
    raise typer.Exit(2)


@models_app.command("update")
def models_update(
    observed: Path = typer.Option(
        ...,
        "--observed",
        help="Observed-models manifest: JSON {vendor: [model_id, ...]}.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be appended; write nothing."
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Skip the confirmation prompt (CI/scripting)."
    ),
) -> None:
    """Apply discover's proposals: append new Plane-1 entries to the catalog.

    Never modifies existing entries and never touches [[agents]] — enrolling
    a model for routing stays your editorial decision.
    """
    path, catalog = _resolved_catalog_or_exit()
    manifest = _parse_manifest_or_exit(observed)
    report = diff_catalog(catalog, manifest)
    _print_report(report)
    if not report.new_models:
        console.print("nothing to apply — catalog is up to date")
        return

    original = path.read_bytes()
    fingerprint = hashlib.sha256(original).hexdigest()

    block = render_plane1_block(report.new_models)
    console.print(block)
    if dry_run:
        console.print("[yellow]dry-run[/yellow]: no changes written")
        return
    if not yes and not typer.confirm(
        f"Append {len(report.new_models)} model(s) to {path}?"
    ):
        err_console.print("aborted — no changes written")
        raise typer.Exit(1)

    header = (
        f"\n# added by maestro models update "
        f"{datetime.now(UTC).date().isoformat()}\n"
    )
    new_content = original.decode("utf-8") + header + block

    # Validate the FULL future content in memory BEFORE anything touches
    # disk — an update must not be able to leave the catalog invalid.
    try:
        Catalog.model_validate(tomllib.loads(new_content))
    except Exception as exc:
        err_console.print(
            f"[red]refusing to write: result would be invalid[/red]: {exc}"
        )
        raise typer.Exit(1) from exc

    # Temp file in the same directory, fsync, fingerprint re-check, atomic
    # replace. Best-effort lost-update guard, not a lock protocol.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_content)
            fh.flush()
            os.fsync(fh.fileno())
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        if current != fingerprint:
            err_console.print(
                "[red]catalog changed underneath us[/red] — re-run"
            )
            raise typer.Exit(1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    console.print(
        f"[green]added {len(report.new_models)} model(s) to {path}[/green]"
    )
```

Note the fingerprint is taken from the SAME `original` bytes the new content is composed from (read once, before rendering) — the mutate-mid-run test depends on that ordering.

Create `examples/observed-models.json`:

```json
{
  "_meta": {
    "purpose": "Example observed-models manifest for `maestro models discover|update`.",
    "how_to_fill": "By hand from provider docs/CLIs, or by a scheduled collector. Keys are vendors as in the catalog's Plane 1.",
    "note": "A MISSING vendor key means 'not observed this run' (no deprecation candidates); an EMPTY list means 'observed, offers no models'."
  },
  "observed": "remove this line — top-level keys other than _meta are vendors",
  "anthropic": ["claude-sonnet-4-6", "claude-opus-4-8"],
  "openai": ["gpt-5.5"]
}
```

Wait — that `"observed"` line would fail validation (string value). The example must be directly valid:

```json
{
  "_meta": {
    "purpose": "Example observed-models manifest for `maestro models discover|update`.",
    "how_to_fill": "By hand from provider docs/CLIs, or by a scheduled collector. Keys are vendors as in the catalog's Plane 1.",
    "note": "A MISSING vendor key means 'not observed this run' (no deprecation candidates); an EMPTY list means 'observed, offers no models'."
  },
  "anthropic": ["claude-sonnet-4-6", "claude-opus-4-8"],
  "openai": ["gpt-5.5"]
}
```

(Use the second form — the file must pass `parse_observed_manifest` as-is; add a test line in `TestModelsDiscover` if convenient, or trust the manifest-contract tests.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_catalog_cli.py -q -m "not slow"`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/catalog_cli.py examples/observed-models.json tests/test_catalog_cli.py
git commit -m "feat(cli): maestro models discover + update

discover is read-only (exit 0/2/1 as a public contract; --out refuses
existing targets, writes atomically). update validates the FULL future
content in memory, then temp file + fsync + sha256 fingerprint re-check +
os.replace — a crash leaves either the old file or the new valid one."
```

---

### Task 5: Docs, TODO ticks, final gates

**Files:**
- Modify: `CLAUDE.md` (Development Commands + Architecture module list)
- Modify: `TODO.md` (tick D3 item; tick opencode follow-up #2 with SSOT evidence)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Update CLAUDE.md**

In the Development Commands block, after the "Mode-2 config authoring" section, add:

```markdown
# === Model catalog management (ADR-ECO-003b D3) ===
uv run maestro models init --path ~/.config/atp/agents-catalog.toml   # Scaffold user catalog
uv run maestro models list                                            # Show resolved catalog
uv run maestro models discover --observed observed.json               # Propose additions (exit 2 = new found)
uv run maestro models update --observed observed.json --dry-run       # Apply proposals (Plane 1 only)
```

In the Architecture "Shared infrastructure" module list, after the **catalog.py** bullet add:

```markdown
- **catalog_discovery.py**: Pure diff logic for `maestro models` — observed-manifest contract (missing vendor key = not observed; empty list = observed-and-empty), alias-aware new-model detection, vendor-conflict reporting, TOML-escaped Plane-1 rendering
- **catalog_cli.py**: `maestro models init|list|discover|update` Typer sub-app (ADR-ECO-003b D3) — init from the shipped inert template (`maestro/resources/`), read-only discover (public exit contract 0/2/1), update via validate-then-atomic-replace
```

- [ ] **Step 2: Tick TODO.md items**

In `## Catalog distribution follow-ups (ADR-ECO-003b)`: mark the
"`maestro models init | list | discover | update` CLI (ADR-003b D3)" item
`[x]` with `(closed by feat/models-cli)`.

In `## opencode follow-ups (ADR-ECO-003c)`: mark the SSOT catalog item `[x]`
with the note:

```markdown
      Verified 2026-07-05: atp-platform/method/agents-catalog.toml has
      [harnesses.opencode] + one routable [[agents]] opencode/glm-5.1
      (promoted 2026-07-03, gate 003a D4) + two Path B non-routable entries;
      Maestro's loader resolves default_model_for_harness('opencode') ==
      'glm-5.1' against it. Done upstream by the atp-platform actor.
```

- [ ] **Step 3: Final gates + smoke**

```bash
uv run pytest -q
uv run pytest -q -m slow          # wheel test, once
uv run pyrefly check
uv run ruff format . && uv run ruff check .
git status --short
```

Smoke (full user journey in a sandbox):

```bash
T=$(mktemp -d)
ATP_CATALOG="$T/cat.toml" uv run maestro models init
ATP_CATALOG="$T/cat.toml" uv run maestro models list
echo '{"openai": ["gpt-6"]}' > "$T/obs.json"
ATP_CATALOG="$T/cat.toml" uv run maestro models discover --observed "$T/obs.json"; echo "discover exit: $? (expect 2)"
ATP_CATALOG="$T/cat.toml" uv run maestro models update --observed "$T/obs.json" --yes
ATP_CATALOG="$T/cat.toml" uv run maestro models list
ATP_CATALOG="$T/cat.toml" uv run maestro models discover --observed "$T/obs.json"; echo "discover exit: $? (expect 0)"
```

Expected: init scaffolds; first list notes empty; discover exits 2 with a gpt-6 block; update appends; second list shows gpt-6; second discover exits 0.

- [ ] **Step 4: Commit docs**

```bash
git add CLAUDE.md TODO.md
git commit -m "docs: maestro models CLI shipped (ADR-003b D3); SSOT opencode entry verified done upstream"
```

- [ ] **Step 5: Push and open the PR** (controller may defer until after the final whole-branch review)

```bash
git push -u origin feat/models-cli
gh pr create --title "feat(cli): maestro models init|list|discover|update (ADR-ECO-003b D3)" --body "$(cat <<'EOF'
## Summary
- First shipped implementation of ADR-ECO-003b D3 in the ecosystem: user-runtime catalog management
- `init` — exclusive-create scaffold from a shipped inert template (validates to an EMPTY catalog; no active-model endorsement in the wheel; explicit setuptools package-data + slow wheel-inspection test)
- `list` — resolved catalog with Plane 1/3 tables; distinguishes env-unset from configured-but-missing
- `discover` — read-only diff against an observed manifest; partial-manifest contract (missing vendor key = not observed → zero deprecation candidates); alias-aware; vendor conflicts warned, never touched; public exit contract 0/2/1
- `update` — discover's write side, Plane 1 only: full future content validated in memory, then temp file + fsync + sha256 fingerprint re-check + os.replace
- Side fix: catalog not-configured hint now says `maestro models init` (was `atp models init`)
- TODO: D3 item ticked; opencode SSOT follow-up ticked (done upstream, verified against Maestro's loader)

Spec: docs/superpowers/specs/2026-07-05-models-cli-design.md

## Test plan
- [ ] Full suite green incl. slow wheel test; pyrefly + ruff clean
- [ ] Discovery contract tests (partial manifest, aliases, conflicts, escaping round-trips)
- [ ] CLI tests: init TOCTOU-free exclusive create; update fingerprint-mismatch abort; invalid-result refusal leaves file byte-identical
- [ ] End-to-end smoke: init → list → discover(2) → update → discover(0)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Spec coverage: architecture/modules → Tasks 1-3; ObservedManifest contract → Task 1; template + packaging + wheel test → Task 2; shared resolution contract + init + list + message fix → Task 3; discover/update incl. write strategy + exit contract + examples manifest → Task 4; docs → Task 5. Known-limitations need no code.
- The examples/observed-models.json first draft inside Task 4 Step 3 contained an invalid `"observed"` key — the corrected second form is the one to ship (explicitly marked in the step).
- Type consistency: `diff_catalog(catalog: Catalog | None, observed: dict[str, list[str]])` (Tasks 1→4); report dataclass names identical; `_resolved_catalog_or_exit() -> tuple[Path, Catalog]` (Tasks 3→4); template resource path string identical in Tasks 2/3 and the wheel test.
- Fingerprint ordering pinned: `original` bytes are read BEFORE rendering; the mutate-mid-run test depends on it (noted in Task 4 Step 3).
