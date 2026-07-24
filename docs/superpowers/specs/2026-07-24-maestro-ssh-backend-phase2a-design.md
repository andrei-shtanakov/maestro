# SSH execution backend — Phase 2a (Mode 2 only)

Child of `docs/superpowers/specs/2026-07-21-maestro-distributed-execution-design.md`
(§2, §4, §5, §8, §11, §12, §13). This spec adapts that parent design onto the
**shipped Phase-1 execution layer** (`maestro/execution/`, PR #98 / `661aace`)
and folds in a round of design review (2026-07-24). Where the parent design and
this spec differ, **this spec wins** — it reflects the code as it actually
shipped and the review corrections below.

## Context

After Phase 1, `maestro/execution/` exposes a frozen transport-agnostic
contract that both modes dispatch through:

- `ExecutionRequest` / `ExecutionResult` / `ExecutionHandleRef` /
  `CollectPolicy` / `ProgressMirrorPolicy` (`execution/models.py`) — already
  carry the SSH-shaped fields (`secret_env`, `progress_mirror`, `status_marker`,
  `workdir_mirror_path`, `state_mirror_path`), unused until now.
- `TaskHandle` protocol: `poll()` (**sync, cached-only, no I/O**), `wait()`,
  `terminate()`, `kill()`, `collect()`, `cleanup()`, `os_pid`
  (`execution/backend.py`).
- `ExecutionBackend` protocol: `healthcheck()`, `can_run()`, `run()`,
  `probe()`.
- `LocalBackend` = one transport + an injected `Isolator` (`BareIsolator` /
  `DockerIsolator`). Docker shipped as `LocalBackend(DockerIsolator, docker=…)`
  because `docker run` is still a **local attached process** whose exit code is
  the container's — so `poll()` delegates to a local `Popen`.
- Durable identity: `execution_handles` table + atomic `start_execution`
  (READY→RUNNING CAS + row insert in one txn) + monotonic
  `mark_execution_state` (`prepared → running → terminal → cleaned`).
- Fail-closed recovery: `probe_execution` / `gc_terminal_handle`
  (`execution/docker_recovery.py`), wired into both modes.

**Why SSH is a new backend, not a new isolator.** An SSH run's process lives on
a *remote* host. `poll()` cannot delegate to a local `Popen.poll()`, and the run
must survive a dropped SSH channel. So `SshBackend` is a **new
`ExecutionBackend`** (peer of `LocalBackend`), whose `run()` starts a local
asyncio *monitor* task that tails a remote status marker and updates a cached
exit code; `poll()` returns that cache.

## Goal

Run Mode-2 (Orchestrator) spec-runner workstreams on a remote host over SSH:
rsync the worktree to a remote tmp dir, launch spec-runner detached from the SSH
channel, mirror live progress back (WAL-safe), collect the final worktree
changes back **before** ex-post gates/PR/merge, and recover safely across a
center restart. Isolation is **`bare` only** in this phase (SSH + Docker is
Phase 2c).

### Hard requirements (carried from parent MVP guarantees)

- **No `execution` config → `local + bare`, byte-identical to today.**
- **A remote executor never receives GitHub credentials.** `GH_TOKEN` /
  `GITHUB_TOKEN` / `GH_*` denylisted; git/PR/merge stays on the center.
- **`spec-runner plan --full` (generation) stays local.**
- **Mode 1 is out of scope.** Selecting an SSH backend from a Mode-1 config
  **fails fast as unsupported until Phase 2b** — the shared-workdir hazard
  (parent §7) is not silently enabled.

## Design

### A. Config: `backends:{}` registry + legacy Docker shim

Replace the narrow Phase-1 `execution: {default_backend, docker}` with the
parent-design registry (§13), materializing the transport × isolation axes:

```yaml
execution:
  default_backend: local
  secret_env_defaults:            # optional; NOT auto-applied to any backend
    - ANTHROPIC_API_KEY
  backends:
    gpu-box:
      transport:
        type: ssh
        host: gpu-box             # ssh config alias or a bare hostname; NOT ssh://, NOT user@host:port
        user: null                # optional; passed safely, never folded into `host`
        port: null                # optional; passed via `-p`, never `host:port`
        workdir_root: /var/tmp/maestro
        connect_timeout_s: 10
        ssh_opts: ["-o", "ServerAliveInterval=15"]   # whitelisted; see B
      isolation:
        type: bare
      secret_env:                 # per-backend allowlist (explicit)
        - ANTHROPIC_API_KEY
    build-box:
      transport: { type: ssh, host: build-box, workdir_root: /var/tmp/maestro }
      isolation: { type: bare }
      secret_env: [ANTHROPIC_API_KEY]
```

Rules (all enforced at config load, fail-fast):

- `local` is a **built-in** backend — no declaration required.
- **Per-backend `secret_env`.** A single shared allowlist is a security hazard:
  it would fan every secret out to every SSH host. `secret_env` lives on each
  backend and is authoritative for that backend. `secret_env_defaults` may exist
  at the `execution` level but is **never auto-applied**: a backend inherits it
  **only** when it sets `inherit_secret_defaults: true` (default `false`), in
  which case its effective allowlist is `secret_env_defaults ∪ secret_env`.
  No implicit send of identical secrets to all hosts.
- **Legacy Docker shim.** A Phase-1 `execution.docker: {...}` (incl.
  `docker.secret_env`) normalizes into an internal `backends.docker` with
  `transport: local`, `isolation: docker`, `secret_env` carried over. The
  canonical form is `backends.<name>: {transport: local, isolation: {type:
  docker, …}}`.
- **Legacy + canonical simultaneously → config error.** If both
  `execution.docker` and an explicit `backends.docker` (or any explicit docker
  backend colliding with the shimmed name) are present, raise — no implicit
  precedence.
- `default_backend` and any entity `backend:` reference a registry name;
  unknown name → fail-fast. Backend names do **not** encode transport
  (`gpu-box`, not `ssh-gpu-box`).
- **Mode-1 SSH guard.** The `ProjectConfig` (Mode 1) may parse the registry, but
  resolving/selecting an `ssh`-transport backend in Mode 1 fails fast:
  "SSH backends are Mode-2 only until Phase 2b."

Config models: introduce `BackendSpec { transport: TransportSpec, isolation:
IsolationSpec, secret_env, inherit_secret_defaults, max_concurrent? }`,
`TransportSpec` = `LocalTransport | SshTransport`, `IsolationSpec` =
`BareIsolation | DockerIsolation`. The GH denylist validator
(`exec_config._is_denylisted`) moves to `BackendSpec.secret_env`.

- **Structured host, not a single token.** OpenSSH does not treat an arbitrary
  `[user@]host[:port]` as one safe host token. `SshTransport` uses **separate
  fields**: `host` (an ssh-config alias or a bare hostname), optional `user`,
  optional `port`. `user` is passed as `-l <user>` (or a validated `user@host`),
  `port` via `-p <port>` — never string-concatenated into `host`. A `host`
  containing `@` or `:` is rejected at config load.

### B. `SshBackend` + `SshCli`

New files `maestro/execution/ssh_backend.py` and `maestro/execution/ssh_cli.py`.

- **`SshCli`** — a thin wrapper over an **injectable command runner** (mirrors
  `DockerCli`), so the whole backend is unit-testable **without a real sshd**.
  Owns argv construction for `ssh`/`rsync` with a **security-options whitelist
  and guaranteed precedence**: caller `ssh_opts` are validated against an
  allowlist and appended *before* Maestro's non-negotiable options, so a user
  option can never disable:
  - `-o BatchMode=yes` (no interactive/password prompts),
  - host-key verification (`-o StrictHostKeyChecking` is Maestro-set; user
    cannot force `no`/`accept-new` off),
  - `-o ConnectTimeout=<connect_timeout_s>`,
  - `-o PasswordAuthentication=no` / `-o KbdInteractiveAuthentication=no`.

  Any `ssh_opt` that attempts to set one of these guarded keys is rejected at
  config load.
- **`healthcheck()`** — fail-fast SSH preconditions: reject empty host; run
  `ssh <guarded-opts> host true` and require success within the connect
  timeout. Mirrors `proctor/docs/remote-workers.md` preconditions.
- **`can_run(req)`** — `ssh host 'command -v <tool>'` for each
  `req.required_tools` (e.g. `spec-runner`); missing tools → `CapabilityResult(
  ok=False, missing_tools=…)`.

### C. `run()` sequence (safe launcher, process groups, identity)

1. `ssh host mktemp -d <workdir_root>/maestro-exec-<execution_id>.XXXX`
   (uses **`execution_id`**, the uuid, not the entity `run_id`).
2. **Remote git materialization via a bundle (fixed strategy, not plan-time).**
   A Mode-2 worktree is a *linked* worktree — its `.git` is a file pointing into
   the center's main `.git/worktrees/<name>`, absent remotely. spec-runner needs
   a self-contained git repo, so the sequence is:
   1. the center creates a **git bundle** from the authoritative repo at the
      worktree's HEAD (`git bundle create <bundle> HEAD` / the feature branch);
   2. the bundle is transferred to the remote tmp dir — it carries **no GitHub
      credentials** and no remote URLs;
   3. remote `git clone <bundle> <tmp>/repo` produces a self-contained repo;
   4. the local worktree contents — **including initially dirty/untracked
      files** — are rsync'd over the clone (`rsync -a`, excluding `.git`,
      `.maestro`, logs, backend temp), reproducing the exact working state;
   5. the remote `.git` is **never** collected back;
   6. final file changes are computed against the **pre-run content baseline**
      (E), not remote git history.

   This keeps the center the sole owner of branch/commit/PR/merge and needs no
   GitHub auth on the executor.
