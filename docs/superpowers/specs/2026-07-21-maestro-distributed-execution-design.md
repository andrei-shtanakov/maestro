# Distributed / remote / containerized execution backend

## Context

Maestro executes every task as a **local OS process**, in two paths:

- **Mode 2 (Orchestrator):** a DAG workstream shells out to the `spec-runner`
  CLI via `asyncio.create_subprocess_exec` in a local git worktree
  (`maestro/orchestrator.py:662`), then tracks progress by reading spec-runner's
  SQLite state file from local disk (`maestro/spec_runner.py:79`
  `_read_state_from_sqlite`).
- **Mode 1 (Task Scheduler):** agent spawners (`claude`/`codex`/`aider`/
  `opencode`) each `subprocess.Popen([...cli...])` directly in the task's
  **shared** workdir (`maestro/spawners/claude_code.py:93`,
  `maestro/scheduler.py:995`). Availability is a local PATH check
  (`AgentSpawner.is_available()`, `maestro/spawners/base.py:47`, backed by
  `shutil.which`).

The entire lifecycle — PID tracking, `poll()`/`wait()`/`terminate()`/`kill()`,
recovery, `max_concurrent` throttling — is committed to a local process handle
(`maestro/scheduler.py:140`, `maestro/orchestrator.py:95`). There is **no**
remote/SSH/Docker/K8s capability anywhere in the codebase.

We want Maestro to run spec-runner workstreams and agent tasks on: (a) the local
machine (as today), (b) a local Docker container (sandbox), (c) a remote host
over SSH, (d) a remote container. This enables sandboxing, parallelism across
machines, and running the Maestro "center" on a weak machine while executing on
stronger executors.

This design was reviewed read-only against the live `maestro` tree; the review
(`_cowork_output/maestro-distributed-execution-design-review-2026-07-21.md`) is
folded in here as amendments and the phasing below.

## Goal

Introduce a transport-agnostic execution layer — `ExecutionRequest` /
`TaskHandle` / `ExecutionBackend` — that both Mode 1 and Mode 2 dispatch
through, plus concrete `LocalBackend` and `SshBackend`, and a `DockerIsolator`
composable with either transport. Model the space as two orthogonal axes:

```
                 bare (on host)            docker (in container)
   local     →   current behavior          local sandbox
   ssh       →   remote host, direct        remote sandbox / powerful machine
```

Implemented by composition (2 transports × 2 isolators), not four backend
classes.

### MVP guarantees (hard requirements)

- **No `execution` config → `local + bare`, byte-identical to today.** The
  entire refactor is behavior-preserving when unconfigured.
- **A remote executor never receives GitHub credentials by default.**
  `GH_TOKEN`/`GITHUB_TOKEN`/`GH_*` are denylisted; git/PR/merge stays on the
  center (`maestro/orchestrator.py:1001`, `:1053`).
- **`spec-runner plan --full` (generation) stays local in MVP.** The "weak
  center" goal does not yet cover decomposition/generation
  (`maestro/decomposer.py:319`); it remains a local preflight step. Explicitly
  out of scope for remote execution.
- **Non-local Mode 1 is gated.** Mode 1's shared workdir must not be naively
  rsync'd round-trip under parallelism (see Design §7). Remote Mode 1 requires
  scope-aware collect + locking, or `max_concurrent=1`.

## Design

### 1. Contract

`ExecutionRequest` describes not just `argv/cwd/env` but the full lifecycle:
where to read/write the workdir, when to collect changes, how to probe/kill an
orphan, what capabilities are required, and which env/secrets are permitted on
the executor.

