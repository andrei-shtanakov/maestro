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
   namespace (ADR-003b line 164).
   **"No catalog" vs "broken catalog" are distinct** (review P0.1): a missing
   catalog must never bring down a self-sufficient routed task, and the warn must
   stay best-effort.
   - `$ATP_CATALOG` **unset** → `None` ("no catalog").
   - `$ATP_CATALOG` **set but file absent** → `None` ("no catalog"; a path typo
     must not crash a routed run). Emit a one-line `info` so the typo is
     discoverable, but do not raise.
   - `$ATP_CATALOG` **set and file present but malformed / schema-invalid** →
     **raise** (corrupt data cannot be silently ignored).
   - **Fail-loud fires only when the default path hits `None`** — i.e. no routed,
     no env, and no catalog. Not on the routed/env paths.
4. **Validation (old "item 2"):** warn-only, folded in now, with three refinements:
   - **Source-aware, but only the `unknown` branch is source-gated** (review
     P1.4). Membership is tautological for a `catalog`-sourced model, so the
     `unknown` warning is skipped when `source == "catalog"`. **Status is always
     checked**, every source: a routable default that points at a `retired` /
     `deprecated` model must still warn loud — the dev-SSOT catches that in CI,
     but a user-config catalog has no CI, so Maestro is the only guard. Primary
     purpose remains a **drift-detector on the routed path** (routed model absent
     from catalog = arbiter/catalog drift).
   - **Status-aware gradation** (membership **and** status, Plane 1 carries
     `status`): `retired` → loud warning (runtime echo of ADR-003 "retired →
     CI-fail on live reference"); `unknown` → soft ("maybe new — run
     `models discover` / add it"), routed/env only; `deprecated` → light;
     `active` → silent.
   - **Coherence-only.** Validate against the catalog, never against provider
     reality — that is the CLI's job. Clean boundary.
5. **Fail-loud propagation:** `CatalogNotConfiguredError` **bubbles raw** out of
   `spawn()` — not wrapped as `SchedulerError`. But raw bubbling is not enough on
   its own (review P0.2): `_spawn_ready_tasks` (`scheduler.py:670-674`) has a
   catch-all `except Exception` that routes every spawn error into
   `_handle_spawn_error` → marks the task **FAILED** and continues (the existing
   workdir `SchedulerError`s are swallowed the same way — they are **not** a
   halt-loud precedent). So this slice **narrows that catch-all**: a dedicated
   `except CatalogNotConfiguredError: raise` ahead of the broad handler lets the
   error propagate out of `_main_loop` → `run()`'s `try/finally` (so `_cleanup()`
   still runs) → out, terminating the run loud. It is **terminal, never marked
   FAILED, never retried** — retrying cannot create a catalog (P4-hang lesson).
