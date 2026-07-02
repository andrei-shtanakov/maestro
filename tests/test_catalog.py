"""Tests for the model catalog loader (ADR-ECO-003b)."""

from pathlib import Path

import pytest

from maestro.catalog import (
    Catalog,
    CatalogError,
    CatalogMalformed,
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
    assert not issubclass(HarnessModelUnresolved, CatalogError)


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


def test_fixture_matches_sibling_ssot() -> None:
    """When the sibling dev/ops SSOT exists, the fixture's routable defaults must
    still match it. Skipped in isolation (CI without the sibling repo). Seed of
    the ADR-003b cross-reader conformance test (shape only, not behavior)."""
    ssot = Path(__file__).parents[2] / "atp-platform" / "method" / "agents-catalog.toml"
    if not ssot.is_file():
        pytest.skip("sibling atp-platform SSOT not present")
    import tomllib

    data = tomllib.loads(ssot.read_text(encoding="utf-8"))
    cat = Catalog.model_validate(data)
    assert cat.default_model_for_harness("claude_code") == "claude-sonnet-4-6"
    assert cat.default_model_for_harness("codex_cli") == "gpt-5.5"
