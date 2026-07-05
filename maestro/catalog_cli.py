"""`maestro models` — user-runtime catalog management (ADR-ECO-003b D3).

init scaffolds a user catalog from the shipped inert template; list shows
the resolved catalog; discover/update (added alongside) compare an observed
provider manifest and propose/apply Plane-1 additions. Plane 2/3 are never
written by any command here.
"""

import hashlib
import json
import os
import tempfile
import tomllib
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from maestro.catalog import (
    Catalog,
    CatalogMalformed,
    load_catalog,
    resolve_catalog_path,
)
from maestro.catalog_discovery import (
    DiscoveryReport,
    ManifestInvalid,
    diff_catalog,
    parse_observed_manifest,
    render_plane1_block,
)


models_app = typer.Typer(help="Manage the model catalog (ADR-ECO-003b D3).")
console = Console(width=200)
err_console = Console(stderr=True, width=200)


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
    if catalog is None:  # file vanished between is_file() and load
        err_console.print(
            f"[red]catalog configured at {path} but the file is missing[/red]"
            f" — run 'maestro models init --path {path}' or fix $ATP_CATALOG"
        )
        raise typer.Exit(1)
    return path, catalog


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
            f"{escape(repr(conflict.model_id))} is cataloged under "
            f"{escape(repr(conflict.catalog_vendor))} but observed under "
            f"{escape(repr(conflict.observed_vendor))} — not touching it"
        )
    if report.already_present:
        for hit in report.already_present:
            via = f" (alias of {escape(hit.matched)})" if hit.via_alias else ""
            console.print(f"already present: {escape(hit.model_id)}{via}")
    if report.deprecation_candidates:
        console.print(
            "[yellow]deprecation candidates[/yellow] "
            "(review by hand — this tool never edits existing entries):"
        )
        for cand in report.deprecation_candidates:
            console.print(f"  {escape(cand.model_id)} ({escape(cand.vendor)})")
    if report.new_models:
        console.print(f"new models: {len(report.new_models)}")


def _write_new_file_exclusive(target: Path, content: str) -> None:
    """Create `target` exclusively; refuses an existing file (no TOCTOU).

    `--out` is a regenerable proposal artifact: it needs EXCLUSIVITY, not
    atomicity. A single `open(..., "x")` makes the existence check and the
    write one operation, closing the check-then-replace race that a
    separate `exists()` + temp-file-replace dance would leave open.
    """
    try:
        with target.open("x", encoding="utf-8") as fh:
            fh.write(content)
    except FileExistsError:
        err_console.print(f"[red]{target} already exists[/red] — refusing to overwrite")
        raise typer.Exit(1) from None
    except OSError as exc:
        err_console.print(f"[red]cannot write {target}[/red]: {exc}")
        raise typer.Exit(1) from exc


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
        err_console.print("[red]no target[/red]: pass --path or set $ATP_CATALOG")
        raise typer.Exit(1)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create: existence check and write are one atomic
        # operation — no TOCTOU window.
        with target.open("x", encoding="utf-8") as fh:
            fh.write(_load_template())
    except FileExistsError:
        err_console.print(f"[red]{target} already exists[/red] — refusing to overwrite")
        raise typer.Exit(1) from None
    except OSError as exc:
        err_console.print(f"[red]cannot write {target}: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Catalog scaffolded:[/green] {target}")
    console.print("Next steps: edit the file (uncomment / add your models).")
    if os.environ.get("ATP_CATALOG") != str(target):
        console.print(f"Point all readers at it:\n  export ATP_CATALOG={target}")


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
    # Use console.file.write to bypass Rich's markup interpretation of [...]
    console.file.write(block)
    if out is not None:
        _write_new_file_exclusive(out, block)
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

    try:
        original = path.read_bytes()
    except OSError as exc:
        err_console.print(f"[red]cannot read {path}[/red]: {exc}")
        raise typer.Exit(1) from exc
    fingerprint = hashlib.sha256(original).hexdigest()

    block = render_plane1_block(report.new_models)
    # Use console.file.write to bypass Rich's markup interpretation of [...]
    console.file.write(block)
    if dry_run:
        console.print("[yellow]dry-run[/yellow]: no changes written")
        return
    if not yes and not typer.confirm(
        f"Append {len(report.new_models)} model(s) to {path}?"
    ):
        err_console.print("aborted — no changes written")
        raise typer.Exit(1)

    header = (
        f"\n# added by maestro models update {datetime.now(UTC).date().isoformat()}\n"
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
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    except OSError as exc:
        err_console.print(f"[red]cannot write {path}: {exc}[/red]")
        raise typer.Exit(1) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_content)
            fh.flush()
            os.fsync(fh.fileno())
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        if current != fingerprint:
            err_console.print("[red]catalog changed underneath us[/red] — re-run")
            raise typer.Exit(1)
        Path(tmp).replace(path)
    except OSError as exc:
        Path(tmp).unlink(missing_ok=True)
        err_console.print(f"[red]cannot write {path}: {exc}[/red]")
        raise typer.Exit(1) from exc
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    console.print(f"[green]added {len(report.new_models)} model(s) to {path}[/green]")
