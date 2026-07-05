# `maestro models init|list|discover|update` — design

**Date:** 2026-07-05
**Status:** approved
**Context:** ADR-ECO-003b D3 ("CLI управления — discovery для пользователя");
TODO.md "Catalog distribution follow-ups". First shipped implementation of D3
in the ecosystem (ATP's `atp models` does not exist yet). The discover logic
ports the semantics of the dev prototype `_cowork_output/devtools/
discover_models.py` INTO the shipped package (ADR-sanctioned move; shipped
code must never reference `_cowork_output/` — this is a reimplementation, not
an import).

## Goal

User-facing catalog management for Maestro's model catalog (the user-runtime
catalog of ADR-003b D4 — NOT the dev/ops SSOT):

- `init` — scaffold a starter user catalog from an inert shipped template.
- `list` — show what the resolved catalog contains and where it came from.
- `discover` — compare an observed provider-model manifest against the user
  catalog; report new models / deprecation candidates; propose a Plane-1 TOML
  block. Read-only.
- `update` — the write-side of discover (user decision 2026-07-05): append
  the proposed Plane-1 blocks to the user catalog, with dry-run and explicit
  confirmation.

## Decisions locked during brainstorm

- **`update` semantics:** apply discover's proposals (new Plane-1 model
  entries) to the USER catalog. Never touches existing entries, never touches
  Plane 2/3 (`[[agents]]`, harness, routable) — routing promotion stays
  benchmark-gated upstream.
- **`init` target while the `<eco>` XDG namespace is unratified:** `--path`
  or `$ATP_CATALOG`; no self-invented XDG default. The canonical XDG path is
  a separate ticket after ratification (extends `resolve_catalog_path`, no
  migration debt created here).

## Architecture

Two new modules + a Typer sub-app; `cli.py` gains only the registration line.

