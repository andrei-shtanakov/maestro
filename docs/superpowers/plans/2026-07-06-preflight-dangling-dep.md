# I1: preflight `dangling-dep` check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `preflight.validate_project` flags a workstream whose `depends_on` references a nonexistent workstream id (`error`, code `dangling-dep`) — defense-in-depth for programmatic mutate-after-load callers that bypass the Pydantic load validator.

**Architecture:** One new static check `_check_dangling_deps` in `maestro/preflight.py`, wired into `validate_project` before `_check_cycles` (so it runs under `check_fs=False` too). Plus the module docstring correction (its current text becomes false) and docs.

**Tech Stack:** Python 3.12+, uv, pytest, pyrefly, ruff. Spec: `docs/superpowers/specs/2026-07-06-preflight-dangling-dep-design.md`.

## Global Constraints

- `uv` only; `uv run pytest` / `uv run pyrefly check` / `uv run ruff format .` + `uv run ruff check .`; line length 88.
- Severity `error`, code `dangling-dep`; one issue per workstream, listing all its unknown ids SORTED (deterministic CLI output).
- Not folded into `dag.find_cycle` (its documented "ignore non-key deps" contract stays).
- Integration tests MUST bypass the Pydantic load validator by mutating `config.workstreams[i].depends_on` AFTER `make_config` (a config built with a dangling dep won't construct — it would test Pydantic, not preflight). Verified fact: `OrchestratorConfig`'s validator rejects unknown deps at load but ACCEPTS a pure cycle, so the cyclic-and-dangling test can build the a↔b cycle directly via `make_config`, then mutate in the dangling edge.
- The `maestro/preflight.py` module docstring MUST be corrected (it currently claims unknown deps are "NOT re-implemented here").
- Branch: `fix/preflight-dangling-dep` (exists, spec committed). Full suite green at the commit.

---

### Task 1: `_check_dangling_deps` + wiring + docstring + docs

**Files:**
- Modify: `maestro/preflight.py` (module docstring lines 4-6; new `_check_dangling_deps`; wire into `validate_project`)
- Modify: `CLAUDE.md` (preflight blurb)
- Modify: `docs/bugs/2026-07-05-validate-dangling-depends-on.md` (mark resolved)
- Test: `tests/test_preflight.py`

**Interfaces:**
- Consumes: `ValidationIssue(severity, code, workstream_ids, message)`, `WorkstreamConfig` (both existing); test helpers `make_config(workstreams, repo_path=...)` and `ws(id_, scope, depends_on)` in `tests/test_preflight.py`.
- Produces: `_check_dangling_deps(workstreams: list[WorkstreamConfig]) -> list[ValidationIssue]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_preflight.py` (imports `make_config`, `ws`, `validate_project` already present; add `from maestro.preflight import _check_dangling_deps` — or reference it via the module if the file imports `preflight` as a module; match the file's existing import style):

```python
class TestDanglingDeps:
    def test_single_unknown_dep_is_error(self) -> None:
        from maestro.preflight import _check_dangling_deps

        issues = _check_dangling_deps(
            [ws("a", ["src/a/**"], []), ws("b", ["src/b/**"], ["nope"])]
        )
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert issues[0].code == "dangling-dep"
        assert issues[0].workstream_ids == ["b"]
        assert "nope" in issues[0].message

    def test_all_deps_valid_is_empty(self) -> None:
        from maestro.preflight import _check_dangling_deps

        issues = _check_dangling_deps(
            [ws("a", ["src/a/**"], []), ws("b", ["src/b/**"], ["a"])]
        )
        assert issues == []

    def test_each_dangling_workstream_gets_one_issue(self) -> None:
        from maestro.preflight import _check_dangling_deps

        issues = _check_dangling_deps(
            [ws("a", ["src/a/**"], ["x"]), ws("b", ["src/b/**"], ["y"])]
        )
        assert {i.workstream_ids[0] for i in issues} == {"a", "b"}
        assert len(issues) == 2

    def test_multiple_unknown_ids_sorted_in_message(self) -> None:
        from maestro.preflight import _check_dangling_deps

        issues = _check_dangling_deps(
            [ws("a", ["src/a/**"], ["a", "z-missing", "a-missing"])]
        )
        # one issue, unknown ids listed sorted (a-missing before z-missing)
        assert len(issues) == 1
        assert "a-missing, z-missing" in issues[0].message

    def test_integration_mutate_after_load(self) -> None:
        # bypass the Pydantic load validator by mutating post-construction
        config = make_config(
            [ws("a", ["src/a/**"], []), ws("b", ["src/b/**"], ["a"])]
        )
        config.workstreams[1].depends_on.append("does-not-exist")
        report = validate_project(config, check_fs=False)
        assert report.ok is False
        assert any(i.code == "dangling-dep" for i in report.issues)

    def test_integration_cyclic_and_dangling_independent(self) -> None:
        # a<->b cycle constructs at load (validator accepts pure cycles),
        # then mutate in a dangling edge → both codes present, independently
        config = make_config(
            [ws("a", ["src/a/**"], ["b"]), ws("b", ["src/b/**"], ["a"])]
        )
        config.workstreams[0].depends_on.append("ghost")
        report = validate_project(config, check_fs=False)
        codes = {i.code for i in report.issues}
        assert "dangling-dep" in codes
        assert "dag-cycle" in codes

    def test_valid_project_has_no_dangling_dep(self) -> None:
        config = make_config(
            [ws("a", ["src/a/**"], []), ws("b", ["src/b/**"], ["a"])]
        )
        report = validate_project(config, check_fs=False)
        assert all(i.code != "dangling-dep" for i in report.issues)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_preflight.py::TestDanglingDeps -q`
Expected: FAIL — `_check_dangling_deps` does not exist (ImportError), and the integration tests find no `dangling-dep` issue.

- [ ] **Step 3: Implement `_check_dangling_deps` + wiring**

In `maestro/preflight.py`, add the function next to `_check_cycles`:

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

Wire it into `validate_project`, FIRST in the `if config.workstreams:` block:

```python
    if config.workstreams:
        issues.extend(_check_dangling_deps(config.workstreams))
        issues.extend(_check_cycles(config.workstreams))
        issues.extend(_check_scope_empty(config.workstreams))
        issues.extend(_check_overlap_static(config.workstreams, overlap_pairs))
```

- [ ] **Step 4: Correct the module docstring (required)**

`maestro/preflight.py` lines 4-6 currently read:

```
Aggregates errors and warnings into a ValidationReport instead of raising,
so callers can render everything at once. Schema-level validation (duplicate
ids, unknown deps, self-deps) stays in the pydantic models and is NOT
re-implemented here.
```

Replace the second sentence:

```
Aggregates errors and warnings into a ValidationReport instead of raising,
so callers can render everything at once. Schema-level validation (duplicate
ids, unknown deps, self-deps) catches these on config load in the pydantic
models; preflight repeats selected graph-integrity checks (dangling deps,
cycles) as defense-in-depth for configs mutated programmatically after load.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_preflight.py -q`
Expected: PASS (including existing preflight tests).

- [ ] **Step 6: Docs**

`CLAUDE.md` — the preflight blurb (line ~96) lists what it detects. Add
`dangling-dep` to the enumerated checks, e.g. change "cycle detection via
shared dag.find_cycle" to "dangling-dep + cycle detection (shared
dag.find_cycle)".

`docs/bugs/2026-07-05-validate-dangling-depends-on.md` — append a resolution
line under the triage section: "**Resolved** by `fix/preflight-dangling-dep`
(commit `<hash>`): `_check_dangling_deps` in preflight emits an `error`
`dangling-dep` for unknown `depends_on` ids, covering the programmatic
mutate-after-load path." (Fill the hash after committing, or reference the
branch.)

- [ ] **Step 7: Gates + full suite**

```bash
uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .
```

Expected: suite green (~1472 + 7 new); pyrefly clean; ruff clean.

- [ ] **Step 8: Smoke — the real CLI on a mutated config**

```bash
uv run python -c "
from maestro.models import OrchestratorConfig, WorkstreamConfig
from maestro.preflight import validate_project
def ws(i,s,d): return WorkstreamConfig(id=i,title=i,description='d',scope=s,depends_on=d)
c = OrchestratorConfig(project='p', repo_url='git@x:y.git', repo_path='/tmp/x',
    workspace_base='/tmp/w', workstreams=[ws('a',['src/a/**'],[]), ws('b',['src/b/**'],['a'])])
c.workstreams[1].depends_on.append('does-not-exist')
r = validate_project(c, check_fs=False)
print('ok:', r.ok, '| codes:', [i.code for i in r.issues])
"
```

Expected: `ok: False | codes: ['dangling-dep']`.

- [ ] **Step 9: Commit**

```bash
git add maestro/preflight.py tests/test_preflight.py CLAUDE.md docs/bugs/2026-07-05-validate-dangling-depends-on.md
git commit -m "fix(preflight): flag dangling depends_on as an error (I1)

_check_dangling_deps emits an error 'dangling-dep' for depends_on ids
that reference no workstream — defense-in-depth for programmatic
mutate-after-load callers that bypass the Pydantic load validator (the
DAG's find_cycle intentionally ignores non-key deps, so the cycle check
never caught these). Module docstring corrected to match."
```

- [ ] **Step 10: Push and open the PR** (controller may defer until after the final review)

```bash
git push -u origin fix/preflight-dangling-dep
gh pr create --title "fix(preflight): flag dangling depends_on as an error (I1)" --body "$(cat <<'EOF'
## Summary
- `validate_project` now flags a workstream whose `depends_on` references a nonexistent workstream id: `error`, code `dangling-dep`, one issue per workstream listing all its unknown ids (sorted)
- Runs under `--no-fs` too (static check, before the cycle check)
- **Why it's defense-in-depth, not a user-facing bug:** the normal path never reaches preflight with a dangling dep — Pydantic's `validate_workstream_dependencies_exist` rejects it at config load. This check covers programmatic callers that mutate a config after load (the steward's emitter-contract-check), which the `find_cycle`-based cycle check silently ignored (it drops non-key deps by design)
- Module docstring corrected (it claimed unknown deps were "NOT re-implemented here")

Bug: docs/bugs/2026-07-05-validate-dangling-depends-on.md
Spec: docs/superpowers/specs/2026-07-06-preflight-dangling-dep-design.md

## Test plan
- [ ] Full suite green; pyrefly + ruff clean
- [ ] Unit: single/multiple/none unknown; sorted-message determinism; one issue per dangling workstream
- [ ] Integration (mutate-after-load, bypassing Pydantic): dangling → ok=False with dangling-dep; cyclic-AND-dangling → both codes independently
- [ ] Smoke: real validate_project on a mutated config → ok=False, codes=['dangling-dep']

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Spec coverage: `_check_dangling_deps` + wiring → Step 3; docstring correction → Step 4; unit + sort + mutate-after-load + cyclic-and-dangling + regression tests → Step 1; docs (CLAUDE.md, bug doc) → Step 6. All spec sections covered by the single task.
- Verified fact baked in: `OrchestratorConfig` accepts a pure a↔b cycle at load (validator only rejects unknown deps), so the cyclic-and-dangling test builds the cycle directly and only mutates in the dangling edge — no need to construct the cycle via mutation.
- Type consistency: `_check_dangling_deps(workstreams: list[WorkstreamConfig]) -> list[ValidationIssue]` matches the sibling `_check_cycles`/`_check_scope_empty` signatures; `ValidationIssue` field names (`severity`, `code`, `workstream_ids`, `message`) match the model.
- Single-task plan: the change is small and cohesive (one file of logic + its tests + docs); splitting would fragment a reviewer's gate without benefit.
