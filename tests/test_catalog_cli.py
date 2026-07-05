"""Tests for the `maestro models` CLI (ADR-ECO-003b D3)."""

import json
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

    def test_init_unwritable_parent_fails_with_message_not_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read-only parent dir must produce a clean exit 1, never a
        propagated OSError traceback."""
        monkeypatch.delenv("ATP_CATALOG", raising=False)
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o555)
        target = ro / "sub" / "cat.toml"
        try:
            result = runner.invoke(app, ["models", "init", "--path", str(target)])
        finally:
            ro.chmod(0o755)
        assert result.exit_code == 1
        assert "cannot write" in result.output
        assert str(target) in result.output
        assert "Traceback" not in result.output


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


OBSERVED_UP_TO_DATE = {"openai": ["gpt-5.5"]}
OBSERVED_WITH_NEW = {"openai": ["gpt-5.5", "gpt-6"]}


def _write_manifest(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestModelsDiscover:
    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        target = tmp_path / "agents-catalog.toml"
        _write_catalog(target, MINIMAL_CATALOG)
        monkeypatch.setenv("ATP_CATALOG", str(target))
        return target

    def test_up_to_date_exit_0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup(tmp_path, monkeypatch)
        manifest = _write_manifest(tmp_path / "obs.json", OBSERVED_UP_TO_DATE)
        result = runner.invoke(app, ["models", "discover", "--observed", str(manifest)])
        assert result.exit_code == 0
        assert "up to date" in result.output

    def test_new_models_exit_2_with_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog_path = self._setup(tmp_path, monkeypatch)
        before = catalog_path.read_text(encoding="utf-8")
        manifest = _write_manifest(tmp_path / "obs.json", OBSERVED_WITH_NEW)
        result = runner.invoke(app, ["models", "discover", "--observed", str(manifest)])
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

    def test_out_nonexistent_parent_fails_with_message_not_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--out into a directory that doesn't exist must exit 1 with a
        message naming the target, never a raw FileNotFoundError traceback."""
        self._setup(tmp_path, monkeypatch)
        manifest = _write_manifest(tmp_path / "obs.json", OBSERVED_WITH_NEW)
        out = tmp_path / "nonexistent-dir" / "block.toml"
        result = runner.invoke(
            app,
            ["models", "discover", "--observed", str(manifest), "--out", str(out)],
        )
        assert result.exit_code == 1
        assert "cannot write" in result.output
        assert str(out) in result.output
        assert "Traceback" not in result.output

    def test_malformed_manifest_exit_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup(tmp_path, monkeypatch)
        manifest = _write_manifest(tmp_path / "obs.json", {"openai": "gpt"})
        result = runner.invoke(app, ["models", "discover", "--observed", str(manifest)])
        assert result.exit_code == 1
        assert "openai" in result.output

    def test_vendor_conflict_warned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup(tmp_path, monkeypatch)
        manifest = _write_manifest(tmp_path / "obs.json", {"someone-else": ["gpt-5.5"]})
        result = runner.invoke(app, ["models", "discover", "--observed", str(manifest)])
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
            lambda _: "[models.unclosed\n",
        )
        result = runner.invoke(
            app, ["models", "update", "--observed", str(manifest), "--yes"]
        )
        assert result.exit_code == 1
        assert target.read_bytes() == before

    def test_readonly_parent_fails_with_message_not_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A catalog whose parent dir becomes read-only after setup must
        exit 1 with a message naming the path, never a raw OSError
        traceback, and must leave the catalog byte-identical."""
        target, manifest = self._setup(tmp_path, monkeypatch)
        before = target.read_bytes()
        target.parent.chmod(0o555)
        try:
            result = runner.invoke(
                app, ["models", "update", "--observed", str(manifest), "--yes"]
            )
        finally:
            target.parent.chmod(0o755)
        assert result.exit_code == 1
        assert "cannot write" in result.output
        assert str(target) in result.output
        assert "Traceback" not in result.output
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

        def mutate_then_render(new_models):
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
