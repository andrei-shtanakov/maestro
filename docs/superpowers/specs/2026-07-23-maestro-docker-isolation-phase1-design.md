# Distributed Execution — Phase 1: Local Docker Isolation

> Parent design: `docs/superpowers/specs/2026-07-21-maestro-distributed-execution-design.md`
> Phase 0 (contract + `LocalBackend`) shipped in PR #90.
> This spec is co-authored with a two-round review against the live Phase-0 tree
> (2026-07-23); every amendment below is folded in.

## Context

Phase 0 introduced a transport-agnostic execution layer — `ExecutionRequest` /
`TaskHandle` / `ExecutionBackend` — and a single `LocalBackend` wired into **both**
run sites:

- `maestro/scheduler.py:264` — `self._backend = LocalBackend()` (Mode 1 tasks).
- `maestro/orchestrator.py:194` — `self._backend = LocalBackend()` (Mode 2
  workstreams; `plan --full` generation stays on a raw `create_subprocess_exec`
  at `orchestrator.py:907` and remains local).

Four Phase-0 facts constrain this phase and are the reason its scope is larger
than "just add a Docker isolator":

1. **`LocalBackend` is cleanup-free by design.** The parent log fd is closed at
   spawn time (`maestro/execution/local.py:154-160`); the scheduler's terminal
   path never calls `wait()`/`collect()`/`cleanup()`. It polls
   (`scheduler.py:1202` → `_handle_task_completion`). A container run **without
   `--rm`** would therefore leak after every run unless finalization is wired.
2. **`ExecutionHandleRef` is declared "persisted" but is never written anywhere.**
   After a restart the transport ref is gone; the task row does not store a
   backend; Mode-1 recovery (`maestro/recovery.py`) sees only the `Database`, not
   the original config. Recovery-by-ref is impossible without new persistence.
3. **`ExecutionRequest.run_id == task_id` (Mode 1) / `workstream_id` (Mode 2)**
   (`scheduler.py:1042`, `orchestrator.py:729`) — an entity id, **not** a
   globally-unique run identity. Two Maestro databases can each hold `task-1`,
   attempt 1. A `maestro.run_id` label would be ambiguous across databases.
4. **`self._backend` is a single hardcoded instance** in each mode — there is no
   per-task / per-workstream backend selection.

## Goal

Prove a single **vertical slice** — `config → resolver → DockerIsolator →
lifecycle → durable identity → recovery → cleanup` — for `local + docker` only,
by composition on top of the Phase-0 contract, with **zero regression** when no
`execution` config is present.

Two axes, composed (not four backend classes):

```
                 bare (on host)          docker (in local container)
   local     →   current behavior        THIS PHASE
```

## Non-goals (Phase 1)

- SSH / remote transports (parent design Phase 2a/2b), remote `plan --full`.
- The full named-`backends:{}` registry — deferred until a second isolator or
  transport exists. Phase 1 ships a narrow, proven `execution.docker` block.
- Vendoring proctor's `ContainerRuntime`. We reuse its **patterns and test
  cases** as prior art (injected `RunCmd` for daemon-free tests, `--format
  '{{json .}}'` inspect, `0600`/`0700` env-file, cleanup-by-label) — proctor is a
  read-only neighbor; no code is vendored or edited.
- `DOCKER_HOST=ssh://` remote-socket mode.
- Config-file / login-state agent auth inside the image (CLIs + `secret_env`
  only).
- Routing **validation** through the execution layer (parent design §9). Phase 1
  keeps post-task validation on `LocalBackend` as today; `validation_backend:
  same|local|<backend>` is a follow-up. Recorded as a deferred item below.

## Design

### 1. Isolator seam — pure `prepare` / effectful `materialize`

`LocalBackend` remains the single **transport**; an **isolator** is injected
(default `BareIsolator`). The isolator is split so argv/mount/label construction
stays deterministic and unit-testable, and all filesystem/secret side effects
happen in one place immediately before spawn.

`prepare` is **deterministic w.r.t. its arguments, not pure w.r.t. process
globals**: it must not read `os.environ` / `child_env()` itself (`BareIsolator`
needs the inherited env; both isolators need the trace env). Those are passed in
explicitly so a test controls the whole plan with no monkeypatching. Secret
**values** are still read only in `materialize`.

