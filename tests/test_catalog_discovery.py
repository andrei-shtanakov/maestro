"""Tests for the pure catalog-discovery diff logic (ADR-ECO-003b D3)."""

import tomllib

import pytest

from maestro.catalog import Catalog, CatalogModel
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
