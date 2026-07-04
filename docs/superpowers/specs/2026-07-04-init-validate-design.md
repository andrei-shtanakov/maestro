# Design: `maestro init` + `maestro validate` (Mode-2 project.yaml authoring)

**Date:** 2026-07-04
**Status:** approved design, pre-implementation
**Origin:** `docs/idea-workstream-framework.md` step 1 (scaffold + validate — the
narrow vertical slice with the highest ROI; SDK/DSL/import stay gated on proven pain).

## Problem

Mode-2 `project.yaml` is authored by hand as bare YAML. There is no scaffold
command, the only template is `examples/project.yaml`, and the de-facto schema is
the pydantic pair `OrchestratorConfig` / `WorkstreamConfig` (`maestro/models.py`).
Authoring workstreams relies on keeping the schema in one's head and copy-pasting
the example. Several classes of mistakes surface only mid-run: dependency cycles
between workstreams deadlock the orchestrator, overlapping scopes cause merge
conflicts, and path/glob typos silently produce workstreams that own nothing.

## Scope

- **In scope:** Mode-2 `project.yaml` only (`OrchestratorConfig`).
- **Out of scope:** Mode-1 `tasks.yaml` (its validation largely exists via
  pydantic + `TaskDAG`), SDK, DSL, import from external formats, changes to
  `orchestrator.py`'s programmatic API.

## Decisions (from brainstorming)

1. Coverage: Mode-2 `project.yaml` only.
2. `init` UX: non-interactive smart template — git-derived autofill + schema
   defaults + comments; flags for overrides.
3. `validate` depth: static checks + filesystem checks (repo exists, globs match).
   No network/git-remote checks in this slice.
4. Integration: `maestro orchestrate` runs the same validator before starting
   (errors block, warnings print); `validate` is the standalone preflight.
