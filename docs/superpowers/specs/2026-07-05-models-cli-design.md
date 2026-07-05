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
    prototype's offline format). Validation contract:
    - `_meta` (any JSON type) is ignored entirely.
    - Every other top-level key is a vendor; its value MUST be a list of
      non-empty strings, else a typed `ManifestInvalid` error (CLI → exit 1
      with the offending key named). Empty list is VALID and meaningful:
      "vendor observed, offers no models".
    - Duplicate ids within a vendor list are deduplicated (first occurrence
      wins, order preserved); vendor keys and ids are used verbatim (no case
      normalization).
  - `CHECKABLE_VENDORS = {"anthropic", "openai", "deepseek", "xiaomi",
    "alibaba", "zhipu"}` — vendors whose provider listings mean something;
    local/baseline models (ollama, meta) are never deprecation candidates
    because there is no "provider offering" to fall out of.
  - `diff_catalog(catalog: Catalog | None, observed: ObservedManifest) ->
    DiscoveryReport`. Report fields:
    - `new_models` — observed ids matching NEITHER a Plane-1 key NOR any
      existing model's `aliases` entry (an observed id that is an alias of
      a cataloged model is NOT new — it must not be re-added as a separate
      model).
    - `deprecation_candidates` — cataloged models whose vendor is checkable
      AND **present as a key in the manifest** AND status is active AND the
      id is absent from that vendor's observed list. A vendor key MISSING
      from the manifest means "vendor not observed this run" and produces NO
      candidates for that vendor — a partial manifest must never mass-flag
      an entire vendor. (Empty list = observed-and-empty DOES produce
      candidates; that is the meaningful case.)
    - `already_present` — observed ids that matched a Plane-1 key or an
      alias (with which model they matched).
    - `vendor_conflicts` — observed ids whose Plane-1 entry exists but under
      a DIFFERENT vendor than the manifest claims. Never auto-touched;
      rendered as a prominent warning in both discover and update output.
    A None/empty catalog → everything observed is new.
  - `render_plane1_block(new_models) -> str` — ready-to-append TOML
    (`[models."<id>"]` with `vendor` and `status = "active"`), one block,
    stable ordering (by vendor, then id). Model ids and vendor values are
    escaped as TOML basic strings (quotes, backslashes, control characters,
    non-ASCII must survive round-trip); tests cover `"`, `\`, Unicode, and
    control chars, not just friendly identifiers.
- `maestro/catalog_cli.py` — `models_app = typer.Typer()` with the four
  commands (Rich output, matching the existing CLI style).
- `maestro/cli.py` — `app.add_typer(models_app, name="models",
  help="Manage the model catalog (ADR-ECO-003b D3)")`.
- `maestro/resources/agents-catalog-template.toml` — shipped inert template,
  loaded via `importlib.resources` (package `maestro.resources` gets an
  `__init__.py`). Content: header comment explaining the schema and the
  three-reader contract ($ATP_CATALOG shared by Maestro/ATP/arbiter), plus
  FULLY COMMENTED-OUT example `[models."..."]` and `[[agents]]` entries,
  plus one UNCOMMENTED bare `[models]` table header — `Catalog.models` is a
  required field, so the empty table is schema scaffolding the loader needs
  (it is not an active-model endorsement).
  Invariant (tested): the template parses as valid TOML and validates into
  an EMPTY `Catalog` (no models, no agents) — a fresh init resolves nothing,
  preserving fail-loud and shipping no active-model endorsement in the wheel
  (ADR-003b: "никаких активных моделей внутри wheel").
- **Packaging:** `pyproject.toml` gains explicit package data —
  `[tool.setuptools.package-data]` with `"maestro.resources" = ["*.toml"]`
  (setuptools `packages.find` picks the package up once it has
  `__init__.py`, but data files need the explicit entry). Two-level
  verification: a regular test loads the template via `importlib.resources`
  (source tree), and a `@pytest.mark.slow` test builds a wheel
  (`uv build --wheel`) and asserts via `zipfile` that
  `maestro/resources/agents-catalog-template.toml` is inside — the only
  proof the resource actually ships.

## Commands

### Catalog resolution in the CLI (shared by list/discover/update)

`load_catalog()` returns None for BOTH "env unset" and "env set but file
missing" (`catalog.py:120`) — the CLI must distinguish them itself:

- `$ATP_CATALOG` unset → "no catalog configured — run `maestro models init`"
  (exit 1).
- Set but the file does not exist → "catalog configured at <path> but the
  file is missing — run `maestro models init --path <path>` or fix
  $ATP_CATALOG" (exit 1, names the exact path).
- Present but malformed → surface the `CatalogMalformed` message (exit 1).

### `maestro models init [--path FILE]`

1. Target := `--path`, else `$ATP_CATALOG`, else error:
   "no target: pass --path or set $ATP_CATALOG" (exit 1).
2. Create parent dirs, then create the file EXCLUSIVELY (`open(..., "x")`)
   — the existence check and the write are one atomic operation, no TOCTOU
   window. `FileExistsError` → refuse with message, exit 1 (no `--force`;
   YAGNI).
3. Print next steps: edit the file (uncomment/add models), and if `--path`
   was used, `export ATP_CATALOG=<path>` so all three readers find it.

### `maestro models list`

1. Resolution per the shared contract above.
2. Print: path + source (`$ATP_CATALOG`), models table (id, vendor, status —
   deprecated/retired visually flagged), agents table (harness, model,
   tested, routable). Empty catalog → explicit "catalog is empty — edit
   <path> or run `maestro models discover`" note, exit 0.

### `maestro models discover --observed FILE [--out FILE]`

1. Resolution per the shared contract; an EMPTY catalog is fine (fresh
   init) — everything observed is new.
2. Parse the observed manifest per the ObservedManifest contract
   (`ManifestInvalid` → exit 1 naming the offending key).
3. `diff_catalog` → print the report: new models (per vendor), deprecation
   candidates (with "review by hand — discover never edits" note),
   already-present matches, vendor-conflict WARNINGS, summary.
4. New models exist → print the proposed Plane-1 block; `--out FILE` writes
   it atomically (temp file + `os.replace`), REFUSING an existing target
   (exit 1) — never the catalog itself. Exit 2.
5. No new models → "catalog is up to date", exit 0. (Deprecation candidates
   and vendor conflicts alone do not change the exit code — same as the
   prototype.)

**Exit codes are a public CLI contract**, documented in the command help
text: 0 = up to date, 2 = new models found (not an error — CI signal),
1 = error. Same contract for `discover`; `update` uses plain 0/1.

### `maestro models update --observed FILE [--dry-run] [--yes]`

1. Same resolution/parse as discover; catalog must be configured (a
   writable file path is required — this command edits it).
2. No new models → "nothing to apply", exit 0 (idempotent re-runs).
3. `--dry-run` → print the block that WOULD be appended, exit 0, no write.
4. Otherwise show the block (and any vendor-conflict warnings) and confirm
   (`typer.confirm`); `--yes` skips the prompt (CI/scripting). Declined →
   exit 1, no write.
5. **Write strategy — validate-then-replace, not append-then-rollback:**
   1. Read the current catalog bytes; record a fingerprint (sha256).
   2. Compose the FULL new content in memory: original bytes + a dated
      comment header (`# added by maestro models update <ISO-date>`) + the
      rendered Plane-1 block. Existing bytes are preserved verbatim as the
      prefix; entries are only ever added logically, never rewritten.
   3. Validate the NEW content in memory (parse TOML → `Catalog`
      validation) BEFORE anything touches disk. Invalid → exit 1, file
      untouched.
   4. Write the new content to a temp file in the SAME directory, fsync,
      then re-check the fingerprint of the target (a concurrent writer
      changed it → abort with "catalog changed underneath us, re-run",
      exit 1) and `os.replace` the temp over the catalog.
   This bounds the failure modes: a crash leaves either the old file or the
   new valid file (replace is atomic on POSIX); a concurrent modification
   is detected by the fingerprint re-check (best-effort lost-update guard,
   not a distributed lock — stated as such); partial writes can only hit
   the temp file. `[[agents]]` is never touched.

