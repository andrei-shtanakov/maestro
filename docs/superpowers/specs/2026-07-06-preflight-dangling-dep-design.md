# I1: preflight `dangling-dep` check — design

**Date:** 2026-07-06
**Status:** approved
**Context:** Bug report `docs/bugs/2026-07-05-validate-dangling-depends-on.md`
(from the steward's emitter-contract-check). `maestro validate` /
`preflight.validate_project` does not flag a workstream whose `depends_on`
references a nonexistent workstream id.

## Root cause (confirmed)

`validate_project` builds the dependency graph and detects cycles via
`dag.find_cycle`, but `find_cycle` **intentionally ignores dependencies whose
ids are not graph keys** (dag.py:33 docstring). So a dangling `depends_on`
edge is silently swallowed by the cycle check, and no other check covers it.

## Triage — why this is defense-in-depth, not a user-facing bug

The bug report's stated symptom does NOT reproduce through the normal path.
`OrchestratorConfig.validate_workstream_dependencies_exist` (a Pydantic
`model_validator`) rejects a dangling `depends_on` at config LOAD time, before
`validate_project` ever runs — `validate_project`'s own docstring says it
receives an "already schema-validated config". Verified on the live CLI:
`maestro validate --no-fs` on a YAML with a dangling dep exits 1 with
"Workstream 'b' has unknown dependencies: {'does-not-exist'}".

The report's repro bypasses the validator by MUTATING an already-validated
config in memory (`w.depends_on = list(...) + ["does-not-exist"]` — field
assignment does not re-run validation). So the real residual gap is: a
programmatic caller that builds/mutates an `OrchestratorConfig` after load
(the steward's emitter `decomposition → project.yaml` contract check, WS-002
REQ-203) has no preflight safety net for dependency-edge integrity. This
change closes that gap. Severity: low (defense-in-depth for the programmatic
path; not user-facing).

## Change

New static check `_check_dangling_deps(workstreams: list[WorkstreamConfig]) ->
list[ValidationIssue]` in `maestro/preflight.py`, wired into
`validate_project` alongside the other static checks so it also runs under
`check_fs=False` (`--no-fs`):

```python
def _check_dangling_deps(
    workstreams: list[WorkstreamConfig],
) -> list[ValidationIssue]:
    known = {w.id for w in workstreams}
    issues: list[ValidationIssue] = []
    for w in workstreams:
        unknown = [d for d in w.depends_on if d not in known]
        if unknown:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="dangling-dep",
                    workstream_ids=[w.id],
                    message=(
                        f"Workstream '{w.id}' depends on unknown "
                        f"workstream(s): {', '.join(sorted(unknown))}. "
                        "Check the depends_on ids."
                    ),
                )
            )
    return issues
```

Wiring in `validate_project`, in the `if config.workstreams:` block:

```python
    if config.workstreams:
        issues.extend(_check_dangling_deps(config.workstreams))
        issues.extend(_check_cycles(config.workstreams))
        issues.extend(_check_scope_empty(config.workstreams))
        issues.extend(_check_overlap_static(config.workstreams, overlap_pairs))
```

Ordering: dangling-deps first, so an unknown-id error is reported before the
cycle check (which ignores that edge anyway) — the more actionable message
leads.

### Design decisions

- **Severity `error`**, consistent with `dag-cycle`: a dangling edge means
  the DAG references a nonexistent node; scheduling would break on it. Matches
  the bug report's Acceptance.
- **One issue per workstream**, listing all of that workstream's unknown ids
  (sorted for deterministic output), rather than one issue per bad edge — less
  noise, groups by the workstream the author edits.
- **Not folded into `find_cycle`.** Extending the graph utility to also report
  unknown deps would overload it with validation concerns and break its
  documented "ignore non-key deps" contract that other callers rely on. A
  dedicated preflight check is the right layer.

## Testing

- Unit `_check_dangling_deps`: one unknown id → single `error` `dangling-dep`
  with the right `workstream_ids` and the unknown id named in the message;
  multiple unknown ids on one workstream → one issue listing all (sorted);
  all deps valid → empty; several workstreams each with a dangling edge → one
  issue each.
- Integration via `validate_project(config, check_fs=False)`: the bug
  report's exact repro — load a valid config, then mutate
  `w.depends_on += ["does-not-exist"]` — yields `report.ok is False` with a
  `dangling-dep` error. A cyclic-AND-dangling config still reports both
  (dangling-dep and dag-cycle), proving the checks are independent.
- Regression: existing preflight tests stay green; a fully valid project is
  still `ok=True` with no `dangling-dep` issue.

## Documentation

- CLAUDE.md preflight blurb enumerates the codes — add `dangling-dep`.
- `docs/bugs/2026-07-05-validate-dangling-depends-on.md` — mark resolved with
  the commit hash.

## Out of scope

- Any runtime check in the scheduler/orchestrator — preflight is the correct
  layer; the DAG builder already tolerates missing nodes by design.
- Changing the Pydantic `validate_workstream_dependencies_exist` validator
  (it already covers the load path correctly).
- Auto-repairing dangling edges.
