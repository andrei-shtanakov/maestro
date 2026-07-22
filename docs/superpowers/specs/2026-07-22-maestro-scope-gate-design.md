# Executable scope-gate — design

- **Date:** 2026-07-22
- **Status:** approved (brainstorming)
- **Scope:** idea #7a from `../prograph-vault/authored/notes/2026-07-22-ideas-from-ai-repos-research.md`
  (interfaces-ledger half, #7b, is explicitly out of scope)

## 1. Problem

A workstream declares a `scope` (list of file/dir globs). Today that scope is
enforced only two ways, and neither guarantees the workstream's *actual*
changes stay inside it:

- **Preflight** (`maestro/preflight.py`) checks scope *overlap between*
  workstreams before the run. It never checks whether a workstream's committed
  diff adheres to its own declared scope.
- **WS-006 gates** (`maestro/gates.py::GateKeeper`) are **opt-in** and delegate
  classification to an external `steward risk-classify` binary. The ex-post
  gate already computes the actual diff and can emit a `scope_violation` flag —
  but only if steward is configured (`config.gates is not None`) and only as a
  risk-tier judgement, not a deterministic structural check.

**Gap:** when `config.gates is None` (the default), nothing stops a workstream
from committing changes to files *outside* its declared scope and merging them
into the base branch. Scope containment is currently a prompt-level expectation,
not an executable invariant.

## 2. Goal

Make scope containment a deterministic, always-on, executable gate:

> A workstream's own committed diff (from its branch-point,
> `merge-base(base, HEAD)..HEAD`, minus orchestrator-managed artifacts) must
> touch only paths matched by its declared `scope`. Any escape blocks the merge
> (exit≠0 / `NEEDS_REVIEW`), with a deliberate operator override.

The surface is the **branch-point** diff, not a two-dot `base..HEAD` tree
delta — see §3.2 for why (a sibling workstream advancing `base` would otherwise
inject false escapes).

This is the codex-build `check_scope.py` philosophy: declared scope = allowlist,
any path outside → hard fail — no LLM, no risk model, no human in the common
path.

## 3. Architecture

Three parts, split by responsibility so the matching logic is pure and
transport-agnostic while the git dependency stays at the edge:

Module split (keeps the git import out of the pure matcher):

- `maestro/scope_gate.py` — the pure `find_escapes` core.
- `maestro/changed_paths.py` — the git changed-paths source + the
  orchestrator-managed filter.

### 3.1 Pure core — `maestro/scope_gate.py`

No git, no DB, no filesystem. One function:

```python
def find_escapes(
    changed_paths: list[str],   # normalized, repo-relative, POSIX
    scope: list[str],           # normalized glob patterns
) -> list[str]:
    """Return the subset of changed_paths matched by no scope pattern.

    Empty result == containment holds. Empty scope == skip (returns []).
    """
```

Fully unit-testable on lists of strings. Callers pass **already-normalized**
paths and patterns; the core does not normalize, glob the filesystem, or shell
out.

### 3.2 Changed-paths source — `maestro/changed_paths.py`

Kept explicitly separate from the pure matcher:

```python
async def changed_paths_since(
    base_ref: str, head_ref: str, repo_root: Path
) -> list[str]:
    """The workstream's OWN committed changes, from its branch-point.

    merge_base = `git merge-base base_ref head_ref`
    paths      = `git diff --no-renames -z --name-only <merge_base> <head_ref>`

    Orchestrator-managed artifacts filtered out; normalized to repo-relative
    POSIX paths. Output is NUL-split (`-z`) rather than line-split, so paths
    containing newlines/control chars parse correctly (cheap robustness; not a
    scenario current scopes rely on).
    """
```

Two git decisions, both load-bearing for an always-on gate:

- **Branch-point, not two-dot `base..HEAD`.** `git diff A..B` is
  `diff(tree_A, tree_B)`, not a range. Once a sibling workstream merges into
  `base` (`_merge_into_base` advances it mid-run), `diff(base_tip, head)` would
  surface that sibling's out-of-scope paths as a reverse-diff and flag them as
  *this* workstream's escapes. Diffing from `merge-base(base, HEAD)` isolates
  exactly the workstream's own commits.
- **`--no-renames`.** Not passing `-M` is insufficient: a user's
  `diff.renames=true` git config re-enables rename detection. Force
  `--no-renames` so a rename is always reported as delete(old)+add(new) on every
  machine (see §4).

The `git diff --name-only` + `_orchestrator_managed` filter logic currently
lives inside `GateKeeper` (`gates.py::_git_diff_paths`, `_orchestrator_managed`).
Move it here so both the always-on scope-gate and `GateKeeper` use one
implementation — the scope-gate must **not** depend on a `GateKeeper` instance
existing (it only exists when steward gates are configured).
`GateKeeper.evaluate_ex_post()` delegates to `changed_paths_since` and drops its
own `_git_diff_paths`.

**Behavior change (intentional):** `GateKeeper`'s ex-post surface shifts from
two-dot to branch-point + `--no-renames`. This is a correctness fix — the
steward ex-post gate has the same false-escape-after-base-advances latent bug —
and is covered by a regression test (§9). One source of truth for "what did this
workstream change".

