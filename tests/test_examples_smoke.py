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
        # load_config wraps pydantic validation failures into ConfigError.
        load_config(path)  # raises ConfigError on schema drift


def test_observed_models_json_parses() -> None:
    data = json.loads((_EXAMPLES / "observed-models.json").read_text(encoding="utf-8"))
    parse_observed_manifest(data)  # raises on a malformed manifest


def test_smoke_rejects_a_broken_config() -> None:
    # Discrimination: an invalid config must be rejected, so the per-example
    # smoke genuinely catches drift rather than passing vacuously. ProjectConfig
    # requires `repo` (and `project`); a mapping missing `repo` must fail.
    # load_config_from_string wraps the pydantic error into ConfigError.
    with pytest.raises(ConfigError):
        load_config_from_string("project: broken\n")
