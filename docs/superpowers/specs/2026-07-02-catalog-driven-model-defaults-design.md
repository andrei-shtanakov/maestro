# Design: Catalog-Driven Model Defaults (AI#4)

**Date:** 2026-07-02
**Status:** Approved (brainstorm), pending implementation plan
**Branch:** `adr-eco-003/maestro-catalog-defaults`
**Implements:** ADR-ECO-003 AI#4, as amended by ADR-ECO-003a (discovery→adoption)
and **ADR-ECO-003b (catalog distribution)** — the amendment that changes the
approach from "vendor + codegen a constant" to "runtime loader, no baked default".

## Context

Maestro's two model-aware spawners currently hardcode their default model:

- `maestro/spawners/claude_code.py:21` — `DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"`
- `maestro/spawners/codex.py:21` — `DEFAULT_CODEX_MODEL = "gpt-5.5"`

`resolve_model(routed, env_var, default)` (landed in PR #35, `spawners/base.py:27`)
already implements the precedence `routed > env > default`; AI#4 only needs to
replace the `default` layer's source.

**ADR-003b changes the target.** The model catalog is *not* shipped in the
package and *not* vendored into git for runtime use. It is **user configuration**
resolved at runtime, with **no baked default** (`claude-sonnet-*` must not appear
anywhere in the Maestro package). The dev/ops SSOT
(`atp-platform/method/agents-catalog.toml`) remains the authority for the
ecosystem benchmark→routing loop, but an installed Maestro must **not** read it —
it reads the user-config catalog. Maestro's explicit 003b action (line 150-152):

> Читать тот же user-config путь для `DEFAULT_<H>_MODEL` вместо хардкода;
> fail-loud при отсутствии.

## Decisions (locked during brainstorm)

1. **Approach:** runtime catalog **loader**, no baked default, no package-shipped
   catalog (ADR-003b D1).
2. **Precedence:** `routed > MAESTRO_<H>_MODEL > catalog-default > fail-loud`.
   The existing `MAESTRO_CLAUDE_MODEL` / `MAESTRO_CODEX_MODEL` env vars stay as the
   env layer (minimal churn to #35). The catalog's `model_env` fields
   (`CLAUDE_MODEL` / `CODEX_MODEL`) stay **ATP-shim-only** — not a Maestro layer in
   this slice.
3. **Path resolution:** `$ATP_CATALOG` (explicit path) only, for now. The XDG
   default path is deferred to a follow-up, gated on the ratified `<eco>`
   namespace (ADR-003b line 164). `$ATP_CATALOG` **unset** → no catalog;
   `$ATP_CATALOG` **set but missing/invalid** → loud error (misconfiguration).
4. **Validation (old "item 2"):** warn-only, folded in now, with three refinements:
   - **Source-aware.** Validation only meaningfully fires on `routed` / `env`
     sources. A `catalog`-sourced model is tautologically in the catalog → skip
     (a warning there would be a bug). Primary purpose: **drift-detector on the
     routed path** (routed model absent from catalog = arbiter/catalog drift).
   - **Status-aware gradation** (membership **and** status, Plane 1 carries
     `status`): `retired` → loud warning (runtime echo of ADR-003 "retired →
     CI-fail on live reference"); `unknown` → soft ("maybe new — run
     `models discover` / add it"); `deprecated` → light; `active` → silent.
   - **Coherence-only.** Validate against the catalog, never against provider
     reality — that is the CLI's job. Clean boundary.
5. **Fail-loud propagation:** `CatalogNotConfiguredError` **bubbles raw** out of
   `spawn()` and the scheduler loop — it is terminal, not wrapped as
   `SchedulerError`, and must not feed per-task retry (retrying cannot create a
   catalog).

## Components

### 1. `maestro/catalog.py` (new) — loader, schema, resolution, validation

Pydantic schema (the "schema validation" a 003b reader ships):

```python
class CatalogModel(BaseModel):
    vendor: str
    status: Literal["active", "deprecated", "retired"] = "active"
    aliases: list[str] = []

class CatalogAgent(BaseModel):
    harness: str
    model: str
    tested: bool = False
    routable: bool = False

class Catalog(BaseModel):
    models: dict[str, CatalogModel]
    agents: list[CatalogAgent]

    def default_model_for_harness(self, harness: str) -> str | None:
        """Model of the routable [[agents]] entry for this harness; None if none."""

    def status_of(self, model: str) -> str | None:
        """Membership + status, resolving aliases. None means unknown (not in catalog)."""

    def nearest_models(self, model: str, n: int = 3) -> list[str]:
        """difflib close matches over model ids, for the warning payload."""
```

Loader + resolution + validation:

```python
class CatalogNotConfiguredError(RuntimeError):
    """No model catalog configured. Terminal, not retryable."""

def resolve_catalog_path() -> Path | None:
    """$ATP_CATALOG only for now. XDG default path = TODO (needs <eco> namespace)."""

def load_catalog() -> Catalog | None:
    """Parse the catalog TOML (tomllib, stdlib on 3.12+).
    Returns None when $ATP_CATALOG is unset (unconfigured).
    Raises CatalogNotConfiguredError when $ATP_CATALOG is set but the file is
    missing, and a schema/parse error when it is set but malformed (loud).
    """

def resolve_model(
    routed: str | None, env_var: str, harness: str, catalog: Catalog | None
) -> tuple[str, str]:
    """Precedence routed > env > catalog-default > fail-loud. Returns (model, source)
    with source in {"routed", "env", "catalog"}. An empty routed string is treated
    as absent (preserves the #35 guard against a degenerate "<harness>@" id)."""
    if routed:
        return routed, "routed"
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val, "env"
    if catalog is None:
        raise CatalogNotConfiguredError(
            "модельный каталог не настроен: задай $ATP_CATALOG (или 'atp models init')"
        )
    default = catalog.default_model_for_harness(harness)
    if default is None:
        raise CatalogNotConfiguredError(
            f"каталог не содержит routable-модели для harness '{harness}'"
        )
    return default, "catalog"

def warn_on_model_status(model: str, source: str, catalog: Catalog | None) -> None:
    """Coherence check. No-op when source == 'catalog' (tautological) or catalog is
    None (can't validate). Graded by status_of(model): retired→loud, unknown→soft,
    deprecated→light, active→silent. Payload: model, source, nearest_models()."""
```

`resolve_model` moves out of `base.py` into `catalog.py` (model/catalog concerns
colocate). `base.py` keeps `spawn_env` + `AgentSpawner`. Spawner imports update.

### 2. Spawner wiring (`claude_code.py`, `codex.py`)

Delete `DEFAULT_CLAUDE_MODEL` / `DEFAULT_CODEX_MODEL`. Each `spawn()`:

```python
catalog = load_catalog()                      # Catalog | None — one load per spawn
resolved, source = resolve_model(model, "MAESTRO_CLAUDE_MODEL", "claude_code", catalog)
_obs_log.info("agent.model_resolved", harness="claude_code", model=resolved, source=source)
warn_on_model_status(resolved, source, catalog)
```

One catalog load per spawn, injected into both pure functions — no module-level
cache (spawns are infrequent; TOML parse is negligible; avoids test cache-bleed).
`aider` / `announce` spawners are untouched (no model concept).

### 3. Error propagation

`CatalogNotConfiguredError` bubbles unwrapped from `spawn()` at
`scheduler.py:894`, through the `task.spawn` span, out of the run loop. It halts
loud (fail-loud), like the workdir `SchedulerError` guards at `scheduler.py:853/856`,
and is **not** routed into retry.

## Data flow

```
scheduler._start_task
  routed_model = model_of_agent_id(task.routed_agent_type) or None
  spawner.spawn(..., model=routed_model)
    catalog = load_catalog()                         # $ATP_CATALOG → Catalog | None
    resolved, source = resolve_model(routed, env, harness, catalog)
        routed? → (routed, "routed")
        env?    → (env, "env")
        catalog is None → raise CatalogNotConfiguredError   # bubbles raw
        else    → (catalog.default_model_for_harness(h), "catalog")
    log agent.model_resolved
    warn_on_model_status(resolved, source, catalog)   # drift-detector, best-effort
    subprocess.Popen([cli, model_flag, resolved, ...])
```

## Testing

- `tests/fixtures/agents-catalog.toml` — fixture mirroring the SSOT schema
  (offline, deterministic). Core tests run against it via `$ATP_CATALOG`.
- **Contract test (optional/skipped in isolation):** when
  `atp-platform/method/agents-catalog.toml` exists, assert the fixture's schema
  and routable defaults still match it. Seed of the ADR-003b "common conformance
  test on catalog fixtures" (line 160).
- `tests/test_catalog.py` (new): path resolution (`$ATP_CATALOG` set/unset);
  fail-loud (unset → raises on default path; set-but-missing → raises);
  `default_model_for_harness`; `status_of` (active / deprecated / retired /
  unknown / alias-hit); warn gradation asserted via captured obs events;
  source-aware skip (`catalog` source → no warn); `nearest_models`.
- `tests/test_spawners.py`: drop the `DEFAULT_*` imports; a fixture sets
  `$ATP_CATALOG` → the fixture file; the 4 assertions (322, 457, 847, 947) assert
  the resolved model equals the value **loaded from the fixture**, not a string
  literal. This discharges the ADR-003a "de-hardcode test model strings" item.

## Scope boundary

**In this slice:** loader + pydantic schema + `$ATP_CATALOG` resolution +
fail-loud + catalog-default + status-graded warn + tests.

**Out (follow-ups, → TODO.md):**
- XDG default path (gated on the ratified `<eco>` namespace).
- `maestro models init | list | discover | update` CLI (ADR-003b D3).
- The shared `CLAUDE_MODEL` / `CODEX_MODEL` cross-tool override layer.
- Extracting the loader to a shared PyPI lib with a cross-reader conformance test
  (ADR-003b line 160). ATP and arbiter loaders live in their own repos.

**Dependency:** none new — `tomllib` is stdlib on Python 3.12+.
