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
   - **Deliberate asymmetry (review P2):** an *absent* catalog spares a routed task
     (→ `None`, routed self-sufficient), but a *malformed* catalog halts everything
     including routed tasks (`load_catalog` runs unconditionally in `spawn()` and
     raises `CatalogMalformed`). This is intended: absent = "nothing to say",
     safe to skip; malformed = a config the operator *believes* is live but is
     corrupt — we cannot trust it even for a best-effort warn, so fail-fast for the
     whole run rather than act on garbage.
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
5. **Exception taxonomy split by blast radius** (review P0-1/P0-2). "Fail-loud
   everywhere" is too coarse: fail-loud is right for a **global** fault (the
   catalog is unusable for *everyone*), but a **per-task** fault (this one harness
   can't resolve a default) must fail only that task, not kill unrelated healthy
   work. Reusing one exception for both — and letting a `catalog`-typed re-raise
   miss the malformed error — created two holes. The taxonomy:

   ```
   CatalogError(RuntimeError)          # GLOBAL — catalog unusable for everyone → HALT run
     ├─ CatalogNotConfigured           # no catalog at all AND a default is needed
     └─ CatalogMalformed               # $ATP_CATALOG set, file present, but corrupt/schema-invalid

   HarnessModelUnresolved(RuntimeError)  # PER-TASK — separate hierarchy → this task NEEDS_REVIEW, run continues
   ```

   - **Global (`CatalogError` and subclasses) → halt.** `_spawn_ready_tasks`
     (`scheduler.py:670-674`) has a catch-all `except Exception` that routes spawn
     errors into `_handle_spawn_error` → task **FAILED** + retry (the workdir
     `SchedulerError`s are swallowed the same way — **not** a halt-loud precedent).
     A dedicated `except CatalogError: raise` **ahead of** the catch-all lets both
     global subclasses escape → `_main_loop` → `run()`'s `try/finally`
     (`_cleanup()` still runs) → out. Terminal, never FAILED, never retried
     (P4-hang lesson — retrying cannot create or repair a global catalog).
     **`CatalogMalformed` is the fix for review P0-1:** `load_catalog` wraps
     `tomllib`/pydantic errors in `CatalogMalformed` so corrupt data actually
     takes the halt path instead of falling through to per-task retry-churn.
   - **Per-task (`HarnessModelUnresolved`, deliberately NOT a `CatalogError`) →
     one task to `NEEDS_REVIEW`.** A dedicated handler (§3, Decision #6) sets that
     task `NEEDS_REVIEW` (deterministic → no retry); the run continues. Kept
     outside `CatalogError` on purpose so a later "tidy-up" can't re-parent it and
     silently turn a local fault back into a global halt.
6. **Per-task default faults (review P0-2 blast radius):** both "no routable model
   for this harness" and "**>1** routable for this harness" (the ADR-003a A/B
   window, e.g. `claude_code@claude-sonnet-4-6` + `@claude-sonnet-5` both routable
   during a flip) raise **`HarnessModelUnresolved`** — per-task, not global. So a
   catalog that fully serves `claude_code` but lacks a `codex` default will FAIL a
   non-routed `codex` task while `claude_code` work proceeds; and the A/B window
   fails only non-routed tasks of the ambiguous harness (routed tasks are
   unaffected — arbiter supplies the model). The message tells the operator to
   disambiguate via `MAESTRO_<H>_MODEL`; never silently pick one. Because the
   condition is **deterministic** — a retry cannot create or de-duplicate a
   routable entry, it would only burn retry budget en route to NEEDS_REVIEW
   (review P2) — this task goes **straight to `NEEDS_REVIEW`, skipping retries**,
   via a dedicated handler (§3) rather than the retry-looping `_handle_spawn_error`.
   The ergonomic long-term fix — an explicit `default = true` field in the catalog
   `[[agents]]` schema — is a cross-repo, PM-owned **follow-up**.
7. **Deliberate non-blocks (review P1) — recorded so they aren't "fixed" later):**
   - **`retired`/`deprecated` as a resolved model still spawns** (warn only, never
     hard-fail). The CLI/provider is the availability authority; Maestro only warns
     on catalog incoherence. Do **not** turn this into a block — that would break
     the no-block boundary (Decision #4, coherence-only).
   - **A broken `$ATP_CATALOG` path silently disables the routed-path drift
     detector** (absent → `None` → `warn_on_model_status` no-ops; only an `info` at
     load). Accepted known property, not a bug — otherwise "drift isn't caught"
     reads as a defect later.
   - **Degenerate routed `agent_id`** (`model_of_agent_id(...)` returns `""` for a
     trailing-`@` id): the scheduler must **warn** when `task.routed_agent_type` is
     set but yields an empty model, rather than silently falling through to the
     catalog default and masking bad routing. Warn at the `routed_model`
     computation site (`scheduler.py:889-893`), then proceed with the normal chain.

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

    def default_model_for_harness(self, harness: str) -> str:
        """Model of the single routable [[agents]] entry for this harness.
        Raises HarnessModelUnresolved (per-task, NOT a CatalogError) when there is
        no routable entry, or >1 (ADR-003a A/B window) — the caller disambiguates
        via MAESTRO_<H>_MODEL. May return a value written as an alias; that is fine
        for --model, and status_of() resolves aliases so downstream checks agree."""

    def status_of(self, model: str) -> str | None:
        """Membership + status, resolving aliases. None means unknown (not in catalog)."""

    def nearest_models(self, model: str, n: int = 3) -> list[str]:
        """difflib close matches over model ids, for the warning payload."""
```

Loader + resolution + validation:

```python
# GLOBAL faults — catalog unusable for everyone → halt run (terminal, not retried)
class CatalogError(RuntimeError): ...
class CatalogNotConfigured(CatalogError):
    """No catalog configured and a default is needed."""
class CatalogMalformed(CatalogError):
    """$ATP_CATALOG set, file present, but corrupt / schema-invalid."""

# PER-TASK fault — separate hierarchy on purpose → FAIL this task, run continues
class HarnessModelUnresolved(RuntimeError):
    """No routable model, or >1 (A/B window), for this harness. Disambiguate via env."""

def resolve_catalog_path() -> Path | None:
    """$ATP_CATALOG only for now. XDG default path = TODO (needs <eco> namespace)."""

def load_catalog() -> Catalog | None:
    """Parse the catalog TOML (tomllib, stdlib on 3.12+).
    Returns None for BOTH "no catalog" cases: $ATP_CATALOG unset, or set but the
    file is absent (a path typo must not crash a routed run — log an info line so
    it is discoverable). Raises CatalogMalformed (wrapping tomllib.TOMLDecodeError /
    pydantic ValidationError) when the file is present but corrupt / schema-invalid,
    so corrupt data takes the halt path — never silent, never per-task retry-churn.
    """

def resolve_model(
    routed: str | None, env_var: str, harness: str, catalog: Catalog | None
) -> tuple[str, str]:
    """Precedence routed > env > catalog-default > fail. Returns (model, source)
    with source in {"routed", "env", "catalog"}. An empty routed string is treated
    as absent (preserves the #35 guard against a degenerate "<harness>@" id)."""
    if routed:
        return routed, "routed"
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val, "env"
    if catalog is None:
        raise CatalogNotConfigured(              # GLOBAL → halt
            "модельный каталог не настроен: задай $ATP_CATALOG (или 'atp models init')"
        )
    # default_model_for_harness raises HarnessModelUnresolved (PER-TASK) on
    # no-routable / ambiguous — routed to _handle_unresolvable_task (NEEDS_REVIEW),
    # not the global halt.
    return catalog.default_model_for_harness(harness), "catalog"

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

### 3. Error propagation — one catch-all, split by blast radius

`_spawn_ready_tasks` (`scheduler.py:670-674`) wraps the spawn in a catch-all
`except Exception` → `_handle_spawn_error` → `update_task_status(FAILED)` +
retry-per-policy (the workdir `SchedulerError`s at `:853/856` are swallowed the
same way — *not* a halt-loud precedent, contrary to an earlier draft). We add
two dedicated handlers ahead of it — a **global-halt** arm and a
**per-task-no-retry** arm:

```python
try:
    launched = await self._spawn_task(task_id)
except CatalogError:                        # GLOBAL (NotConfigured / Malformed) → halt run
    raise
except HarnessModelUnresolved as e:         # PER-TASK, deterministic → NEEDS_REVIEW, no retry
    await self._handle_unresolvable_task(task_id, e)
except Exception as e:                       # everything else → FAILED + bounded retry
    await self._handle_spawn_error(task_id, e)
```

- **`CatalogError`** propagates `_spawn_ready_tasks` → `_main_loop` → `run()`'s
  `try/finally` (`_cleanup()` still runs, no orphaned processes) → out. Terminal:
  never FAILED, never retried.
- **`HarnessModelUnresolved`** (not a `CatalogError`) → new
  `_handle_unresolvable_task` sets the task to **`NEEDS_REVIEW`** directly
  (deterministic fault; retrying is futile — review P2). The run keeps going; one
  task's harness misconfig never halts unrelated tasks (P0-2 blast radius).
- Everything else keeps the existing `_handle_spawn_error` → FAILED + retry.

`_handle_unresolvable_task` mirrors `_handle_spawn_error` (`scheduler.py:966`) but
targets `TaskStatus.NEEDS_REVIEW` with the error message, and emits the matching
status-change/event. Note the task is already `RUNNING` at this point (the
transition at `:880` precedes the `spawn()` at `:894`, and the raise is before
`_running_tasks` tracking at `:899`, so there is no process to reap). This adds a
`RUNNING → NEEDS_REVIEW` edge; like `_handle_spawn_error`'s `RUNNING → FAILED`, it
force-sets status (no `expected_status` guard). Update the state-machine doc in
`CLAUDE.md` to record the new edge.

## Data flow

```
scheduler._start_task
  routed_model = model_of_agent_id(task.routed_agent_type) or None
  if task.routed_agent_type and not routed_model:   # degenerate agent_id (trailing-@)
      warn "routed agent_id produced empty model" (P1) — then proceed
  spawner.spawn(..., model=routed_model)
    catalog = load_catalog()          # unset/absent → None; malformed → raise CatalogMalformed
    resolved, source = resolve_model(routed, env, harness, catalog)
        routed? → (routed, "routed")
        env?    → (env, "env")
        catalog is None → raise CatalogNotConfigured          # GLOBAL, default path only → halt
        no / >1 routable for harness → raise HarnessModelUnresolved   # PER-TASK → NEEDS_REVIEW
        else    → (catalog.default_model_for_harness(h), "catalog")
    log agent.model_resolved
    warn_on_model_status(resolved, source, catalog)   # drift-detector, best-effort
    subprocess.Popen([cli, model_flag, resolved, ...])

# CatalogError (global) → dedicated re-raise in _spawn_ready_tasks → run() → halt loud.
# HarnessModelUnresolved (per-task) → _handle_unresolvable_task → NEEDS_REVIEW (no retry), run continues.
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
    (+ info logged, no raise); set-but-malformed → raises **`CatalogMalformed`**
    (wrapping the tomllib/pydantic error).
  - fail on the **default path only**: no routed + no env + no catalog →
    `CatalogNotConfigured`; but routed/env resolve fine even when `$ATP_CATALOG`
    is unset or points at an absent file (P0.1 — routed self-sufficiency).
  - `default_model_for_harness`: single routable → that model; **no routable →
    `HarnessModelUnresolved`**; **>1 routable → `HarnessModelUnresolved`** (A/B
    window); value written as an alias round-trips. Assert `HarnessModelUnresolved`
    is **not** a subclass of `CatalogError` (guards the blast-radius split).
  - `status_of`: active / deprecated / retired / unknown / alias-hit.
  - warn gradation via captured obs events: retired→loud and deprecated→light
    **fire even for `source=="catalog"`** (P1.4); unknown→soft fires for
    routed/env but is **skipped for `source=="catalog"`**; active→silent;
    `catalog is None` → no warn. `nearest_models` populated in the payload.
- `tests/test_scheduler*.py` — blast-radius cases, each under an `anyio`/timeout
  guard so a regression surfaces as a failure, not a hang:
  - **Global → halt** (P0.2): a model-aware task with no routed model, no
    `MAESTRO_<H>_MODEL`, no `$ATP_CATALOG` → `run()` raises `CatalogNotConfigured`,
    loop terminates, task **not** FAILED-retried / HOLD.
  - **Malformed → halt** (P0-1): `$ATP_CATALOG` → a corrupt file → `run()` raises
    `CatalogMalformed`, loop terminates (proves malformed takes the halt path, not
    per-task churn) — halts **even for a routed task** (the P2 asymmetry).
  - **`_cleanup` on the halt path** (P2): with a live child process already
    tracked in `_running_tasks`, a `CatalogError` halt must still run `_cleanup()`
    — assert no orphaned subprocess survives (`run()`'s `try/finally` fired). Guards
    the "no orphaned processes" promise in §3.
  - **Per-task → NEEDS_REVIEW, run continues** (P0-2): two ready tasks, catalog
    serves harness A but not harness B (no/ambiguous routable for B); B goes
    **`NEEDS_REVIEW`** (`HarnessModelUnresolved` → `_handle_unresolvable_task`,
    **no retry attempts**) while A spawns normally and the run does **not** halt.
  - **Degenerate routed id** (P1): `task.routed_agent_type` set but
    `model_of_agent_id` → `""` → a warn is emitted and resolution proceeds down the
    normal chain (not a silent fall-through).
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