## Side fix in scope

`maestro/catalog.py:32` — the not-configured message currently suggests
`atp models init` (written before Maestro had the command). Change to
`maestro models init`. Its assertion in tests updates accordingly.

## Error handling / edges

- All four commands are Mode-agnostic (no DB, no scheduler): pure
  file/console operations, synchronous.
- Vendor conflict (observed id exists in Plane 1 under a different vendor):
  never added, never rewritten — reported in `vendor_conflicts` and rendered
  as a prominent warning; resolution is the user's editorial decision.
- Alias hit (observed id appears in some model's `aliases`): not new — the
  canonical entry already covers it; reported in `already_present`.
- Observed manifest with a vendor not in CHECKABLE_VENDORS: its models can
  still be NEW (addition is allowed for any vendor); the vendor gate applies
  only to DEPRECATION candidates.
- Non-UTF8 / non-JSON manifest, TOML-invalid catalog: exit 1 with the
  underlying message; never a traceback.

## Known accepted limitations

- The fingerprint re-check before `os.replace` is a best-effort lost-update
  guard for the "two updates racing" case, not an advisory-lock protocol;
  a writer that lands in the microseconds between re-check and replace can
  still be lost. Acceptable for a human-edited config file.
- The observed manifest carries no per-provider collection status; a
  MISSING vendor key is the only "not observed" signal (contract above).
  Richer `_meta` per-provider status (collected_at, ok/failed) is a
  follow-up once a real collector exists — today manifests are hand-filled.

## Testing

- `tests/test_catalog_discovery.py` (pure logic): new-model detection;
  alias hit is NOT new (lands in already_present); vendor conflict detected
  and never proposed; deprecation candidates gated on CHECKABLE_VENDORS +
  active status + **vendor key present in manifest** (missing vendor key →
  zero candidates for that vendor; empty list → candidates produced);
  deprecated/retired entries are not re-proposed as new; empty/None catalog;
  manifest validation (non-list vendor value, non-string/empty-string id,
  in-vendor duplicates deduped, `_meta` of any type ignored) raises/behaves
  per contract; `render_plane1_block` output parses as TOML and round-trips
  through `Catalog` validation, including ids/vendors containing `"`, `\`,
  Unicode, and control characters; stable ordering.
- `tests/test_catalog_cli.py` (Typer CliRunner): init (writes template to
  --path / to $ATP_CATALOG / refuses existing file / no target → exit 1 +
  hint); list (renders tables / unset → init hint / set-but-missing file →
  message naming the exact path / empty-catalog note); discover (exit 0
  up-to-date / exit 2 with proposed block / --out writes atomically and
  refuses an existing target / catalog untouched / exit 1 malformed
  manifest); update (dry-run no-write / --yes: new content validated
  in-memory then atomically replaced, second run is a no-op / declined
  confirm → no write / in-memory validation failure → exit 1 and file
  byte-identical / fingerprint mismatch (file mutated between read and
  replace, simulated in-test) → abort exit 1).
- Template invariant test: shipped template loads via importlib.resources,
  parses, validates to an empty Catalog.
- Wheel packaging test (`@pytest.mark.slow`): `uv build --wheel` + zipfile
  assertion that the template ships.
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
