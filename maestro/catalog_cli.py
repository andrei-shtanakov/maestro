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
        err_console.print("[red]no target[/red]: pass --path or set $ATP_CATALOG")
        raise typer.Exit(1)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Exclusive create: existence check and write are one atomic
        # operation — no TOCTOU window.
        with target.open("x", encoding="utf-8") as fh:
            fh.write(_load_template())
    except FileExistsError:
        err_console.print(f"[red]{target} already exists[/red] — refusing to overwrite")
        raise typer.Exit(1) from None
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