5. Architecture: new `maestro/preflight.py` module with a `ValidationReport`
   (approach A) — not pydantic model validators (no warning semantics, FS checks
   don't belong in pure models), not a plugin-rule linter framework (YAGNI).

## Component 1: `maestro/preflight.py`

Pure validation logic, no I/O beyond reading the target repo's file tree.

```python
class ValidationIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str                      # stable machine-readable code, kebab-case
    workstream_ids: list[str]      # affected workstreams ([] for project-level)
    message: str                   # human-readable, includes a fix hint

class ValidationReport(BaseModel):
    issues: list[ValidationIssue]
    @property errors -> list[ValidationIssue]      # severity == "error"
    @property warnings -> list[ValidationIssue]    # severity == "warning"
    @property ok -> bool                           # no errors

def validate_project(
    config: OrchestratorConfig, *, check_fs: bool = True
) -> ValidationReport: ...
```

### Checks

| Code | Severity | What | Source |
|---|---|---|---|
| (pydantic) | error | schema shape, duplicate IDs, unknown deps, self-dep, ID format | already enforced at load time by `OrchestratorConfig`; the CLI renders `ValidationError` as error issues — not re-implemented in preflight |
| `dag-cycle` | error | cycle in workstream `depends_on` | shared pure cycle detector (see "Shared cycle detection" below); report includes the cycle path |
| `scope-overlap` | warning | scope overlap between two workstreams | two-tier (see "Scope overlap accuracy" below): static glob heuristic always; exact file-set intersection when `check_fs=True` |
| `scope-empty` | warning | workstream has empty `scope` | new — no conflict protection for that workstream |
| `repo-missing` | error | `repo_path` does not exist (after `~` expansion) | FS, only when `check_fs=True` |
| `repo-not-git` | error | `repo_path` exists but has no `.git` | FS |
| `scope-no-match` | warning | a scope glob matches zero files under `repo_path` | FS, `pathlib.Path.glob`. Honest framing: this cannot distinguish a typo from a legitimate glob for not-yet-created files — it flags "probably-empty scope", nothing stronger. Low-signal by construction; kept because it is free once the FS pass globs anyway |
| `scope-invalid-pattern` | warning | a scope glob is unsafe to expand: empty/whitespace, absolute (leading `/`), contains a `..` path segment, or otherwise rejected by `pathlib.Path.glob` (e.g. `NotImplementedError`/`ValueError`) | FS. Detected before globbing so it never raises and never lets a pattern escape `repo_path` via `..`; the pattern contributes no files to that workstream's scope and does not also emit `scope-no-match` |

Empty `workstreams` (auto-decompose mode): DAG/scope checks are skipped, FS
repo checks still run.

### Shared cycle detection (refactor, MVP-blocking)

`DAG._detect_cycles` + `_find_cycle_path` (`maestro/dag.py:109-181`) are
extracted into a module-level pure function in `dag.py`:

```python
def find_cycle(deps: dict[str, set[str]]) -> list[str] | None:
    """Kahn's algorithm + DFS path recovery over an id->dependencies map."""
```

`DAG` calls it with `{id: node.dependencies}` (raising `CycleError` as today);
preflight calls it with `{ws.id: set(ws.depends_on)}`. One algorithm, one test
suite, no second Kahn implementation to drift. Existing `DAG` behavior and
tests are unchanged (pure extraction).

### Scope overlap accuracy (two-tier)

`_patterns_overlap` (`maestro/decomposer.py:427`) is a heuristic (mutual
`fnmatch` + shared top-level directory) with known false-negative classes:
unnormalized paths (`./src/**` vs `src/**`), patterns whose first segments
differ but expand to the same files. Because `scope-overlap` is the headline
check, a silent false negative is the worst failure mode. Therefore:

- **Static tier** (always, and the only tier under `--no-fs`): the existing
  heuristic via `Decomposer.validate_non_overlap`. Its limits are documented
  in the `validate --no-fs` help text.
- **Exact tier** (`check_fs=True`): expand every workstream's globs against
  the real repo tree (the FS pass globs anyway for `scope-no-match`), then
  intersect the resulting file sets pairwise. Exact for files that exist;
  overlapping globs over not-yet-created files remain covered only by the
  heuristic. Issues from both tiers share the `scope-overlap` code and are
  de-duplicated per workstream pair.

## Component 2: `maestro validate` (CLI)

```
maestro validate <project.yaml> [--strict] [--no-fs]
```

Thin wrapper in `cli.py`:

1. `load_orchestrator_config(path)`; a pydantic `ValidationError` is rendered as
   error issues (one per pydantic error, code `schema`) and exits 1.
2. `validate_project(config, check_fs=not no_fs)`.
3. Rich output: errors in red, warnings in yellow, one line per issue
   (`[code] ws-a, ws-b: message`), summary line `N errors, M warnings`.
4. Exit codes: `0` — no errors (warnings allowed); `1` — errors present;
   `--strict` — warnings also cause exit 1 (CI mode). `--no-fs` skips filesystem
   checks (deterministic run without the real repo).

## Component 3: `maestro init` (CLI) + `maestro/scaffold.py`

```
maestro init [PATH=project.yaml] [--force] [--project NAME]
```

Non-interactive. `maestro/scaffold.py` holds the logic:

- **Template:** a commented string template in the style of
  `examples/project.yaml`, including one example workstream and commented-out
  optional sections (`notifications`). Values are substituted into the template
  so comments survive (direct pydantic serialization would lose them).
- **Autofill** via a single private helper (`_git_query`) inside `scaffold.py`
  using sync `subprocess.run`. `GitManager` is async and worktree-oriented —
  wrong tool here — but this makes scaffold the second git access path in the
  repo, so the helper is private and must not be imported elsewhere; if a third
  caller ever needs sync git queries, promote it deliberately:
  - `project` — current directory basename, overridable via `--project`;
  - `repo_path` — absolute cwd;
  - `repo_url` — `git remote get-url origin`; if absent, a placeholder plus a
    `# TODO: fill in` comment;
  - `base_branch` — origin default branch (`git symbolic-ref
    refs/remotes/origin/HEAD`), fallback to current branch, then `main`;
  - `workspace_base` — `/tmp/maestro-ws/<project>`.
- **Self-check:** before writing, the generated YAML is parsed with
  `load_orchestrator_config` — `init` can never emit a config the loader
  rejects; this also catches template drift when the schema evolves.
- Existing target file without `--force` → error message, exit 1.
- Running `init` outside a git repo is allowed (placeholders + TODO comments);
  the generated file will then fail `validate`'s FS checks until edited —
  that is the intended guidance loop.

## Component 4: `orchestrate` integration

In `cli.py::orchestrate_command`, inside the existing async path where the
config is loaded (`_run_orchestrator`, cli.py:898), immediately after
`load_orchestrator_config` and before any DB/orchestrator work:

- run `validate_project(config)`, print the report with the same renderer;
- errors → abort with exit 1 and hint `run 'maestro validate <path>' for details`;
- warnings → print and continue.

`orchestrator.py` is untouched; programmatic users are unaffected.

## Error handling

- Preflight never raises for config-content problems — everything is an issue in
  the report. Only genuinely unexpected failures (e.g. `OSError` walking the
  repo) propagate as exceptions.
- CLI keeps the existing pattern: human-readable message + `typer.Exit(1)`.

## Testing

- `tests/test_dag.py` additions: `find_cycle` pure-function cases (no cycle,
  2-node, 3-node, disconnected components); existing `DAG` cycle tests keep
  passing unchanged — they prove the extraction is behavior-preserving.
- `tests/test_preflight.py`: 2-node and 3-node cycles (self-dep already covered
  by pydantic), overlap warning from the static tier, overlap caught **only**
  by the exact FS tier (heuristic false negative, e.g. `./src/**` vs
  `src/**`), de-duplication when both tiers fire, empty scope, FS cases via
  `tmp_path` (missing repo, non-git dir, glob with zero matches, glob with
  matches), empty workstreams list, `check_fs=False` skips FS issues.
- `tests/test_scaffold.py`: generation inside a tmp git repo with remote
  (round-trip through `load_orchestrator_config`), repo without remote
  (placeholder path), non-git cwd — **explicitly asserting the placeholder
  output still passes `load_orchestrator_config`** (placeholders must satisfy
  the pydantic schema, e.g. `validate_repo_path`; only `validate`'s FS checks
  may fail on it), existing file / `--force`.
- CLI tests via `typer.testing.CliRunner` following existing test style:
  `validate` exit codes (ok / errors / `--strict` with warnings), `init`
  happy path and refusal to overwrite.
- Regression: existing `orchestrate` tests keep passing; a valid config runs
  exactly as before (warnings do not block).

## Implementation order (TDD throughout)

1. `dag.py`: extract `find_cycle` pure function (behavior-preserving refactor,
   existing tests green before and after)
2. `preflight.py` (report + checks, both overlap tiers)
3. `maestro validate` CLI command
4. `orchestrate` integration
5. `scaffold.py` + `maestro init` CLI command