**Transport seam (Phase 1+):** for remote/Docker backends the changed-path
source moves to the execution-layer `collect` step (`CollectPolicy`,
`CollectResult`) rather than a local `git diff`. The pure core is unchanged;
only the source is swapped. This design wires the Phase-0 local source and
leaves that seam explicit — no contract changes now.

### 3.3 Callers

- Orchestrator ex-post edge (always-on gate).
- `maestro check-scope` CLI (operator/CI inspection).

## 4. Glob semantics (pure core)

Pathlib-`Path.glob`-compatible **string** matching (not filesystem globbing —
`git diff` reports deleted paths that no longer exist on disk, so
`repo.glob()` would miss them). Implemented via a small glob→regex translator,
**no new dependency**.

- `**` matches any number of path segments (including zero).
- `*` matches within a single segment; it does **not** cross `/`.
- A pattern with no glob metacharacters is an **exact match only**:
  `src/foo.py` matches exactly `src/foo.py`.
- `src/**` covers `src/foo.py` and `src/a/b.py`.
- `dir/**` matches the **contents** of `dir`, not `dir` itself. Matching `dir`
  itself would need `dir` or `dir/**` as a separate pattern. (Minor: `git diff`
  reports files, not bare directories.)
- Matching is case-sensitive (POSIX paths).

**Normalization** happens in the source layer *before* the core: strip a
leading `./`, use POSIX separators, ensure repo-relative. The core assumes
normalized input on both sides.

**Empty scope → skip:** `find_escapes(paths, [])` returns `[]`. Enforcing an
empty scope would be a breaking always-on failure, and preflight already emits a
`scope-empty` warning for it.

**Renames:** the source explicitly passes `--no-renames` (§3.2), so
`--name-only` reports a rename as a delete (old path) + add (new path) on every
machine, regardless of the user's `diff.renames` git config. The gate checks
*every* affected repo-relative path independently — it does not try to
understand rename semantics. Stricter, simpler, and correct for containment: if
either endpoint of a rename is outside scope, that endpoint is an escape.

## 5. Orchestrator integration

Insert into `_handle_success`, **before** `_gate_ex_post` — the cheap
deterministic check runs first (fail fast); the optional steward risk gate runs
after. Both must pass to reach `MERGING`.

**Always-on:** the scope-gate runs regardless of `config.gates`. This differs
from `GateKeeper`, which is opt-in. It is an intentional behavior change:
workstreams whose committed diff escapes their declared scope now block by
default. Workstreams with correct/loose scopes are unaffected.

**On escape:** `RUNNING → FAILED → NEEDS_REVIEW` — the same two-step arc the
existing ex-post block path uses (`_gate_ex_post` writes `FAILED` then
`NEEDS_REVIEW`), so recovery and stats behave identically (`stats.failed += 1`).
The worktree is left **intact** for inspection. The `error_message` MUST carry
the approval marker (see §5.1) — without it the override cannot be recorded.

The steward ex-post gate is untouched; its own risk-model `scope_violation` flag
now sits above a deterministic floor. Independent sources — acceptable
redundancy.

### 5.1 Block reason & approval marker

The block reason is **marker-bearing**. `maestro workstream-approve` records a
durable approval only when `error_message` contains the marker parsed by
`gates.parse_approval_marker()` — format
`gates:approval-required phase=<phase> sha=<sha>` (`APPROVAL_MARKER_PREFIX`).
Without it, approval is a plain requeue that records nothing, and the H-6 resume
loops forever on the same escape. So the scope-gate emits:

```
scope escape: a.py, b.py, c.py, ... (+N more); re-queue to approve. gates:approval-required phase=ex_post sha=<worktree-head-sha>
```

- **Truncation applies to the path list only, never the marker** — the marker
  is mandatory and always complete. `N` counts the omitted paths.
- Marker construction/parsing is **shared with `GateKeeper`**, not a second
  format: reuse `APPROVAL_MARKER_PREFIX` / `parse_approval_marker` (moving the
  marker helpers to a neutral module, e.g. `maestro/gate_approvals.py`, is
  acceptable if it avoids a `scope_gate → gates` import edge).
- The `sha` is the worktree HEAD, matching the `(ex_post, sha)` key the resume
  check (§5.2) and the approval store use.

The **full** escape list is emitted via the orchestrator's structured logger
(always available, independent of whether `GateKeeper`/steward is configured),
never dropped. It is not conditional on the `gate_verdicts.jsonl` stream, which
only exists under `GateKeeper`.

### 5.2 Evaluation order (avoid redundant git work)

Check the approval **before** computing the diff:

```
approvals = await db.list_gate_approvals(workstream_id)  # per-workstream set
sha = worktree HEAD
if (phase="ex_post", sha) in approvals:   # operator already waived this sha
    skip scope-gate  ->  proceed
else:
    paths = await changed_paths_since(base, HEAD, worktree)
    escapes = find_escapes(paths, scope)
    if escapes: block (FAILED -> NEEDS_REVIEW, marker-bearing reason)
```