3. **Secrets via protected temp file, never in argv.** Build the `0600`
   env-file **locally** in a `0700` temp dir from this backend's `secret_env`
   allowlist (reuse the control-char validation currently in
   `DockerIsolator.materialize`, extracted to a shared
   `execution/secret_file.py` helper), then deliver it to the remote `0700`
   dir via `rsync`/stdin. Values never appear in any `ssh`/`rsync` argv and are
   never logged. Referenced remotely via `set -a; . <envfile>; set +a`.
4. **Verified launcher over stdin, detached, process-group identified.** The
   remote command is a fixed launcher **script piped over stdin** to
   `ssh host bash -s`, **not** assembled by shell interpolation of argv. Remote
   paths and the `execution_id` are passed as **positional arguments**
   (`bash -s <execution_id> <tmp> -- "$@"`), never interpolated into the script
   text/heredoc. The script:
   - writes an **ownership marker** `<tmp>/.maestro-owner` containing the
     `execution_id` (gates the guarded recursive delete in D);
   - `setsid` the job into its own **session/process group** so the whole tree
     can be signalled and descendants can't be orphaned;
   - writes both `pid` and `pgid` to `<tmp>/<execution_id>.pid`;
   - on exit writes `<tmp>/<execution_id>.status` **atomically** (write to
     `.status.tmp`, `fsync`, `rename`) as `{pid, pgid, exit_code,
     completed_at}`.

   argv (`req.argv`, e.g. `spec-runner run --all …`) is passed to the script as
   a properly-quoted array (`"$@"` / `printf %q`), so spaces, quotes, and
   hostile tokens can't break out.
