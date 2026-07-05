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
