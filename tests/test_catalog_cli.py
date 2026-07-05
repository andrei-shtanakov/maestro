"""Tests for the `maestro models` CLI (ADR-ECO-003b D3)."""

import subprocess
import tomllib
import zipfile
from importlib import resources
from pathlib import Path

import pytest
from typer.testing import CliRunner

from maestro.catalog import Catalog
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


class TestModelsInit:
    def test_init_writes_template_to_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATP_CATALOG", raising=False)
        target = tmp_path / "cat" / "agents-catalog.toml"
        result = runner.invoke(app, ["models", "init", "--path", str(target)])
        assert result.exit_code == 0
        assert "export ATP_CATALOG=" in result.output
        cat = Catalog.model_validate(tomllib.loads(target.read_text(encoding="utf-8")))
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

    def test_list_unset_env_hints_init(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