```python
class Isolator(Protocol):
    id: str
    def prepare(                                  # DETERMINISTIC: no global-state reads, no I/O
        self,
        req: ExecutionRequest,
        *,
        trace_env: Mapping[str, str],             # child_env(): TRACEPARENT, ORCHESTRA_*
        inherited_env: Mapping[str, str] | None,  # os.environ snapshot; None for docker
    ) -> PreparedRunPlan: ...
    def materialize(self, plan: PreparedRunPlan) -> PreparedRun: ...   # I/O, just before spawn
    def wrap(self, local: LocalTaskHandle, prepared: PreparedRun,
             ref: ExecutionHandleRef) -> TaskHandle: ...

class PreparedRunPlan(BaseModel):      # pure, fully assertable in unit tests
    argv: list[str]                    # effective argv (docker run ... image <orig argv>)
    env: dict[str, str]                # non-secret env actually passed
    container_name: str | None         # maestro-<execution_id> (docker only)
    labels: dict[str, str]             # identity labels (see §5)
    env_file_keys: list[str]           # secret NAMES to write (values never in the plan)
    cidfile_path: Path | None
    tmp_dir: Path | None               # 0700 dir holding env-file + cidfile

class PreparedRun(BaseModel):          # after materialize: paths that now exist on disk
    plan: PreparedRunPlan
    env_file: Path | None
    cleanup_paths: list[Path]          # tmp_dir/env-file/cidfile to unlink
```

- **`BareIsolator`** — identity. `prepare` returns today's argv and
  `{**inherited_env, **trace_env}` (honoring `inherit_env`); `materialize` is a
  no-op; `wrap` returns the `LocalTaskHandle` unchanged. Behavior-compatible with
  today.
- **`DockerIsolator(cfg)`** — `prepare` builds the `docker run` argv (§4), the
  identity labels (§5) and the env split (§3, `trace_env` inlined via `-e`,
  `inherited_env` ignored), naming secret keys only. `materialize` creates the
  `0700` tmp dir, writes the `0600` env-file, and fixes the `--cidfile` path.
  `wrap` returns a `DockerTaskHandle`.

`LocalBackend.run(req)` becomes: `plan = isolator.prepare(req, trace_env=…,
inherited_env=…)` → `prepared = isolator.materialize(plan)` → spawn
`prepared.plan.argv` via the existing asyncio path → build `ExecutionHandleRef`
(docker `transport_ref = "docker:maestro-<execution_id>"`) →
`isolator.wrap(local_handle, prepared, ref)`. On spawn failure, `materialize`'d
paths are cleaned (§3).

### 2. `DockerTaskHandle` — compositional, honest lifecycle