5. A local asyncio **monitor** task (D) tails the job's output over SSH into the
   center's `log_path` and polls the status marker to update the cached
   `poll()`.
6. **Durable identity.** A delimited `"ssh:<host>:<run_id>"` string is rejected
   (ambiguous to parse). `transport_ref` is stored as an **opaque, versioned
   JSON encoding** (`{"v":1,"transport":"ssh","host":…,"port":…,"remote_dir":…,
   "status_marker":…}`) — treated as an opaque blob, never string-split. The
   **source of truth for recovery lookup** is the **dedicated columns** added in
   G: `remote_host`, `remote_dir`, `status_marker` (`<tmp>/<execution_id>.status`),
   keyed by `execution_id`. `probe(ref)` after a transport drop reads the marker
   (or `ssh host kill -0 -<pgid>`) without a live channel.

### D. `SshTaskHandle` and the monitor

`poll()` is **sync, cached-only** (contract invariant): returns the exit code
the monitor last published, `None` while running. `os_pid` → `None` (remote).

The monitor task must:

- **Track byte offset** of the remote log so a reconnect (`ssh … tail -c +N`)
  never duplicates already-mirrored bytes into `log_path`.
- **Survive transient transport failures** with bounded exponential backoff;
  distinguish three states: *remote still running*, *terminal marker present*,
  *probe currently unavailable*.
- **Signal terminal only after** reading the atomically-published `.status`
  marker (never infer completion from a dropped channel alone).
- **Tear down the tail + progress-mirror tasks before** any remote-dir cleanup.

`wait()` awaits the monitor's terminal signal → `ExecutionResult` (exit code
from the marker; `timed_out` if `req.timeout_seconds` elapsed first).
`terminate(grace)` / `kill()` signal the **process group** (`ssh host kill
-TERM -<pgid>` then `kill -KILL -<pgid>`), not just the wrapper pid. **No broad
`pkill` by run_id** — it could match a foreign process.