```python
class ExecutionRequest(BaseModel):
    run_id: str                              # unique; names remote tmp + container
    argv: list[str]                          # ["spec-runner","run",...] | ["claude","-p",...]
    workdir: Path                            # local worktree (Mode 2) or shared dir (Mode 1)
    log_path: Path                           # where the CENTER writes merged stdout/stderr
    stdin: str | None = None
    env: dict[str, str] = {}                 # explicit, non-secret
    secret_env: list[str] = []               # allowlist of NAMES read from center env
    inherit_env: bool = False                # True only meaningful for LocalBackend
    timeout_seconds: int | None = None
    collect: CollectPolicy
    labels: dict[str, str] = {}              # maestro.run_id / backend_id / task_id
    required_tools: list[str] = []           # probed on the executor (claude, spec-runner, python)

class CollectPolicy(BaseModel):
    mode: Literal["none", "whole_worktree", "scope_paths", "state_mirror"]
    include: list[str] = []
    exclude: list[str] = [".git/**", ".maestro/**"]
    conflict_policy: Literal["fail", "overwrite"] = "fail"
    on_failure: Literal["collect", "skip"] = "collect"   # parity with local partial-change behavior

class ExecutionHandleRef(BaseModel):         # persisted; survives center restart
    backend_id: str
    run_id: str
    transport_ref: str                       # local_pid | ssh_host+remote_pid | ssh_host+container_name
    started_at: datetime
    workdir_mirror_path: Path | None = None
    state_mirror_path: Path | None = None

class TaskHandle(Protocol):
    ref: ExecutionHandleRef
    def poll(self) -> int | None: ...                 # non-blocking exit code (scheduler needs this)
    async def wait(self) -> int: ...
    async def terminate(self, grace_seconds: float) -> None: ...
    async def kill(self) -> None: ...
    async def collect(self) -> CollectResult: ...     # explicit lifecycle phase (see §5)
    async def cleanup(self) -> None: ...              # remove remote tmp / container / env-file

class ExecutionBackend(Protocol):
    id: str
    async def healthcheck(self) -> BackendHealth: ...            # transport reachable? (fail-fast)
    async def can_run(self, req: ExecutionRequest) -> CapabilityResult: ...  # required_tools present?
    async def run(self, req: ExecutionRequest) -> TaskHandle: ...
    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult: ...       # is a persisted run alive?
```

`TaskHandle` must replace **both** `Popen` (scheduler) and
`asyncio.subprocess.Process` (orchestrator) — not merely wrap `run()`.

### 2. Backends and isolators

- **`LocalBackend`** — wraps today's asyncio/`Popen` spawn in `workdir`.
  `collect`/`cleanup` are no-ops. `inherit_env=True` is honored here only. This
  is the zero-regression refactor target.
- **`SshBackend(host, workdir_root, isolator, secrets_mode, ssh_opts)`** — see
  §4.
- **Isolators** injected into any transport:
  - `BareIsolator` — argv runs as-is.
  - `DockerIsolator(image, network, memory, cpus, user)` — wraps argv via the
    vendored `ContainerRuntime` (§6): `docker run -v <tmp>:/work -w /work
    --env-file <f> --label maestro.run_id=<id> <image> <argv>`.

Four combinations arise from 2 transports + 2 isolators.

### 3. Availability split (was: local-only `is_available`)

`AgentSpawner.is_available()` checks "this system" — wrong for SSH, where the
tool lives on the executor. Split:

- `spawner.can_build_request(task, context) -> bool` — local validation of
  model/prompt/config only.
- `backend.can_run(req)` / `backend.healthcheck()` — probes `required_tools`
  (`claude`, `codex`, `spec-runner`, `python`, …) on the **target** executor.

Scheduler preflight (`maestro/scheduler.py:922`) queries the backend, not just a
local binary.

### 4. `SshBackend.run` sequence

1. `ssh host mktemp -d` → unique `maestro-exec-<run_id>` under `workdir_root`.
2. `rsync workdir/ → host:tmp/` (exclude `.git` internals per §8).
3. Secrets (`secrets_mode: injected`): write a `0600` env-file inside a `0700`
   dir on the executor (proctor pattern, §6), referenced via `--env-file` /
   `set -a; . envfile`. Never in argv, never logged.
