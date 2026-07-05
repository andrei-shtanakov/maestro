"""Pure diff logic for `maestro models discover|update` (ADR-ECO-003b D3).

Reimplements the semantics of the dev prototype
(`devtools/discover_models.py` in the coordination workspace) for the
USER-runtime catalog. Shipped code: no references to the workspace.

Planes: discovery reads/writes Plane 1 only (model existence). Plane 2/3
(harnesses, enrollment, routable) are never touched — routing promotion is
a separate benchmark-gated process upstream.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from maestro.catalog import Catalog


# Vendors whose provider listings mean something for deprecation. Local /
# baseline models (ollama, meta llama) are never deprecation candidates —
# there is no "provider offering" for them to fall out of.
CHECKABLE_VENDORS: frozenset[str] = frozenset(
    {"anthropic", "openai", "deepseek", "xiaomi", "alibaba", "zhipu"}
)


class ManifestInvalid(ValueError):
    """The observed-models manifest violates its contract."""


def parse_observed_manifest(data: object) -> dict[str, list[str]]:
    """Validate and normalize an observed-models manifest.

    Contract:
      * top level must be a JSON object;
      * ``_meta`` (any type) is ignored entirely;
      * every other key is a vendor whose value must be a list of
        non-empty strings — an EMPTY list is valid and means "vendor
        observed, offers no models";
      * duplicate ids within a vendor are deduplicated, first occurrence
        wins, order preserved; no case normalization.

    Raises:
        ManifestInvalid: naming the offending key.
    """
    if not isinstance(data, dict):
        raise ManifestInvalid("manifest top level must be a JSON object")
    observed: dict[str, list[str]] = {}
    for vendor, raw in data.items():
        if vendor == "_meta":
            continue
        if not isinstance(raw, list):
            raise ManifestInvalid(
                f"vendor {vendor!r}: value must be a list of model ids"
            )
        seen: dict[str, None] = {}
        for item in raw:
            if not isinstance(item, str) or not item:
                raise ManifestInvalid(
                    f"vendor {vendor!r}: model ids must be non-empty strings"
                )
            seen.setdefault(item)
        observed[vendor] = list(seen)
    return observed


@dataclass(frozen=True)
class NewModel:
    model_id: str
    vendor: str


@dataclass(frozen=True)
class AlreadyPresent:
    model_id: str
    matched: str
    via_alias: bool


@dataclass(frozen=True)
class VendorConflict:
    model_id: str
    catalog_vendor: str
    observed_vendor: str


@dataclass(frozen=True)
class DeprecationCandidate:
    model_id: str
    vendor: str


@dataclass
class DiscoveryReport:
    """Outcome of diffing an observed manifest against the user catalog."""

    new_models: list[NewModel] = field(default_factory=list)
    deprecation_candidates: list[DeprecationCandidate] = field(default_factory=list)
    already_present: list[AlreadyPresent] = field(default_factory=list)
    vendor_conflicts: list[VendorConflict] = field(default_factory=list)


def diff_catalog(
    catalog: Catalog | None, observed: dict[str, list[str]]
) -> DiscoveryReport:
    """Diff observed provider offerings against catalog Plane 1.

    Deprecation candidates require the vendor to be PRESENT as a manifest
    key (missing key = "vendor not observed this run" — a partial manifest
    must never mass-flag a vendor), checkable, the entry active, and
    neither the id nor any of its aliases observed for that vendor.
    """
    models = catalog.models if catalog is not None else {}
    alias_owner: dict[str, str] = {}
    for key, entry in models.items():
        for alias in entry.aliases:
            alias_owner.setdefault(alias, key)

    report = DiscoveryReport()
    for vendor in sorted(observed):
        for model_id in observed[vendor]:
            entry = models.get(model_id)
            if entry is not None:
                if entry.vendor != vendor:
                    report.vendor_conflicts.append(
                        VendorConflict(model_id, entry.vendor, vendor)
                    )
                else:
                    report.already_present.append(
                        AlreadyPresent(model_id, model_id, via_alias=False)
                    )
            elif model_id in alias_owner:
                report.already_present.append(
                    AlreadyPresent(model_id, alias_owner[model_id], via_alias=True)
                )
            else:
                report.new_models.append(NewModel(model_id, vendor))

    for key in sorted(models):
        entry = models[key]
        if (
            entry.vendor in CHECKABLE_VENDORS
            and entry.vendor in observed
            and entry.status == "active"
            and key not in observed[entry.vendor]
            and not any(a in observed[entry.vendor] for a in entry.aliases)
        ):
            report.deprecation_candidates.append(
                DeprecationCandidate(key, entry.vendor)
            )
    return report


def _toml_basic_string(value: str) -> str:
    """Render a TOML basic string (double-quoted, fully escaped).

    TOML requires escaping of quote, backslash, and all control characters
    (U+0000..U+001F, U+007F) inside basic strings.
    """
    short = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
    out = ['"']
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch in short:
            out.append(short[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def render_plane1_block(new_models: Sequence[NewModel]) -> str:
    """Ready-to-append Plane-1 TOML for the given new models.

    Stable ordering (vendor, then id); ids and vendors escaped as TOML
    basic strings so arbitrary model ids survive a round-trip.
    """
    lines: list[str] = []
    for nm in sorted(new_models, key=lambda n: (n.vendor, n.model_id)):
        lines.append(f"[models.{_toml_basic_string(nm.model_id)}]")
        lines.append(f"vendor = {_toml_basic_string(nm.vendor)}")
        lines.append('status = "active"')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