Wraps a `LocalTaskHandle` (the attached `docker run` process — its exit code is
the container's exit code):

- `poll()` / `os_pid` — delegate (sync, no I/O).
- `wait()` — **not** a plain delegate. `LocalTaskHandle.wait()` on timeout kills
  only the `docker run` process; the container may survive. So: `result = await
  local.wait(); if result.timed_out: await self._stop_container()` (targeted
  `docker stop -t <grace>` then `docker kill` by its own `--name`, then reap).
- `terminate(grace)` / `kill()` — signal the CLI process **and** issue a targeted
  `docker stop`/`docker kill` for this container name.
- `collect()` — **no-op that is genuinely called and tested** (§6): results live
  in the bind-mounted workspace, so there is nothing to apply back. Returns
  `CollectResult(applied=False, detail="docker: bind-mounted /work")`.
- `cleanup()` — **ownership-checked** (§7) `docker rm -f <name>` + unlink
  env-file / cidfile / tmp dir; idempotent.

### 3. Env / secret contract

The Docker path **never inherits host env**. Effective env =
`req.env` (explicit non-secret) + `trace_env` (`child_env()`: `TRACEPARENT`,
`ORCHESTRA_*`) passed via `-e`. Secrets go via `--env-file`:

- `secret_env` is an **allowlist of NAMES**; values are read from the center
  `os.environ` at `materialize` time, written to a `0600` env-file inside a
  `0700` dir, referenced by `--env-file`. Values never appear in the plan, argv,
  logs, event log, or DB.
- **Value validation (fail-fast):** reject any secret value containing `\n`,
  `\r`, or `NUL` (env-file format hazard). File permissions are verified **after**
  creation.
- **Cleanup on spawn failure:** the env-file / cidfile / tmp dir are removed even
  if `docker run` fails to start.
- The env-file **path** may appear in argv; **values** may not.
- **Denylist** with fail-fast: `secret_env` containing `GH_TOKEN`,
  `GITHUB_TOKEN`, or any `GH_*` raises a config error (not a silent drop) unless
  a future explicit override lands. Git / PR / merge stay on the center.
- `inherit_env: true` is honored **only** by `BareIsolator`/`LocalBackend`.

### 4. Mounts and hard constraints

`docker run` argv (attached, **no `--rm`** — recovery must see exited/dead
containers; removal is explicit and idempotent):

```
docker run --name maestro-<execution_id> --cidfile <tmp>/cid \
  -v <workdir>:/work -w /work \
  --user <cfg.user> --network <cfg.network|none> \
  --memory <cfg.memory> --cpus <cfg.cpus> \
  --env-file <tmp>/env \
  -e TRACEPARENT=... -e <non-secret>=... \
  --label maestro.execution_id=<id> --label maestro.entity_kind=... \
  --label maestro.entity_id=... --label maestro.attempt=... \
  --label maestro.backend_id=docker \
  <cfg.image> <original argv>
```

- **Never** mount the Docker socket.
- The workspace bind mount is the **only** project mount (writable). Any future
  read-only mounts must be explicit; Phase 1 has none.
- `--user` from config so container writes are not root-owned on the host.
- `--network none` is the secure default; a real agent run needs an explicit
  network mode (documented, §8).
- `inspect` is parsed only via `--format '{{json .}}'`, never scraped human text.

### 5. Identity and labels

`ExecutionRequest.run_id` is an entity id, so a `maestro.run_id` label is
misleading. Identity is carried by an **`execution_id = uuid4()` minted per
attempt** — a UUID gives global (cross-database) uniqueness with no separate
project-instance id. Labels:

```
maestro.execution_id   # UUID, globally unique — primary probe key
maestro.entity_kind    # task | workstream
maestro.entity_id      # task_id / workstream_id
maestro.attempt        # retry_count for this attempt
maestro.backend_id     # docker
```

Container name = `maestro-<execution_id>`; `transport_ref =
"docker:maestro-<execution_id>"` is fully formed **before** spawn. If a persisted
pipeline id is added later, it becomes an additional label — it does not replace
`execution_id`.

**Launch context lives on `ExecutionRequest`.** `backend.run(req)` receives only
an `ExecutionRequest`, but `prepare` needs the identity — and the identity must
be minted *and persisted* before `run()` (§6). So the backend cannot mint it
inside `prepare`. The Phase-0 contract is extended with backward-compatible,
defaulted launch fields:

```python
class ExecutionRequest(BaseModel):
    ...                                              # Phase-0 fields unchanged
    execution_id: str | None = None                 # uuid4; None = local/bare (no persisted row)
    entity_kind: Literal["task", "workstream"] | None = None
    attempt: int = 1                                 # = retry_count + 1 (cost-contract numbering)
    backend_id: str = "local"
```

`entity_id` is the existing `run_id` (an entity id today). The orchestration
layer — **not** the backend — mints `execution_id`, persists the
`execution_handles` row (§6), and only then hands the fully-populated request to
`backend.run`. Old local/bare requests keep the defaults and write no row.

### 6. Durable execution identity — `execution_handles` table

Execution is a first-class "one launch attempt" entity, not a property of a
task/workstream (one task has many attempts; Phase 2 adds SSH refs; one contract
serves both entity kinds; `ExecutionHandleRef` already models this). A dedicated
table also preserves orphan-attempt history for diagnosis and avoids duplicating
columns across `tasks` and `workstreams`.

```
execution_handles
  execution_id   TEXT PK              -- uuid4
  entity_kind    TEXT                 -- 'task' | 'workstream'
  entity_id      TEXT
  attempt        INTEGER
  backend_id     TEXT                 -- what this attempt actually ran with
  transport_ref  TEXT                 -- 'docker:maestro-<execution_id>' | 'local_pid:<pid>'
  state          TEXT                 -- 'prepared' | 'running' | 'terminal' | 'cleaned'
  created_at     TEXT
  finished_at    TEXT NULL
```

Added via the existing linear `schema_migrations` runner (LABS-85). A separate
`backend` field is **also** added to `TaskConfig`/`WorkstreamConfig` and threaded
into the runtime `Task`/`Workstream` models and their rows — it is the *selected
configuration for future runs*, distinct from `execution_handles.backend_id`
which records what a specific past attempt used. Without threading `backend`
into the persisted entity, the choice is lost after a DB read. `backend` must
participate in the config serialization round-trip and the schema migration for
**both** `tasks` and `workstreams` (nullable, default `None` → resolves to
`default_backend`).

Rows are written for docker-backed attempts. Local/bare attempts may write a row
too (uniformity) or skip it; Phase 1 writes rows only for non-local backends to
keep the local **runtime** path behavior-compatible (the schema itself changes —
the migration adds tables/columns, so "byte-identical" applies to observable
behavior, not to the DB schema).

#### Persistence ordering — one atomic primitive

There is **one** decision here, not a "big vs. minimal" fork. A single atomic DB
primitive couples the status CAS and the row insert; the two can never diverge:

```python
async def start_execution(
    entity_kind: Literal["task", "workstream"],
    entity_id: str,
    expected_status: TaskStatus | WorkstreamStatus,   # READY
    handle: ExecutionHandleRecord,                     # execution_id, backend_id, transport_ref
) -> Entity:                                           # updated row, or raises ConcurrentModificationError
```

In one `aiosqlite` transaction it: (1) CAS-updates the entity `READY → RUNNING`
(the existing `expected_status` guard / `ConcurrentModificationError`, cf.
`database.py:1087` / `:1880` / `reset_for_retry_atomic` at `:954`); (2) inserts
`execution_handles(state="prepared")`; (3) commits. If the transaction rolls
back, **neither** the status **nor** the execution row persists — there is no
window where a `prepared` row exists for an entity that never entered `RUNNING`.
The separate-insert fallback from the draft is **removed** for exactly this
reason.

Because the transition-hooks work (PR #94/#96) routes status effects through the
`TransitionDispatcher`, the new atomic path must fire hooks the same way: after
`start_execution` commits, the orchestration layer calls
`_dispatch_committed_transition` (`scheduler.py:333`) for the committed
`READY → RUNNING` — otherwise status/effect desync reappears.

Full ordering:

1. Mint `execution_id`, container name, labels.
2. `start_execution(...)` — atomic CAS `RUNNING` + `execution_handles` row
   `prepared`; then `_dispatch_committed_transition` for the committed edge.
3. `materialize` (write env-file / cidfile / tmp dir).
4. `docker run`.
5. Row → `running`.
6. After terminal handling (§7 finalize): row → `terminal`.
7. After *confirmed* cleanup only: row → `cleaned`.

A crash after step 2 but before/at spawn leaves a `prepared`/`running` row;
recovery (§11) treats both as *uncertain* and probes by label — because the
container name is deterministic from the already-persisted `execution_id`, the
"container created, CID not yet persisted" window is closed with no extra
sentinel.

### 7. Finalization — single owner, no new protocol method

The Phase-0 protocol already has `wait()` / `collect()` / `cleanup()`; **no
`finalize()` is added to the protocol.** Instead a shared helper enforces order
and never lets a `collect`/`cleanup` fault swallow the run result:

```python
async def finalize_handle(handle: TaskHandle) -> ExecutionResult:
    result = await handle.wait()                 # reap; the authoritative outcome
    errors: list[str] = []
    try:
        await handle.collect()                   # docker: no-op, still called
    except Exception as e:                        # collect must not hide the result
        errors.append(f"collect: {e}")
    try:
        # Shield so an external cancellation cannot abort a targeted
        # container stop/rm or leave secret files on disk.
        await asyncio.shield(handle.cleanup())   # raises on ownership/daemon failure
    except Exception as e:
        errors.append(f"cleanup: {e}")           # row stays 'terminal', NOT 'cleaned'
    if errors:
        result.error_message = "; ".join(
            filter(None, [result.error_message, *errors])
        )
    return result
```

Contract this pins down:

- **Exactly one finalization task per running entity.** The monitor records the
  in-flight finalize `Task` on the running-entity record; any second caller
  (shutdown loop, a re-tick) awaits that same task and its result rather than
  starting a second finalize. `cleanup` idempotency is the safety net, not the
  primary mechanism.
- **`collect`/`cleanup` errors are recorded, not fatal.** They fold into
  `ExecutionResult.error_message`; terminal entity processing (cost parse, status
  transition) still runs on the reaped `result`.
- **`cleanup` is cancellation-shielded** enough to finish the targeted
  `docker stop`/`rm` and unlink the env-file / cidfile / tmp dir.
- **`execution_handles.state = "cleaned"` is set only after a confirmed
  successful cleanup.** A daemon/ownership failure leaves the row `terminal`, so
  recovery (§11) can still find it and GC the possibly-leftover container (the
  entity's maestro status is already settled — this is a resource sweep, not a
  status change).
- **`cleanup()` signals a fail-closed ownership refusal by raising** (the
  protocol returns `None`; a structured `CleanupResult` is deferred). The helper
  catches and records it — it does not crash the monitor.

**Ownership is singular at the monitor.** The monitor loop is the one caller that
finalizes, then dispatches to the existing status handlers with the reaped
result:

- Normal completion (`poll()` returns non-None): call `finalize_handle` directly
  — `wait()` returns at once.
- Timeout / cancellation / shutdown (process still live): the owner first
  `terminate(grace)` / `kill()` the handle, **then** finalizes (now `wait()`
  reaps immediately and the container-stop is targeted).

Finalization is **not** scattered across `_handle_task_completion`,
`_handle_task_failure`, and `_handle_task_timeout`. The same single-owner
refactor is applied to the Mode-2 orchestrator monitor. Cost parsing and (local)
validation run after `finalize_handle` returns; because `collect` is a bind-mount
no-op and the log is already on the host, ordering is preserved.

**Safe cleanup (ownership check).** Before `docker rm -f <name>`, `inspect` the
container and confirm its `maestro.execution_id` label matches the expected id.
On mismatch, do **not** remove and **raise** a fail-closed ownership error (the
helper records it; the row stays `terminal`). If the container is already absent,
cleanup still unlinks the local env-file / cidfile / tmp dir and succeeds. UUID
collisions are effectively impossible, but a destructive op still verifies
ownership.

### 8. Local-only scope — Docker context

Phase 1 is local only. `healthcheck` inspects the effective Docker endpoint:

- Reject a `DOCKER_HOST` of `ssh://…` or `tcp://…` → fail-fast (remote is Phase
  2, and must not be reached implicitly via env).
- Allow a local Unix socket / Docker Desktop context.

Selecting `backend: docker` runs this check, plus image presence
(`docker image inspect`), **before the first task starts**. If `can_run` probes
`required_tools` by starting a throwaway container inside the image, that
**helper container uses `--rm` and its own unique label** and is not an execution
container — the "no `--rm`" rule (§4) applies only to execution containers, whose
exited state recovery must be able to observe.

### 9. Config contract

```yaml
execution:
  default_backend: local
  docker:
    image: maestro-runner:...
    network: none            # secure default; widen explicitly
    memory: 8g
    cpus: 2
    user: "1000:1000"
    secret_env: [ANTHROPIC_API_KEY]   # NAMES from host env
tasks:                       # or workstreams: for Mode 2 — same backend: local|docker
  - id: refactor
    backend: docker
```

The `execution` block is added to **both** root config models (`ProjectConfig`,
`OrchestratorConfig`) via a shared mixin. Rules:

- No `execution` section → `local + bare`, behavior-compatible with today
  (runtime path unchanged; the schema migration still applies).
- `default_backend: local` requires no Docker.
- `backend: docker` with no `execution.docker` → **fail-fast** config error.
- Unknown backend name → **fail-fast, no fallback to local**.
- Docker availability / image / context validated before the first task (§8).
- `secret_env` values are never persisted to config, argv, event log, or DB.
- The full `backends:{}` registry is deferred; the resolver interface is
  internally extensible while the public YAML stays narrow.

### 10. Backend resolution — per dispatch

A `BackendResolver` caches `local` / `docker` instances but resolves **per
entity** at dispatch:

```python
backend_name = entity.backend or execution.default_backend
backend = self._backends.resolve(backend_name)   # fail-fast on unknown
cap = await backend.can_run(request)
handle = await backend.run(request)
```

The hardcoded `self._backend = LocalBackend()` in both run sites is replaced by
the resolver.

### 11. Recovery — probe-by-label, fail-closed

Recovery reads persisted `execution_handles` rows and handles two concerns.

**(a) Uncertain execution** — a docker-backed row in state `prepared` or
`running` (the entity may be mid-flight):

- Probe by the persisted exact ref first, then label fallback:
  `docker ps -a --filter label=maestro.execution_id=<id>` — across **all**
  container states (running, restarting, paused, exited, dead).
- A found container is verified against the **full** expected label set (guards
  against an accidentally reused name).
- Any confirmed container (any state) → `NEEDS_REVIEW`. Never auto
  attach/resume/restart.
- Nothing found → the existing recovery path proceeds.
- Docker daemon unavailable, `inspect` error, or multiple candidates →
  `NEEDS_REVIEW`.
- The recovery probe **deletes nothing** — it classifies only. Cleanup is a
  separate explicit action bound by the same ownership check (§7).

**(b) Leftover-container GC** — a row in state `terminal` but not `cleaned`
(finalize ran, cleanup did not confirm). The entity's maestro status is already
settled, so this is **not** a status change: recovery runs the ownership-checked
cleanup (§7) to remove the possibly-orphaned container and unlink stale secret
files, then marks the row `cleaned`. A daemon failure leaves it `terminal` for
the next sweep.

### 12. Observability

Spans `execution.dispatch` (backend/isolation) and `execution.run`; record the
backend/isolation each task ran on (extends `DOGFOOD_LOG`). `TRACEPARENT` is
propagated into the container via `trace_env` (§3). Update the `CLAUDE.md:155`
communication note so docs do not contradict the Docker execution path.

## Testing

**Unit (no daemon; injected `RunCmd` prior-art pattern):**

- `DockerIsolator.prepare` argv / mounts / labels / env split are pure and fully
  asserted; secret **values** never appear in the plan or argv.
- env-file written `0600` in a `0700` dir; value validation rejects `\n`/`\r`/`NUL`;
  denylist (`GH_*`) fails fast.
- `BackendResolver` fail-fast cases: `docker` without `execution.docker`, unknown
  name, `DOCKER_HOST=ssh://`.
- Recovery classification against a mock docker: exact + full-label match, all
  container states, ambiguous / daemon-down → `NEEDS_REVIEW`.
- Ownership-checked cleanup: label mismatch → no removal, fail-closed diagnostic.

**Four must-have lifecycle cases:**

1. A timeout inside `wait()` kills **and removes** the container.
2. A normal success runs `finalize_handle` → no leftover exited container.
3. A spawn failure removes env-file / cidfile / tmp dir.
4. Two entities (or two databases) with the same `task_id` never receive the same
   container name (guaranteed by `execution_id`).

**Opt-in integration (requires docker, auto-skip):** UID / file ownership on the
host; `--network`/`--memory` flags applied; the **collect no-op contract** (a
file written to `/work` in the container appears on the host); cleanup-by-label;
`terminate`/`kill` actually stops the container.

**Networking test must not depend on the public internet:** opt-in networking is
exercised against a **local test Docker network**; real Anthropic/GitHub
reachability is a manual smoke step, not CI.

## Deferred / follow-ups

- Validation through the execution layer (`validation_backend: same|local|<backend>`,
  parent design §9).
- Full named-`backends:{}` registry (arrives with the second isolator/transport;
  the `execution_handles` `backend_id` + resolver interface are already shaped
  for it).
- SSH / remote transports and remote `plan --full` (Phase 2).
- Publishing a `maestro-runner` image; Phase 1 consumes a user-provided image and
  may ship only a sample Dockerfile.

## Acceptance

- No `execution` config → all current tests green; local runtime path
  behavior-compatible (schema migration aside).
- `backend: docker` runs an agent (Mode 1) and a spec-runner workstream (Mode 2)
  in a local container with the workspace bind-mounted; results land on the host.
- Every terminal path (success / failure / timeout / cancellation / shutdown)
  finalizes exactly once; no container is left after a successful run.
- A simulated center crash with a live container is classified `NEEDS_REVIEW`,
  never silently re-run.
- Secret values never appear in argv, logs, event log, or DB.
- The four must-have lifecycle tests and the fail-fast config tests pass.

## Staging hint for the implementation plan

The plan (next step, via writing-plans) will stage this into reviewable
increments, e.g.: (1a) isolator seam + `BareIsolator` + `finalize_handle`
single-owner refactor + resolver + `execution` config, all no-op/local (proves
zero regression); (1b) `DockerIsolator` + `DockerTaskHandle` + env-file secrets +
mounts/constraints; (1c) `execution_handles` persistence + recovery
classification + ownership-checked cleanup; (1d) opt-in integration tests.