4. Isolator builds the command (`bare` vs `docker run`, §2).
5. Stream stdout/stderr over the SSH channel into the center's `log_path`.
6. Handle tracks `ExecutionHandleRef` (`ssh_host+remote_pid` or
   `ssh_host+container_name`).
7. `collect` (§5) and `cleanup` (`shred` env-file, `rm -rf` tmp, `docker rm` by
   `run_id` label), the latter in a `finally`.

SSH preconditions (ssh binary, key/agent, `known_hosts`, `BatchMode=yes`,
`ConnectTimeout`, `StrictHostKeyChecking`) are part of `healthcheck`/fail-fast,
not just docs (mirrors `proctor/docs/remote-workers.md`).

Remote containers in MVP are **`SshBackend + DockerIsolator`** — center rsyncs
to remote tmp, then `ssh host: docker run -v remote_tmp:/work`. We do **not**
use `DOCKER_HOST=ssh://` in MVP (it is a different mechanism — local docker
client to a remote socket, no workdir sync — and mixing the two produces a
double-SSH model and volume-path confusion). `DOCKER_HOST=ssh://` may return
later as a separate, explicit backend.

### 5. `collect` as an explicit lifecycle phase

For remote backends the scheduler order becomes:

1. process terminal → 2. logs already local → 3. `handle.collect()` applies
remote changes locally → 4. cost parse (`maestro/scheduler.py:1138`) →
5. validation (§9) → 6. auto_commit/outcome.

For Mode 2, `collect` must happen **before** ex-post gates / PR / merge
(`maestro/orchestrator.py:989`, `:1001`, `:1053`). `CollectPolicy.on_failure`
defaults to `collect` to preserve local parity (a failed local run leaves
partial changes in the workdir).

Collect is **file-level apply into the existing worktree**, not a directory
swap (§8).

### 6. Vendored `ContainerRuntime` (from proctor, pinned)

Vendor `proctor/src/proctor/infra/docker.py` as
`maestro/_vendor/container_runtime.py` (origin: `proctor@<sha>`, manual bump),
following the monorepo convention (cross-repo contracts vendored as pinned
copies; cf. Maestro already vendoring `obs.py`). Reuse as-is: injected `RunCmd`
(daemon-free unit tests), per-runtime env + op timeout with kill/reap,
structured `inspect` parsing, and the `0600`/`0700` env-file secret pattern
(`proctor/src/proctor/workers/docker.py:_write_env_file`).

Extend for Maestro (proctor's `ContainerSpec` lacks these):

- `command: list[str]`, `volumes`, `workdir`, `user`, `network`, `memory`,
  `cpus`.
- **Foreground/attached run** (`create`/`start`/`attach`/`wait`) to obtain exit
  code and stream logs — proctor's `run()` is always detached (`-d`,
  `infra/docker.py:112`); we need attached.
- Labels `{maestro.run_id, maestro.backend_id, maestro.task_id}` and
  cleanup-by-label.

### 7. Mode 1 shared-workdir hazard

Mode 1 runs agents directly in a **shared** `task.workdir`
(`maestro/scheduler.py:938`, `:995`). Two remote Mode 1 tasks from the same
workdir would each snapshot, mutate remotely, and rsync back — the second
clobbering the first. Mode 2 is safer because each workstream has its own git
worktree.

MVP: remote is enabled for **Mode 2 first**. Remote **Mode 1** ships in a later
sub-phase (§Phasing 2b) requiring one of: `max_concurrent=1` on the non-local
backend; scope-aware collect with a `(workdir, scope)` lock in
`Scheduler`/`BackendPool` forbidding overlap; or patch-based collect
(`git diff --binary` on the remote, applied with conflict detection on the
center). Naive shared-workdir round-trip rsync is **not** silently enabled.

### 8. Collect apply, not swap

A "rsync to temp dir then swap the whole workdir" is unsafe for a linked git
worktree: the worktree holds a `.git` **file**/metadata, Maestro writes
untracked harness files (`maestro/workspace.py:45`, `:53`), and gates/merge read
the current local worktree — a directory swap can break worktree registration.
Instead: rsync remote → local staging; exclude `.git`, `.maestro`, logs, secret
files, backend temp; apply file-level changes into the existing worktree; for
Mode 2 optionally verify `git status`/`git diff`; for Mode 1 require
scope-aware apply/locking (§7).