This avoids a wasted `git diff` on every H-6 resume. `list_gate_approvals` is
already scoped per workstream (§6), so `(phase, sha)` is checked *within that
namespace* — never globally.

## 6. Override & recovery

Reuse the existing gate-approval + H-6 machinery — no new tables, no
reason-specific approvals in this PR:

- `maestro workstream-approve <ws>` records an approval in `gate_approvals`
  **only when the stored `error_message` carries the approval marker** (§5.1);
  `parse_approval_marker` supplies the `(phase, sha)` written. This is why the
  scope-gate block reason must embed the marker — otherwise approval is a plain
  requeue that records nothing.
  **DB key is `(workstream_id, phase, sha)`** (`UNIQUE` constraint); the gate
  receives a per-workstream set via `list_gate_approvals(workstream_id)` and
  checks `(phase, sha)` *inside that per-workstream namespace*. An approval for
  workstream A therefore never waives workstream B, even at the same HEAD sha.
- A single approval clears the ex-post edge for its workstream **regardless of
  cause** — a deterministic scope escape and/or a steward tier. Within the
  per-workstream set, approvals key on `(phase, sha)`, not on the reason.
- Re-queue → `_try_resume_ex_post` (H-6): if the worktree HEAD still equals the
  approved sha → `RUNNING` → `_handle_success` re-runs → the scope-gate sees the
  approval (§5.2) and skips → steward gate (if any) sees it too → `MERGING` →
  merge → `DONE`.
- **Requirement:** the scope-gate MUST honor `(phase="ex_post", sha)` within the
  per-workstream approvals set and skip; otherwise the resume loops forever.

## 7. CLI — `maestro check-scope <workstream-id>`

**Raw containment check** — it reports the containment *fact*, not the gate's
*effective policy decision*. For operator inspection and CI.

```
maestro check-scope <workstream-id> --db maestro.db
```

- Resolves the workstream's `scope` (DB) and worktree, computes
  `changed_paths_since` + `find_escapes`, prints escaping paths.
- **Approval does not change the exit code.** The name `check-scope` and the CI
  use-case mean "did this stay in scope?", not "would the gate let it merge?".
  If an `(ex_post, sha)` approval exists for this workstream, print an
  informational `note: approved (ex_post, <sha>)` line — but escapes still exit
  `1`. An `--effective` mode (approval → exit `0`) is a possible later addition,
  not this PR.

**Exit codes:**

- `0` — clean, or scope empty / skipped.
- `1` — escapes found (paths printed).
- `2` — invalid input: unknown workstream, unreadable DB, missing worktree, or
  bad base/HEAD state.

Aligned with `maestro workstreams` / `maestro workstream-approve` CLI
conventions. `--json` output is **deferred** (not this PR).

## 8. Config

**None.** Always-on, no toggle. Scope lives in the workstream spec, so its
enforcement is a deterministic safety invariant, not an option. Fewer surfaces.

## 9. Testing

**Pure core (`find_escapes`)** — table of cases:

- exact match; `dir/**` contents; `*` does not cross `/`; nested globs; escape
  in a parent dir; empty scope → `[]`; deleted-path string (not on FS) still
  matched.

**Rename cases** (delete+add via `--no-renames --name-only`):

- inside-scope file renamed outside → escape includes the dest; source clean.
- outside file renamed inside → escape includes the source.
- outside → outside by a scoped workstream → both endpoints escape.
- inside → inside → clean.

**Source helper (`changed_paths_since`)** — on a temp repo:

- added / deleted / renamed file, orchestrator-managed filter.
- **branch-point isolation:** `base` advanced by another workstream's
  out-of-scope commit → this workstream's diff (in scope) stays clean, no false
  escape (Finding 1 regression).
- **`--no-renames` under hostile config:** temp repo with
  `git config diff.renames true` → the helper still returns both rename
  endpoints as delete+add (Finding 2 regression).

**Integration:**

- escape → `FAILED → NEEDS_REVIEW` + worktree intact; `stats.failed`
  incremented.
- **marker round-trip (blocker regression):** on escape, `error_message`
  contains a well-formed `gates:approval-required phase=ex_post sha=<sha>`
  marker; `maestro workstream-approve` then records `{("ex_post", sha)}` in
  `gate_approvals`; the resume skips the scope-gate and reaches `DONE`.
- approve → resume → `DONE` (H-6), no redundant diff on resume (§5.2).
- always-on when `config.gates is None`.
- **per-workstream approval isolation:** an `(ex_post, sha)` approval for
  workstream A does not waive workstream B at the same HEAD sha (Finding 3
  regression).
- `GateKeeper.evaluate_ex_post` still passes after delegating to
  `changed_paths_since` (branch-point + `--no-renames` shift, §3.2).

**Regression:** a clean workstream (all changes in scope) passes unchanged.

**CLI:** exit `0` / `1` / `2` paths.

## 10. Out of scope

- #7b interfaces ledger (verified public-interface publication → dependent
  briefs).
- Remote/Docker changed-path source (Phase 1+ execution `collect`).
- `maestro check-scope --json`.
- Uncommitted-change enforcement (only committed changes merge, so the
  branch-point committed diff is the complete and correct surface).