6. **Ambiguous default (>1 routable per harness):** the ADR-003a A/B adoption
   window deliberately makes two entries routable for one harness during a flip
   (e.g. `claude_code@claude-sonnet-4-6` and `@claude-sonnet-5`). A catalog
   default is then ambiguous. `default_model_for_harness` **raises
   `CatalogNotConfiguredError`** in that case, with a message telling the operator
   to disambiguate via `MAESTRO_<H>_MODEL` — fail-loud, never silently pick one.
   *(Recommended; this is the one choice made on the reviewer's behalf.)* The
   ergonomic long-term fix is an explicit `default = true` field in the catalog
   `[[agents]]` schema, which when present wins and removes the ambiguity — but
   that is a cross-repo schema change owned by PM, so it is a **follow-up**, not
   this slice.

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
        """Model of the routable [[agents]] entry for this harness; None if none.
        If >1 routable entry exists for the harness (ADR-003a A/B window), raise
        CatalogNotConfiguredError (ambiguous) — the caller must disambiguate via
        MAESTRO_<H>_MODEL. May return a value written as an alias; that is fine
        for --model, and status_of() resolves aliases so downstream checks agree."""

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
    Returns None for BOTH "no catalog" cases: $ATP_CATALOG unset, or set but the
    file is absent (a path typo must not crash a routed run — log an info line so
    it is discoverable). Raises only when $ATP_CATALOG is set and the file is
    present but malformed / schema-invalid — corrupt data is loud, never silent.
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
    """Coherence check (catalog-only, never provider reality — that is the CLI's
    job). No-op when catalog is None (can't validate). Grades by status_of(model):
    retired→loud, deprecated→light, active→silent — for EVERY source, so a
    catalog-sourced retired/deprecated default still warns. The unknown→soft branch
    is the only source-gated one: skipped when source == 'catalog' (membership is
    tautological there). Payload: model, source, nearest_models()."""
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

### 3. Error propagation — narrow the scheduler catch-all

`CatalogNotConfiguredError` bubbles unwrapped from `spawn()` at
`scheduler.py:894`. But `_spawn_ready_tasks` (`scheduler.py:670-674`) wraps the
spawn in a catch-all `except Exception` that calls `_handle_spawn_error` →
`update_task_status(FAILED)` and continues; the loop then retries per policy.
That would turn a global misconfig into per-task FAILED churn (and the workdir
`SchedulerError`s at `:853/856` are swallowed identically — so they are *not* a
halt-loud precedent, contrary to an earlier draft of this spec).

The fix: add a dedicated handler **ahead of** the catch-all so the error escapes:

```python
try:
    launched = await self._spawn_task(task_id)
except CatalogNotConfiguredError:
    raise                                   # terminal misconfig — halt loud
except Exception as e:
    await self._handle_spawn_error(task_id, e)
```

It then propagates `_spawn_ready_tasks` → `_main_loop` → `run()`'s `try/finally`
(so `_cleanup()` still runs, no orphaned processes) → out. Terminal: never marked
FAILED, never retried (the P4-hang lesson — retrying cannot create a catalog).

## Data flow

```
scheduler._start_task
  routed_model = model_of_agent_id(task.routed_agent_type) or None
  spawner.spawn(..., model=routed_model)
    catalog = load_catalog()          # unset/absent → None; malformed → raise
    resolved, source = resolve_model(routed, env, harness, catalog)
        routed? → (routed, "routed")
        env?    → (env, "env")
        catalog is None → raise CatalogNotConfiguredError    # only on default path
        >1 routable for harness → raise CatalogNotConfiguredError (ambiguous)
        else    → (catalog.default_model_for_harness(h), "catalog")
    log agent.model_resolved
    warn_on_model_status(resolved, source, catalog)   # drift-detector, best-effort
    subprocess.Popen([cli, model_flag, resolved, ...])

# CatalogNotConfiguredError escapes via the dedicated re-raise in
# _spawn_ready_tasks (bypassing the catch-all) → _main_loop → run() → halt loud.
```

## Testing

- `tests/fixtures/agents-catalog.toml` — fixture mirroring the SSOT schema
  (offline, deterministic). Core tests run against it via `$ATP_CATALOG`.
- **Contract test (optional/skipped in isolation):** when
  `atp-platform/method/agents-catalog.toml` exists, assert the fixture's schema
  and routable defaults still match it. Seed of the ADR-003b "common conformance
  test on catalog fixtures" (line 160).
- `tests/test_catalog.py` (new):
  - path resolution: `$ATP_CATALOG` unset → `None`; set-but-absent → `None`
    (+ info logged, no raise); set-but-malformed → raises.
  - fail-loud on the **default path only**: no routed + no env + no catalog →
    `CatalogNotConfiguredError`; but routed/env resolve fine even when
    `$ATP_CATALOG` is unset or points at an absent file (P0.1 — routed
    self-sufficiency).
  - `default_model_for_harness`: single routable → that model; **>1 routable for a
    harness → raises** (P0.3 A/B window); value written as an alias round-trips.
  - `status_of`: active / deprecated / retired / unknown / alias-hit.
  - warn gradation via captured obs events: retired→loud and deprecated→light
    **fire even for `source=="catalog"`** (P1.4); unknown→soft fires for
    routed/env but is **skipped for `source=="catalog"`**; active→silent;
    `catalog is None` → no warn. `nearest_models` populated in the payload.
- `tests/test_scheduler*.py` (new case, P0.2): run the scheduler with one
  model-aware task, no routed model, no `MAESTRO_<H>_MODEL`, no `$ATP_CATALOG` →
  `run()` raises `CatalogNotConfiguredError` and the loop **terminates** (assert
  under an `anyio`/timeout guard so a regression surfaces as a failure, not a
  hang). Assert the task is **not** marked FAILED-then-retried and not left HOLD —
  it halts before/at spawn. Confirms the catch-all narrowing actually took.
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
- Explicit `default = true` field in the catalog `[[agents]]` schema to
  disambiguate the A/B window ergonomically (cross-repo, PM-owned; until then
  Maestro fails loud on ambiguity per Decision #6).
- Extracting the loader to a shared PyPI lib with a cross-reader conformance test
  (ADR-003b line 160). ATP and arbiter loaders live in their own repos.

**Schema-copy note (review P2.5):** the pydantic `Catalog` here is the **third**
definition of the catalog shape (ATP Python + arbiter Rust are the others). The
optional contract test in this slice covers **shape only** — it does **not** yet
assert behavioral agreement (precedence order, alias resolution) across the three
readers. That cross-reader behavioral conformance is the shared-lib follow-up
above; recorded here so the gap is explicit, not assumed-covered.

**Dependency:** none new — `tomllib` is stdlib on Python 3.12+.
