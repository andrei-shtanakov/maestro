"""LABS-88: CI guard against unreferenced public modules.

A maestro module that nothing imports is dead code that shipped. This guard
builds the import graph over ``maestro`` + ``tests`` (grimp resolves every
import form, including ``from maestro import submodule`` and relative imports)
and fails if any leaf module has zero importers and is not a legitimate root
(a console-script / entry-point / ``python -m`` module).

Detection is direct-reference (zero inbound imports), not reachability: a
born-dead single module — the v0.2.0 dogfood symptom — is caught. A dead
*cluster* (A imports B, both otherwise dead) is not, since B has importer A.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import grimp


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


_REPO_ROOT = Path(__file__).resolve().parent.parent

# Modules with no importers that are nevertheless legitimate — not dead code.
# Add here ONLY with a reason (a python -m entry point, a dynamically-imported
# plugin the graph cannot see, etc.).
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # `uv run python -m maestro.schemas.generate` — JSON-schema codegen
        # script, run manually / in docs, imported by nothing.
        "maestro.schemas.generate",
    }
)

# Leaf modules under these prefixes are excluded from the check entirely:
# vendored code (owned upstream) and non-package resource dirs.
_EXCLUDE_PREFIXES: tuple[str, ...] = ("maestro/_vendor/", "maestro/resources/")


def _find_unreferenced(
    modules: Iterable[str],
    importers_of: Callable[[str], set[str]],
    roots: set[str],
) -> list[str]:
    """Modules with no importer and not a root, sorted. Pure — unit-testable."""
    return sorted(m for m in modules if m not in roots and not importers_of(m))


def _roots_from_pyproject() -> set[str]:
    """Entry-point / console-script modules — referenced at runtime, not by an
    import statement. `maestro.cli` is always a root (the `maestro` script)."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    project = data.get("project", {})
    roots = {"maestro.cli"}
    for target in (project.get("scripts", {}) or {}).values():
        roots.add(target.split(":", 1)[0])
    for group in (project.get("entry-points", {}) or {}).values():
        for target in group.values():
            roots.add(target.split(":", 1)[0])
    return roots


def _leaf_modules() -> list[str]:
    """Dotted names of every maestro leaf module (a .py file that is not an
    __init__ and not under an excluded prefix)."""
    modules: list[str] = []
    for path in sorted((_REPO_ROOT / "maestro").rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if path.name == "__init__.py":
            continue
        if any(rel.startswith(p) for p in _EXCLUDE_PREFIXES):
            continue
        modules.append(".".join(path.relative_to(_REPO_ROOT).with_suffix("").parts))
    return modules


def test_find_unreferenced_flags_only_no_importer_non_roots() -> None:
    importers = {"maestro.used": {"maestro.cli"}, "maestro.dead": set()}
    roots = {"maestro.root"}
    result = _find_unreferenced(
        ["maestro.used", "maestro.dead", "maestro.root"],
        lambda m: importers.get(m, set()),
        roots,
    )
    assert result == ["maestro.dead"]  # used has an importer, root is a root


def test_no_unreferenced_maestro_modules() -> None:
    graph = grimp.build_graph("maestro", "tests")
    roots = _roots_from_pyproject() | _ALLOWLIST
    unreferenced = _find_unreferenced(
        _leaf_modules(), graph.find_modules_that_directly_import, roots
    )
    assert unreferenced == [], (
        "Unreferenced maestro modules (nothing imports them). Reference them, "
        "delete them, or — if a python -m / entry-point module — add to "
        f"_ALLOWLIST with a reason: {unreferenced}"
    )
