"""Model catalog loader and resolution (ADR-ECO-003b).

The catalog is user configuration, not shipped in the package and not vendored
for runtime use. It is resolved from $ATP_CATALOG (XDG default path is a
follow-up). There is no baked default model: when no catalog and no override
supply a model, resolution fails loud.

Fault taxonomy is split by blast radius:
  * CatalogError (CatalogNotConfigured / CatalogMalformed) — the catalog is
    unusable for everyone; the scheduler halts the whole run.
  * HarnessModelUnresolved — this one harness cannot resolve a default; the
    scheduler sends only that task to NEEDS_REVIEW and keeps running. It is
    deliberately NOT a CatalogError.
"""

from __future__ import annotations

import difflib
import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from maestro._vendor import obs


_obs_log = obs.get_logger("maestro.catalog")

_NOT_CONFIGURED_MSG = (
    "model catalog not configured: set $ATP_CATALOG (or run 'atp models init')"
)


class CatalogError(RuntimeError):
    """Global catalog fault — the catalog is unusable for everyone. Halts the run."""


class CatalogNotConfigured(CatalogError):
    """No catalog configured and a default is needed."""


class CatalogMalformed(CatalogError):
    """$ATP_CATALOG set, file present, but corrupt / schema-invalid."""


class HarnessModelUnresolved(RuntimeError):
    """No routable model, or >1, for this harness. Per-task; NOT a CatalogError."""


class CatalogModel(BaseModel):
    """Plane 1 model entry."""

    vendor: str
    status: Literal["active", "deprecated", "retired"] = "active"
    aliases: list[str] = []


class CatalogAgent(BaseModel):
    """Plane 3 enrollment entry (harness, model) pair."""

    harness: str
    model: str
    tested: bool = False
    routable: bool = False


class Catalog(BaseModel):
    """Parsed catalog. Plane 2 (harnesses) is ignored — Maestro does not need it."""

    models: dict[str, CatalogModel]
    agents: list[CatalogAgent] = []

    def default_model_for_harness(self, harness: str) -> str:
        """Model of the single routable [[agents]] entry for this harness.

        Raises HarnessModelUnresolved (per-task) when there is no routable entry,
        or more than one (the ADR-003a A/B window).
        """
        routable = [a.model for a in self.agents if a.harness == harness and a.routable]
        if len(routable) == 1:
            return routable[0]
        if not routable:
            raise HarnessModelUnresolved(
                f"catalog has no routable model for harness '{harness}'; "
                f"set MAESTRO_{harness.upper()}_MODEL"
            )
        raise HarnessModelUnresolved(
            f"ambiguous default for harness '{harness}': {len(routable)} "
            f"routable models ({', '.join(routable)}); set "
            f"MAESTRO_{harness.upper()}_MODEL"
        )

    def status_of(self, model: str) -> str | None:
        """Status of a model id, resolving aliases. None means unknown."""
        entry = self.models.get(model)
        if entry is not None:
            return entry.status
        for m in self.models.values():
            if model in m.aliases:
                return m.status
        return None

    def nearest_models(self, model: str, n: int = 3) -> list[str]:
        """Closest known model ids, for warning payloads."""
        return difflib.get_close_matches(model, list(self.models), n=n, cutoff=0.3)


def resolve_catalog_path() -> Path | None:
    """Resolve the catalog file path. $ATP_CATALOG only for now.

    XDG default path ($XDG_CONFIG_HOME/<eco>/agents-catalog.toml) is a follow-up
    gated on the ratified <eco> namespace.
    """
    env_path = os.environ.get("ATP_CATALOG")
    return Path(env_path) if env_path else None


def load_catalog() -> Catalog | None:
    """Load and validate the catalog.

    Returns None for both "no catalog" cases: $ATP_CATALOG unset, or set but the
    file is absent (a path typo must not crash a routed run). Raises
    CatalogMalformed when the file is present but corrupt / schema-invalid.
    """
    path = resolve_catalog_path()
    if path is None:
        return None
    if not path.is_file():
        _obs_log.info("catalog.path_absent", path=str(path))
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return Catalog.model_validate(data)
    except (tomllib.TOMLDecodeError, ValidationError, OSError) as exc:
        raise CatalogMalformed(f"catalog is corrupt ({path}): {exc}") from exc


def resolve_model(
    routed: str | None,
    env_var: str,
    harness: str,
    catalog: Catalog | None,
) -> tuple[str, str]:
    """Resolve the model to run and its source. Precedence: routed > env >
    catalog-default. An empty ``routed`` is treated as absent (guards against a
    degenerate ``"<harness>@"`` id producing an empty ``--model``).

    Raises CatalogNotConfigured (GLOBAL → halt) when the default path is reached
    with no catalog. Propagates HarnessModelUnresolved (PER-TASK) from
    default_model_for_harness for no-routable / ambiguous harnesses.
    """
    if routed:
        return routed, "routed"
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val, "env"
    if catalog is None:
        raise CatalogNotConfigured(_NOT_CONFIGURED_MSG)
    return catalog.default_model_for_harness(harness), "catalog"


def warn_on_model_status(model: str, source: str, catalog: Catalog | None) -> None:
    """Coherence check against the catalog only (never provider reality — that is
    the CLI's job). No-op when catalog is None. Grades by status: retired → loud,
    deprecated → light, active → silent — for every source. The unknown → soft
    branch is the only source-gated one (skipped for source == 'catalog', where
    membership is tautological). Never blocks the spawn.
    """
    if catalog is None:
        return
    status = catalog.status_of(model)
    if status == "retired":
        _obs_log.warning(
            "agent.model_retired",
            model=model,
            source=source,
            nearest=catalog.nearest_models(model),
        )
    elif status == "deprecated":
        _obs_log.warning("agent.model_deprecated", model=model, source=source)
    elif status is None and source != "catalog":
        _obs_log.info(
            "agent.model_unknown",
            model=model,
            source=source,
            nearest=catalog.nearest_models(model),
        )
    # active → silent