### 9. Validation through the execution layer

Post-task validation currently runs as a separate **local** subprocess
(`maestro/validator.py:186`), invoked after a successful agent process
(`maestro/scheduler.py:1140`). If a task went remote/containerized *because* it
needed that environment, local validation can miss dependencies or read
pre-collect filesystem state. Task config gains
`validation_backend: same | local | <backend>`, defaulting to **`same`** for
non-local/container backends. Validation becomes a second `ExecutionRequest` on
the chosen backend.

### 10. Env / secret contract

`spawn_env()` today returns `{**os.environ, **child_env()}`
(`maestro/spawners/base.py:24`) — the full environment. For a remote executor
this leaks `GH_TOKEN`, service tokens, and shell/session vars. Replace with an
explicit split:

- `env` — explicit non-secret vars.
- `trace_env` — `child_env()` (`TRACEPARENT`, `ORCHESTRA_*`) for trace
  correlation.
- `secret_env` — allowlisted **names** read from the center environment,
  delivered via the ephemeral env-file (§4/§6).
- `inherit_env: true` — honored **only** by `LocalBackend`.
- Denylist `GH_TOKEN`, `GITHUB_TOKEN`, `GH_*` unless explicitly allowed.

Agent auth via local `~/.config`/`~/.codex`/login state is **not** supported on
a stateless executor in MVP: the executor image/host must have the CLIs
pre-installed, and model/API credentials arrive via the `secret_env` allowlist.
Config-file auth is out of scope (or a later explicit secret-file mechanism).

### 11. SQLite progress mirror (WAL-aware)

The reader opens `.executor-<prefix>state.db` read-only
(`maestro/spec_runner.py:56`). spec-runner runs in WAL mode — `workspace.py`
already cleans `.db`, `.db-wal`, `.db-shm`, `.json` (`maestro/workspace.py:196`).
So remote polling must **not** mirror only `.db`. Options: rsync `.db` +
`.db-wal` + `.db-shm` together; or run a SQLite checkpoint on the remote before
mirroring; or read a consistent copy via a remote helper. `SshBackend`
periodically mirrors the state files into a local mirror path; the existing
reader points at the mirror (reader unchanged). Mirroring only `.db` risks
stale/incomplete state or `sqlite3.DatabaseError`.

### 12. Recovery for remote runs

Recovery is currently PID-based. Mode 2 stores `process_pid`/`generation_pid`
and checks liveness via `os.kill(pid, 0)` (`maestro/orchestrator.py:76`,
`maestro/models.py:1072`). Mode 1 is coarser — after a crash, `RUNNING`/
`VALIDATING` tasks are transitioned `→ FAILED → READY` for re-execution with
**no** process check at all (`maestro/recovery.py:63`, `:127`,
`_transition_to_ready` at `:176`). For remote/Docker this would silently restart
over a possibly-live remote run.

Persist `ExecutionHandleRef` (§1) and add per-backend recovery policy:

- `LocalBackend` — existing PID behavior.
- `SshBackend` — `probe` the remote `run_id`; if the remote job is alive →
  `NEEDS_REVIEW` (or attach/monitor), never silent `READY`.
- `DockerIsolator` — `probe` container by `run_id` label/name; cleanup by
  label/name.
- Probe impossible → **fail-closed to `NEEDS_REVIEW`**, no auto-restart over a
  potentially-live remote run.

### 13. Config shape