- `maestro/catalog_discovery.py` — pure, testable logic:
  - `ObservedManifest`: parsed `{vendor: [model_id, ...]}` JSON (the
    prototype's offline format; `_meta` key ignored).
  - `CHECKABLE_VENDORS = {"anthropic", "openai", "deepseek", "xiaomi",
    "alibaba", "zhipu"}` — vendors whose provider listings mean something;
    local/baseline models (ollama, meta) are never deprecation candidates
    because there is no "provider offering" to fall out of.
  - `diff_catalog(catalog: Catalog | None, observed: ObservedManifest) ->
    DiscoveryReport` where `DiscoveryReport` carries `new_models`
    (observed, not in catalog Plane 1) and `deprecation_candidates`
    (in catalog with a checkable vendor + status active, absent from that
    vendor's observed list). A None/empty catalog → everything observed is
    new.
  - `render_plane1_block(new_models) -> str` — ready-to-append TOML
    (`[models."<id>"]` with `vendor` and `status = "active"`), one block,
    stable ordering (by vendor, then id).
- `maestro/catalog_cli.py` — `models_app = typer.Typer()` with the four
  commands (Rich output, matching the existing CLI style).
- `maestro/cli.py` — `app.add_typer(models_app, name="models",
  help="Manage the model catalog (ADR-ECO-003b D3)")`.
- `maestro/resources/agents-catalog-template.toml` — shipped inert template,
  loaded via `importlib.resources` (package `maestro.resources` gets an
  `__init__.py`). Content: header comment explaining the schema and the
  three-reader contract ($ATP_CATALOG shared by Maestro/ATP/arbiter), plus
  FULLY COMMENTED-OUT example `[models."..."]` and `[[agents]]` entries.
  Invariant (tested): the template parses as valid TOML and validates into
  an EMPTY `Catalog` (no models, no agents) — a fresh init resolves nothing,
  preserving fail-loud and shipping no active-model endorsement in the wheel
  (ADR-003b: "никаких активных моделей внутри wheel").

## Commands

### `maestro models init [--path FILE]`

1. Target := `--path`, else `$ATP_CATALOG`, else error:
   "no target: pass --path or set $ATP_CATALOG" (exit 1).
2. Target exists → refuse with message, exit 1 (no `--force`; YAGNI).
3. Write the template; create parent dirs.
4. Print next steps: edit the file (uncomment/add models), and if `--path`
   was used, `export ATP_CATALOG=<path>` so all three readers find it.

### `maestro models list`

1. `resolve_catalog_path()` — unset → friendly message pointing at
   `maestro models init` (exit 1).
2. `load_catalog()` — malformed → surface the `CatalogMalformed` message
   (exit 1).
3. Print: path + source (`$ATP_CATALOG`), models table (id, vendor, status —
   deprecated/retired visually flagged), agents table (harness, model,
   tested, routable). Empty catalog → explicit "catalog is empty — edit
   <path> or run `maestro models discover`" note, exit 0.

### `maestro models discover --observed FILE [--out FILE]`

1. Load user catalog (not-configured / malformed → exit 1 with message).
   An EMPTY catalog is fine (fresh init) — everything observed is new.
2. Parse the observed manifest (JSON `{vendor: [ids]}`; `_meta` ignored;
   malformed → exit 1).
3. `diff_catalog` → print a report: new models (per vendor), deprecation
   candidates (with "review by hand — discover never edits" note), summary.
4. New models exist → print the proposed Plane-1 block; `--out FILE` writes
   it (never the catalog itself). Exit 2.
5. No new models → "catalog is up to date", exit 0. (Deprecation candidates
   alone do not change the exit code — same as the prototype.)

### `maestro models update --observed FILE [--dry-run] [--yes]`

1. Same load/parse as discover; catalog must be configured (a writable file
   path is required — this command edits it).
2. No new models → "nothing to apply", exit 0 (idempotent re-runs).
3. `--dry-run` → print the block that WOULD be appended, exit 0, no write.
4. Otherwise show the block and confirm (`typer.confirm`); `--yes` skips the
   prompt (CI/scripting). Declined → exit 1, no write.
5. Append the block to the catalog file (plain text append with a dated
   comment header `# added by maestro models update <ISO-date>`). Existing
   content is never modified; `[[agents]]` never touched.
6. Re-validate: `load_catalog()` must succeed after the write; if it fails,
   restore the pre-write content (kept in memory) and exit 1 loudly — an
   update must never leave the catalog corrupt.

## Side fix in scope

`maestro/catalog.py:32` — the not-configured message currently suggests
`atp models init` (written before Maestro had the command). Change to
`maestro models init`. Its assertion in tests updates accordingly.

## Error handling / edges

- All four commands are Mode-agnostic (no DB, no scheduler): pure
  file/console operations, synchronous.
- `update` with `--observed` naming a model whose id already exists in the
  catalog but with a DIFFERENT vendor: it is not "new" (Plane-1 key is the
  model id) — not added, listed in the report as already-present. Vendor
  conflicts are a human problem; the tool never rewrites existing entries.
- Observed manifest with a vendor not in CHECKABLE_VENDORS: its models can
  still be NEW (addition is allowed for any vendor); the vendor gate applies
  only to DEPRECATION candidates.
- Non-UTF8 / non-JSON manifest, TOML-invalid catalog: exit 1 with the
  underlying message; never a traceback.

## Testing

- `tests/test_catalog_discovery.py` (pure logic): new-model detection;
  deprecation candidates gated on CHECKABLE_VENDORS + active status;
  deprecated/retired entries are not re-proposed as new; empty/None catalog;
  malformed manifest raises the typed error; `render_plane1_block` output
  parses as TOML and round-trips through `Catalog` validation; stable
  ordering.
- `tests/test_catalog_cli.py` (Typer CliRunner): init (writes template to
  --path / to $ATP_CATALOG / refuses overwrite / no target → exit 1 + hint);
  list (renders tables / not-configured hint mentions `maestro models init` /
  empty-catalog note); discover (exit 0 up-to-date / exit 2 with proposed
  block / --out writes block, catalog untouched / exit 1 malformed);
  update (dry-run no-write / --yes appends + re-load validates + second run
  is a no-op / declined confirm → no write / corrupt-after-write restore
  path via monkeypatched load_catalog).
- Template invariant test: shipped template loads via importlib.resources,
  parses, validates to an empty Catalog.
- `catalog.py` message test updated (`maestro models init`).

## Out of scope

- XDG default path (`$XDG_CONFIG_HOME/<eco>/agents-catalog.toml`) — gated on
  `<eco>` namespace ratification; separate TODO item stays open.
- Live provider adapters (anthropic/openai /v1/models) — stubs even in the
  prototype; require keys. The offline manifest is the only source.
- Status sync of existing entries (deprecation stays a report-only proposal).
- Shared PyPI loader lib + cross-reader conformance test (separate TODO item).
- Removing the dev prototype from `_cowork_output/devtools/` (workspace/PM
  owns it; it serves the SSOT loop per D4, which is a different catalog).