`cleanup()` removes the remote tmp dir and best-effort `shred`s the env-file
*when available* (never depends on it), only after collect is confirmed (G).
**Guarded recursive delete:** before any remote `rm -rf`, verify (a) the target
`remote_dir` is **under** the configured `workdir_root`, and (b) it contains an
**ownership marker** (`<tmp>/.maestro-owner`, written in C.4) whose content
matches the expected `execution_id`. A persisted path alone is **not**
sufficient authorization for a recursive delete — both checks must pass or the
delete is refused and the row left for review.

### E. Collect — baseline diff, atomic apply, inside finalization

Mode-2 SSH sets `CollectPolicy(mode="whole_worktree", conflict_policy="fail",
on_failure="collect")` (Phase-1 orchestrator currently sends `mode="none"`).

- **Pre-run baseline.** Before step C.2 rsync-out, capture a baseline of the
  local worktree (content hashes + the set of tracked/untracked paths). Without
  it, a divergence check can't tell whether the local worktree was mutated in
  parallel.
- **Collect** = rsync `host:<tmp>/ → local staging` (exclude `.git`,
  `.maestro`, logs, secret files, backend temp), then apply **file-level** into
  the *existing* worktree (never a directory swap — the worktree holds a `.git`
  file/metadata, parent §8) in **two strictly separated phases**. "No partial
  apply" is stronger than atomic-per-file and requires this split:

  1. **Preflight (zero worktree mutation).** Compute the remote diff vs the
     baseline; check **all** conflicts; validate **all** forbidden-path /
     symlink / path-traversal rules; stage every replacement file and the
     deletion list. Any failure here → **no local change whatsoever**,
     workstream → NEEDS_REVIEW.
     - supports **modified**, **new**, and **deleted** files;
     - tolerates an initially **dirty** worktree;
     - `conflict_policy="fail"` triggers **only** when a path the *remote*
       changed also diverged locally from the baseline (parallel local
       mutation);
     - **symlink / path-traversal protection**: reject any staged path that
       escapes the worktree root or is a symlink pointing outside it;
     - **forbid applying** `.git/**`, `.maestro/**`, backend temp, and secret
       files regardless of manifest.
  2. **Apply with a rollback journal.** Back up every affected local path
     (originals into a journal dir), then apply all replacements/deletions
     (atomic per file: write-temp-in-target-dir + `rename`). On **any** runtime
     error mid-apply, **restore all backups** from the journal (best-effort
     full rollback), then route to NEEDS_REVIEW. The journal is deleted only
     after a fully successful apply.
- On conflict / preflight failure → **zero** local changes; workstream →
  NEEDS_REVIEW; staging + remote tmp preserved. On an I/O error during apply →
  rollback restores the worktree; if rollback itself cannot fully complete, the
  workstream is NEEDS_REVIEW and the journal + staging + remote tmp are all
  preserved for manual recovery.
- **Single-owner.** Collect stays *inside* `finalize_handle` (it already calls
  `handle.collect()`); we do **not** add a second collect call in the success
  continuation. The orchestrator continuation is gated on finalization having
  *collected successfully* (see G/I).

### F. WAL-safe progress mirror — remote SQLite backup snapshot

`ProgressMirrorPolicy(kind="spec_runner_sqlite", local_dir=<mirror>,
interval_seconds≈2)`. The existing reader (`spec_runner.read_executor_state`)
is pointed at `local_dir` and left otherwise unchanged.

**Rejected:** sequentially rsyncing `.db` + `.db-wal` + `.db-shm`. The three
files change between copies; an inconsistent combination can *sometimes open and
return stale/partial data* rather than raising `DatabaseError`, and `.db-shm` is
a machine-local shared-memory index — copying it across machines is not a valid
snapshot protocol.

**Mechanism** (per mirror tick):

1. On the remote, produce a **consistent snapshot** of the live DB into a temp
   file via Python `sqlite3.Connection.backup()`. The helper **script is piped
   over stdin** (`ssh host python3 - <src_db> <dst_snapshot>`) and receives the
   source/destination paths as **positional `sys.argv` arguments** — remote
   paths are **never** interpolated into the script text/heredoc. Safe under an
   active writer; **no new runtime dependency** (Python is already required for
   spec-runner).
2. Atomically `rename` the snapshot to a stable name on the remote.
3. `rsync` the **single** snapshot file to the center.
4. **Atomic replace** into `local_dir`.
5. The unchanged reader reads the snapshot.