```yaml
execution:
  default_backend: local
  secret_env: [ANTHROPIC_API_KEY, OPENAI_API_KEY]   # forwarded in 'injected' mode
  backends:
    local:
      transport: local
      isolation: bare
      max_concurrent: 3
    sandbox:
      transport: local
      isolation:
        type: docker
        image: ghcr.io/andrei-shtanakov/maestro-runner:2026-07-21
        network: none
        memory: 8g
    gpu-box:
      transport:
        type: ssh
        host: gpu-box                # ssh config alias or [user@]host[:port], NOT ssh://
        workdir_root: /var/tmp/maestro
        connect_timeout_s: 10
      isolation: bare
      secrets_mode: injected         # injected | preprovisioned
      max_concurrent: 2

workstreams:
  - id: api
    backend: gpu-box
tasks:
  - id: refactor
    backend: sandbox
```

No `execution` section → `local + bare`, old behavior. Routing: a
workstream/task carries an optional `backend:`; otherwise `default_backend`.

### 14. Observability

Propagate `TRACEPARENT` into the executor env (via `trace_env`) so remote runs
correlate through the vendored `obs.py` (W3C TraceContext + OTel already
present). Add spans: `execution.dispatch` (backend/host/isolation),
`execution.transfer` (bytes in/out, duration), `execution.run`. Record the
backend/host each task ran on (extends `DOGFOOD_LOG` records).

### 15. CLAUDE.md drift note

`maestro/CLAUDE.md:155` records an old decision: "callbacks from spec-runner,
state polling deprecated." This design deliberately reintroduces polling for
remote/NAT executors (§11). Update CLAUDE.md so documentation does not
contradict the implementation.

## Phasing

- **Phase 0 — Contract + `LocalBackend` (pure refactor, 0 regression).**
  `ExecutionRequest`/`TaskHandle`/`ExecutionBackend`/`LocalBackend`. Spawners:
  `spawn(...) -> Popen` → `build_request(...) -> ExecutionRequest`. Scheduler:
  `RunningTask.process` → `handle`. Orchestrator: `RunningWorkstream.process` →
  `handle`. Availability split. Validator routed through `LocalBackend`.
  Acceptance: all current tests green; golden tests on argv/env/log/cwd for
  claude/codex/aider/opencode/announce; `poll/wait/terminate/kill` parity; no
  `execution` config = old local path.
- **Phase 1 — Local Docker isolation.** Vendored + extended `ContainerRuntime`,
  `DockerIsolator`, local bind-mount workdir, image/tooling prerequisites,
  `validation=same`. Acceptance: opt-in docker integration test; UID/file
  ownership; network/memory flags; cleanup by `run_id` label.
- **Phase 2a — SSH backend, Mode 2 only.** `SshTransport`, rsync to remote tmp,
  remote run, WAL-aware progress mirror (`.db + -wal + -shm`), collect into the
  existing worktree before ex-post gates, handle refs + probe/cleanup by
  `run_id`, secret allowlist env-file. Acceptance: localhost-SSH e2e workstream;
  mirrored progress visible; remote crash/transport-fail classified; cleanup
  test with a fake runner.
- **Phase 2b — Mode 1 remote.** Scope-aware/patch collect, `(workdir, scope)`
  locking, conflict detection, validation through the same backend. Do not
  silently enable shared-workdir full rsync.
- **Phase 2c — SSH + Docker isolation.** `DockerIsolator` over `SshTransport`,
  remote tmp mounted into the container, image/tooling preflight, container
  cleanup by label.
- **Phase 3 (deferred) — routing maturity / registry / queues.** NATS/Celery/VM
  provisioning stay out of MVP.

## Non-goals

- Elastic cloud worker fleet, message brokers/queues (NATS/Celery), VM
  provisioning (Vagrant/Ansible) — deferred to Phase 3.
- `DOCKER_HOST=ssh://` remote-socket mode in MVP (see §4).
- Remote `spec-runner plan --full` generation — stays local (see MVP
  guarantees).
- Config-file/login-state agent auth on stateless executors (see §10).
- Runtime coupling to `proctor` — we vendor a pinned copy of `ContainerRuntime`,
  we do not delegate execution to proctor's NATS fleet (that would invert the
  `proctor → maestro` dependency and violate polyrepo `repo-boundaries`).