`DatabaseError` handling in the reader stays as *defense in depth*, not as the
consistency mechanism.

### G. Recovery — durable `collected` state (load-bearing for SSH)

A remote terminal marker is **not** equivalent to a completed Maestro
finalization. Crash sequence:

```
remote process completes → status marker written → CENTER CRASHES before collect
```

The remote worktree then holds **unapplied** changes; a naive cleanup on
recovery would lose them. So the execution state machine gains a durable
`collected` step:

```
prepared → running → terminal → collected → cleaned
```

**Conditional cleanup — a change to the shared `finalize_handle` contract.**
The shipped `finalize_handle` (`execution/finalize.py`) calls `cleanup()`
**unconditionally** after `collect()`, even when collect raised. For SSH that
would `rm -rf` the remote tmp exactly when its unapplied changes must be kept.
The contract changes so cleanup is **gated on collect success**:

```python
@dataclass
class FinalizationResult:
    execution: ExecutionResult
    collect_error: str | None = None
    cleanup_error: str | None = None
    collect_succeeded: bool = False
    cleanup_attempted: bool = False

    @property
    def cleaned(self) -> bool:
        return self.cleanup_attempted and self.cleanup_error is None
```

Rule in `finalize_handle`:

- `collect()` succeeds → mark `collected`, then call `cleanup()`
  (`cleanup_attempted=True`); on success mark `cleaned`.
- `collect()` fails / conflicts → `cleanup()` is **not** called; remote tmp +
  staging are preserved; the monitor routes the workstream to **NEEDS_REVIEW**.
- **Docker / local** `collect()` is a no-op that always succeeds →
  `collect_succeeded=True`, cleanup runs as before — **zero behavior change**.

This makes the `terminal → collected → cleaned` transitions *observable* (driven
by real collect/cleanup outcomes), not merely declarative. Both modes'
`_monitor_running` call sites (which today read `fin.cleaned`) branch on
`fin.collect_succeeded` before marking `collected`/`cleaned` and before entering
any gate/PR flow.

- **Schema migration #8** (`_migrate_*`): extend the `execution_handles.state`
  CHECK to include `'collected'`, and `ALTER TABLE ADD COLUMN` for
  `remote_host`, `remote_dir`, `status_marker`, `collected_at`. (SQLite CHECK
  change ⇒ table rebuild in the migration; existing rows map cleanly.)
- **Docker parity.** Docker's `collect()` is a no-op, so its finalization marks
  `collected` immediately after `terminal` (then `cleaned`). This keeps the
  shared table honest with **zero behavior change** for docker (an extra
  monotonic mark).
- **`SshBackend.probe(ref)` fail-closed matrix** (probe deletes nothing):
  - marker absent **+** process/group alive (`kill -0 -<pgid>`) → `NEEDS_REVIEW`;
  - probe unavailable / ambiguous (host unreachable, multiple matches) →
    `NEEDS_REVIEW`;
  - marker `terminal` but **collect not confirmed** (`state != collected`) →
    `NEEDS_REVIEW`, **remote tmp preserved**, exact diagnostic to the operator;
  - only a handle already `collected` may be `cleaned` (leftover remote tmp GC).
    `get_open_execution_handles` (recovery query) is widened to also select
    `collected` (non-`cleaned`) rows so their leftover remote tmp is GC'd — the
    parallel of docker's `terminal → cleaned` GC sweep.
- **Recovery `collect`/`resume` is a follow-up.** Phase 2a's recovery
  preserves the remote tmp and routes to review with a precise diagnostic; it
  does not attempt to auto-resume the collect. (The happy path still collects
  normally inside `finalize_handle`.)

### H./I. Wiring

- **Shared config layer.** Registry parsing + legacy-docker normalization live
  in `execution/exec_config.py`, shared by `ProjectConfig` (Mode 1) and
  `OrchestratorConfig` (Mode 2). `BackendResolver._build` gains an `ssh` branch:
  `SshBackend(SshCli(transport, guarded_opts), isolator=BareIsolator(),
  backend_id=name, secret_env=spec.secret_env)`.
- **Mode-2 only.** The resolver refuses `ssh` transport when constructed for
  Mode 1 (guard B / A).
- **Progress mirror.** `RunningWorkstream` carries the mirror `local_dir`;
  `_update_progress` reads from it (the reader is pointed at the mirror, not the
  live remote spec dir).
- **Finalization gates the continuation.** In `_monitor_running`, after
  `ensure_finalize_task`, the workstream may proceed to ex-post gate / PR /
  merge **only if** `fin.collect_succeeded`. On collect error/conflict:
  `NEEDS_REVIEW`, preserve remote tmp + staging, do **not** enter the PR/gate
  flow. Execution state is marked `terminal` → `collected` (only on
  `collect_succeeded`) → `cleaned` (only on `fin.cleaned`), per the conditional
  cleanup contract (G).
- **Observability.** Propagate `TRACEPARENT` via `trace_env` into the remote
  env; add spans `execution.dispatch`, `execution.transfer` (bytes in/out), and
  record host/backend on the workstream (parent §14).
- **CLAUDE.md drift note** (parent §15): the "state polling deprecated" line is
  updated — polling is deliberately reintroduced for remote executors.

### J. Testing

Unit (injected fake-SSH runner, **no real sshd**):

- run / poll(cached) / wait / terminate(**process-group**) / kill / cleanup /
  probe;
- **shell quoting**: argv with spaces, quotes, `$()`, newlines survives to the
  remote intact (verified launcher over stdin, positional args);
- **`ssh_opts` cannot disable** `BatchMode` / host verification / connect
  timeout / password-auth-off (guarded precedence);
- **host fields**: a `host` containing `@` or `:` is rejected; `user`/`port`
  render as `-l`/`-p`, never concatenated into `host`;
- secret env-file: `0600`/`0700`, control-char rejection, GH denylist, value
  never in argv;
- **reconnect tail** resumes at byte offset — no duplicated log bytes;
- **process-group terminate** kills descendants (fake tree);
- **collect (two-phase)**: preflight conflict on a remote-changed path →
  **zero** local changes; I/O error mid-apply → **rollback journal restores**
  the worktree; remote **deletion** applied; **symlink escape rejected**;
  dirty-worktree tolerated; atomic per-file apply;
- **conditional cleanup**: collect failure ⇒ `cleanup()` **not** called, remote
  tmp preserved; collect success ⇒ `collected` then `cleaned`; docker/local
  no-op collect ⇒ unchanged;
- **guarded rm -rf**: refused when `remote_dir` is outside `workdir_root` or the
  `.maestro-owner` marker's `execution_id` mismatches;
- **recovery**: crash after terminal marker but before collect → remote tmp
  **preserved**, workstream → `NEEDS_REVIEW`; marker-absent + alive →
  `NEEDS_REVIEW`;
- **progress mirror**: `sqlite3.backup()` snapshot (positional-arg helper over
  stdin) is readable under an active remote writer; mirror atomic-replace;
- config: registry parse, legacy-docker shim + collision error, Mode-1 SSH
  fail-fast, unknown-backend fail-fast, per-backend `secret_env` +
  `inherit_secret_defaults`.

Gated opt-in e2e (real localhost sshd, skipped by default like the docker
integration gate): a Mode-2 workstream over `ssh localhost`, mirrored progress
visible, remote-crash classified, cleanup via a fake runner.

**Verification discipline (operational learning):** verify locally with
**targeted foreground** runs (specific files / `-k` halves) + `pyrefly check` +
`ruff`; **never** offload the whole suite to a background wait (a workspace
watchdog kills long background `pytest` runs); rely on PR CI for the full suite.

### K. Non-goals (Phase 2a)

- Mode 1 remote (Phase 2b); SSH + Docker isolation (Phase 2c);
  validation-backend routing §9 (separate follow-up, and a Mode-1 concern);
  recovery auto-`collect`/resume (follow-up); `DOCKER_HOST=ssh://`;
  config-file/login-state agent auth on stateless executors; publishing a
  `maestro-runner` image; routing/registry maturity/queues (Phase 3).

## Acceptance

- No `execution` config → `local + bare`, all current tests green.
- Registry + legacy-docker shim parse; collision and Mode-1-SSH selection fail
  fast.
- A localhost-SSH Mode-2 workstream runs, mirrors progress (snapshot-based),
  collects changes into the worktree before ex-post gates, PRs, and reaches
  `DONE`; execution states walk `prepared → running → terminal → collected →
  cleaned`.
- Injected-runner unit suite (J) green; `pyrefly check` clean; `ruff` clean.
- Recovery: center crash between terminal marker and collect leaves remote tmp
  intact and routes the workstream to `NEEDS_REVIEW` with a precise diagnostic;
  no silent re-run over a possibly-live remote job.
