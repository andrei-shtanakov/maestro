# Base merge before DONE Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the feature→base merge before the `DONE` transition and gate `DONE` on merge success, so a crash mid-merge is recoverable and a merge conflict routes to `NEEDS_REVIEW` instead of a silent `DONE`.

**Architecture:** Harden `Orchestrator._merge_into_base` to verify the base-branch invariant, abort a failed merge, and raise a typed error; then restructure `_handle_success` to run the merge while the workstream is at `PR_CREATED` and transition to `DONE` only on success (failure → `FAILED`→`NEEDS_REVIEW`, no workspace cleanup).

**Tech Stack:** Python 3.12+, uv, asyncio, pytest (anyio), pyrefly, ruff. Spec: `docs/superpowers/specs/2026-07-07-base-merge-before-done-design.md`.

## Global Constraints

- `uv` only; `uv run pytest` / `uv run pyrefly check` / `uv run ruff format .` + `uv run ruff check .`; line length 88; async tests `@pytest.mark.anyio`; run pytest in the FOREGROUND.
- The base-branch verify (`git rev-parse --abbrev-ref HEAD` != `base_branch` → raise `GitError`, NO merge) MUST run before the merge — it is the load-bearing guard against a silent wrong-branch merge. A detached HEAD (`"HEAD"`) is a violation.
- On a failed merge: `git merge --abort` (best-effort, `check=False`) to leave the base repo clean, THEN raise — `MergeConflictError` if `"conflict" in stderr.lower()`, else `GitError`. `MergeConflictError` and `BranchNotFoundError` subclass `GitError`, so `_handle_success` catches `GitError`.
- `_merge_into_base` keeps its `span("task.execute", …)` + `child_env()` obs wrapping and raw-subprocess mechanics. Do NOT `checkout base`. Do NOT reuse `GitManager.merge_branch`.
- In `_handle_success`: merge runs while status is `PR_CREATED` (both auto_pr paths converge there — auto_pr=False passes `MERGING`→`PR_CREATED` first). Success → `PR_CREATED`→`DONE`, `stats.completed += 1`, cleanup workspace. Failure (`GitError`) → `PR_CREATED`→`FAILED` (with `error_message`) → `FAILED`→`NEEDS_REVIEW`, `stats.failed += 1`, `return` WITHOUT cleanup. Do NOT use `_handle_failure`.
- Branch: `feat/base-merge-before-done` (exists, spec committed). Full suite green at every commit.

---

### Task 1: Harden `_merge_into_base` (verify + abort + raise)

**Files:**
- Modify: `maestro/orchestrator.py` (add `from maestro.git import GitError, MergeConflictError`; rewrite `_merge_into_base` body)
- Test: `tests/test_orchestrator.py` (new `TestMergeIntoBase` class)

**Interfaces:**
- Consumes: existing `self._config.repo_path`, `self._config.base_branch`, `self._logger`; `maestro.git.GitError`, `maestro.git.MergeConflictError`.
- Produces: `_merge_into_base(self, feature_branch: str) -> None` now raises `GitError`/`MergeConflictError` on a bad branch or failed merge (was: log-only).

- [ ] **Step 1: Write the failing tests (real temp git repo)**

Add to `tests/test_orchestrator.py`. Reuse the `git_repo` fixture from
`tests/conftest.py` (a temp repo with an initial commit). Helper + tests:

```python
class TestMergeIntoBase:
    def _run(self, repo: Path, *args: str) -> str:
        import subprocess

        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    def _orch(self, repo: Path, base: str, mock_workspace_mgr, mock_decomposer,
              mock_pr_manager) -> Orchestrator:
        cfg = OrchestratorConfig(
            project="p", repo_url="https://github.com/t/r",
            repo_path=str(repo), workspace_base="/tmp/ws",
            base_branch=base, workstreams=[],
        )
        return Orchestrator(
            db=MagicMock(), workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer, config=cfg, pr_manager=mock_pr_manager,
        )

    def test_merge_success(self, git_repo, mock_workspace_mgr, mock_decomposer,
                           mock_pr_manager) -> None:
        base = self._run(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        # feature branch with a non-conflicting new file
        self._run(git_repo, "checkout", "-b", "feature/x")
        (git_repo / "new.txt").write_text("hi\n")
        self._run(git_repo, "add", ".")
        self._run(git_repo, "commit", "-m", "feat")
        self._run(git_repo, "checkout", base)
        orch = self._orch(git_repo, base, mock_workspace_mgr, mock_decomposer,
                          mock_pr_manager)
        orch._merge_into_base("feature/x")  # no raise
        assert (git_repo / "new.txt").exists()  # landed on base

    def test_merge_conflict_raises_and_aborts(
        self, git_repo, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ) -> None:
        from maestro.git import MergeConflictError

        base = self._run(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        # feature edits README
        self._run(git_repo, "checkout", "-b", "feature/c")
        (git_repo / "README.md").write_text("feature side\n")
        self._run(git_repo, "add", ".")
        self._run(git_repo, "commit", "-m", "feat edit")
        # base edits README differently -> conflict
        self._run(git_repo, "checkout", base)
        (git_repo / "README.md").write_text("base side\n")
        self._run(git_repo, "add", ".")
        self._run(git_repo, "commit", "-m", "base edit")
        orch = self._orch(git_repo, base, mock_workspace_mgr, mock_decomposer,
                          mock_pr_manager)
        with pytest.raises(MergeConflictError):
            orch._merge_into_base("feature/c")
        # abort ran -> repo not mid-merge (no MERGE_HEAD)
        assert not (git_repo / ".git" / "MERGE_HEAD").exists()

    def test_wrong_branch_raises_without_merging(
        self, git_repo, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ) -> None:
        from maestro.git import GitError

        base = self._run(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        self._run(git_repo, "checkout", "-b", "feature/w")
        (git_repo / "w.txt").write_text("w\n")
        self._run(git_repo, "add", ".")
        self._run(git_repo, "commit", "-m", "w")
        # stay on feature/w (NOT base); base_branch=base in config
        orch = self._orch(git_repo, base, mock_workspace_mgr, mock_decomposer,
                          mock_pr_manager)
        with pytest.raises(GitError):
            orch._merge_into_base("feature/w")

    def test_detached_head_raises(
        self, git_repo, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ) -> None:
        from maestro.git import GitError

        base = self._run(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        sha = self._run(git_repo, "rev-parse", "HEAD")
        self._run(git_repo, "checkout", sha)  # detached HEAD
        orch = self._orch(git_repo, base, mock_workspace_mgr, mock_decomposer,
                          mock_pr_manager)
        with pytest.raises(GitError):
            orch._merge_into_base("feature/whatever")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py::TestMergeIntoBase -q`
Expected: FAIL — current `_merge_into_base` never raises (conflict/wrong-branch return normally).

- [ ] **Step 3: Add the import**

At the top of `maestro/orchestrator.py`, with the other `from maestro.…` imports:

```python
from maestro.git import GitError, MergeConflictError
```

- [ ] **Step 4: Rewrite `_merge_into_base`**

Replace the body of `_merge_into_base` (currently at maestro/orchestrator.py:599):

```python
    def _merge_into_base(self, feature_branch: str) -> None:
        """Merge feature branch into base branch in the main repo.

        Prevents accumulation of unmerged branches that diverge and cause
        conflicts. Each workstream is merged immediately after completion so
        the next workstream sees all prior work.

        Verifies the main repo is on ``base_branch`` before merging (the
        Mode-2 worktree topology keeps it there); a wrong or detached branch
        raises rather than silently merging into the wrong place. On a merge
        failure the partial merge is aborted and the error raised so the
        caller can route the workstream to review instead of DONE.

        Raises:
            GitError: If the repo is not on ``base_branch``, or the merge
                fails for a non-conflict reason.
            MergeConflictError: If the merge has conflicts.
        """
        repo = Path(self._config.repo_path).expanduser()
        base = self._config.base_branch
        merge_env = {**os.environ, **child_env()}

        with span("task.execute", task_id=feature_branch):
            head = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo,
                env=merge_env,
                capture_output=True,
                text=True,
                check=False,
            )
            current_branch = head.stdout.strip()
            if head.returncode != 0 or current_branch != base:
                msg = (
                    f"Refusing to merge '{feature_branch}': main repo is on "
                    f"'{current_branch or '(unknown)'}', not base '{base}'. "
                    "The main repo must be checked out on the base branch."
                )
                raise GitError(msg)

            result = subprocess.run(
                ["git", "merge", feature_branch, "--no-edit"],
                cwd=repo,
                env=merge_env,
                capture_output=True,
                text=True,
                check=False,
            )

        if result.returncode == 0:
            self._logger.info("Merged '%s' into '%s'", feature_branch, base)
            return

        # Abort the partial/conflicted merge so the base repo is left clean,
        # then raise so the caller routes the workstream to review, not DONE.
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=repo,
            env=merge_env,
            capture_output=True,
            text=True,
            check=False,
        )
        stderr = result.stderr.strip()
        self._logger.warning(
            "Failed to merge '%s' into '%s': %s", feature_branch, base, stderr
        )
        if "conflict" in stderr.lower():
            msg = (
                f"Merge conflicts merging '{feature_branch}' into "
                f"'{base}':\n{stderr}"
            )
            raise MergeConflictError(msg)
        msg = f"Failed to merge '{feature_branch}' into '{base}':\n{stderr}"
        raise GitError(msg)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_orchestrator.py::TestMergeIntoBase -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add maestro/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): harden _merge_into_base — verify base branch, abort + raise on failure

Verifies the main repo is on base_branch before merging (wrong/detached
branch -> GitError, no merge), and on a failed merge aborts the partial
merge then raises MergeConflictError/GitError instead of swallowing to a
warning. Enables gating DONE on merge success."
```

---

### Task 2: Restructure `_handle_success` — merge before DONE, gate on success

**Files:**
- Modify: `maestro/orchestrator.py` (`_handle_success` DONE block, currently maestro/orchestrator.py:752-785)
- Test: `tests/test_orchestrator.py` (new `TestHandleSuccessMergeGating` class)

**Interfaces:**
- Consumes: `_merge_into_base` (Task 1, now raises `GitError`), `self._db.update_workstream_status`, `self._stats`, `self._workspace_mgr.cleanup_workspace`.
- Produces: `_handle_success` now transitions to `DONE` only after a successful base merge; a merge `GitError` → `FAILED`→`NEEDS_REVIEW` (no cleanup).

- [ ] **Step 1: Write the failing tests (real in-memory DB; monkeypatch `_merge_into_base`)**

Add to `tests/test_orchestrator.py`. Seed the workstream at `RUNNING` (the
method's entry state) and let the full `_handle_success` flow run; `auto_pr`
selects the path. Import `Database` from `maestro.database`.

```python
class TestHandleSuccessMergeGating:
    async def _orch_db(self, tmp_path, auto_pr, mock_workspace_mgr,
                       mock_decomposer, mock_pr_manager):
        from maestro.database import Database

        db = Database(tmp_path / "o.db")
        await db.connect()
        cfg = OrchestratorConfig(
            project="p", repo_url="https://github.com/t/r",
            repo_path="/tmp/r", workspace_base="/tmp/ws",
            auto_pr=auto_pr, workstreams=[],
        )
        orch = Orchestrator(
            db=db, workspace_mgr=mock_workspace_mgr, decomposer=mock_decomposer,
            config=cfg, pr_manager=mock_pr_manager,
        )
        return orch, db

    def _seed(self, zid):
        return Workstream(
            id=zid, title=zid, description="d", scope=["s"],
            branch=f"feature/{zid}", status=WorkstreamStatus.RUNNING,
        )

    @pytest.mark.anyio
    async def test_success_marks_done_and_cleans_up(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
    ) -> None:
        orch, db = await self._orch_db(tmp_path, True, mock_workspace_mgr,
                                       mock_decomposer, mock_pr_manager)
        try:
            await db.create_workstream(self._seed("a"))
            orch._merge_into_base = MagicMock()  # success = no raise
            await orch._handle_success("a", MagicMock())
            assert (await db.get_workstream("a")).status == WorkstreamStatus.DONE
            assert orch._stats.completed == 1
            mock_workspace_mgr.cleanup_workspace.assert_called_once_with("a")
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_merge_conflict_routes_to_needs_review_no_cleanup(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
    ) -> None:
        from maestro.git import MergeConflictError

        orch, db = await self._orch_db(tmp_path, True, mock_workspace_mgr,
                                       mock_decomposer, mock_pr_manager)
        try:
            await db.create_workstream(self._seed("b"))

            def boom(_branch: str) -> None:
                raise MergeConflictError("CONFLICT in README.md")

            orch._merge_into_base = boom
            await orch._handle_success("b", MagicMock())
            w = await db.get_workstream("b")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert w.error_message is not None
            assert orch._stats.failed == 1
            assert orch._stats.completed == 0
            mock_workspace_mgr.cleanup_workspace.assert_not_called()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_auto_pr_false_success_reaches_done(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager,
    ) -> None:
        orch, db = await self._orch_db(tmp_path, False, mock_workspace_mgr,
                                       mock_decomposer, mock_pr_manager)
        try:
            await db.create_workstream(self._seed("c"))
            orch._merge_into_base = MagicMock()
            await orch._handle_success("c", MagicMock())
            assert (await db.get_workstream("c")).status == WorkstreamStatus.DONE
            mock_pr_manager.push_and_create_pr.assert_not_called()
        finally:
            await db.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py::TestHandleSuccessMergeGating -q`
Expected: FAIL — the conflict test fails (current code marks DONE before the
merge and swallows the conflict; also the raising monkeypatched merge now
propagates because the current merge runs after DONE via run_in_executor).

- [ ] **Step 3: Restructure the DONE block**

Replace the "Mark as DONE" block (maestro/orchestrator.py:752-785) — everything
from `# Mark as DONE` through the `cleanup_workspace` call:

```python
        # Ensure the workstream is at PR_CREATED (both auto_pr paths converge
        # here); auto_pr=False creates no PR, so pass MERGING -> PR_CREATED.
        current = await self._db.get_workstream(workstream_id)
        if current.status == WorkstreamStatus.MERGING:
            await self._db.update_workstream_status(
                workstream_id,
                WorkstreamStatus.PR_CREATED,
            )

        # Merge the feature branch into base BEFORE marking DONE, so DONE is
        # gated on a successful merge. A conflict/failure routes to
        # NEEDS_REVIEW (a human resolves it; re-running run --all cannot), and
        # a crash mid-merge leaves the workstream pre-DONE for startup recovery.
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                self._merge_into_base,
                workstream.branch,
            )
        except GitError as e:
            self._logger.warning(
                "Base merge failed for '%s'; routing to NEEDS_REVIEW: %s",
                workstream_id,
                e,
            )
            await self._db.update_workstream_status(
                workstream_id,
                WorkstreamStatus.FAILED,
                expected_status=WorkstreamStatus.PR_CREATED,
                error_message=f"Base merge failed: {e}",
            )
            await self._db.update_workstream_status(
                workstream_id,
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=WorkstreamStatus.FAILED,
            )
            self._stats.failed += 1
            # Leave the workspace intact so a human can resolve the conflict.
            return

        # Merge succeeded -> DONE.
        await self._db.update_workstream_status(
            workstream_id,
            WorkstreamStatus.DONE,
            expected_status=WorkstreamStatus.PR_CREATED,
        )
        self._stats.completed += 1

        # Cleanup workspace
        self._workspace_mgr.cleanup_workspace(workstream_id)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_orchestrator.py::TestHandleSuccessMergeGating -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Full suite + gates**

Run: `uv run pytest -q`
Then: `uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS (existing `_handle_success` tests still green — a successful
workstream still ends `DONE` with the workspace cleaned); pyrefly clean; ruff
clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): merge into base before DONE, gate DONE on merge success

DONE is now written only after a successful base merge; a merge conflict
routes to FAILED -> NEEDS_REVIEW (workspace left intact for the human), and
a crash mid-merge leaves the workstream pre-DONE so startup recovery can
re-run it. Closes the 'DONE but not merged' gap (C4 follow-up #2)."
```

---

### Task 3: Docs, TODO tick, final gates, PR

**Files:**
- Modify: `CLAUDE.md` (orchestrator flow note), `TODO.md` (tick C4 follow-up #2)

- [ ] **Step 1: CLAUDE.md**

In the "Orchestrator Flow" section, update step 5 to reflect merge-before-DONE.
Find the line describing success/auto-merge (currently: "On success: auto-merge
feature branch into base, create PR (if auto_pr), cleanup worktree") and replace
with:

```markdown
5. On success: create PR (if auto_pr), then merge feature branch into base BEFORE marking DONE (DONE is gated on the merge — a conflict routes the workstream to NEEDS_REVIEW with the worktree left intact; a crash mid-merge is recoverable via startup recovery), then cleanup worktree
```

- [ ] **Step 2: TODO.md**

Find the C4 follow-up #2 entry (added in the startup-recovery branch:
"(b) Move `_merge_into_base` BEFORE the DONE transition …") and tick it `[x]`
with `(closed by feat/base-merge-before-done)`.

- [ ] **Step 3: Final gates**

```bash
uv run pytest -q
uv run pyrefly check
uv run ruff format . && uv run ruff check .
```

Expected: full suite green; pyrefly 0; ruff clean.

- [ ] **Step 4: Commit docs**

```bash
git add CLAUDE.md TODO.md
git commit -m "docs: base-merge-before-DONE shipped — C4 follow-up #2 ticked"
```

- [ ] **Step 5: Push and open the PR** (controller may defer until after the final review)

```bash
git push -u origin feat/base-merge-before-done
gh pr create --title "feat(orchestrator): merge into base before DONE (C4 follow-up #2)" --body "$(cat <<'EOF'
## Summary
- `_merge_into_base` ran AFTER the `DONE` transition, so a crash mid-merge left a workstream showing `DONE` with its feature branch never merged into base — and startup recovery skips terminal `DONE`, so the work silently never landed. This moves the merge BEFORE `DONE` and gates `DONE` on merge success
- Hardened `_merge_into_base`: verifies the main repo is on `base_branch` before merging (wrong/detached branch → `GitError`, no merge — closes a silent wrong-branch-merge risk), and on a failed merge aborts the partial merge (base repo left clean) then raises `MergeConflictError`/`GitError` instead of swallowing to a warning
- A merge conflict now routes to `FAILED`→`NEEDS_REVIEW` (a human resolves it; re-running `run --all` cannot) with the worktree left intact for context; `stats.failed` is incremented so the CLI exit code/summary reflect it
- A crash BETWEEN the merge commit and the `DONE` write leaves the workstream at `PR_CREATED` → startup recovery (PR #48) → `READY` → re-run → `git merge` reports "Already up to date" (idempotent) → `DONE`. Fully auto-recovered
- Kept the obs `span`/`child_env` wrapping; did not `checkout base` or reuse `GitManager.merge_branch` (read-only verify closes the invariant risk without an active-checkout failure surface)

Spec: docs/superpowers/specs/2026-07-07-base-merge-before-done-design.md

## Test plan
- [ ] Full suite green; pyrefly + ruff clean
- [ ] `_merge_into_base`: success merges; conflict → `MergeConflictError` + repo left clean (no MERGE_HEAD); wrong branch → `GitError` no merge; detached HEAD → `GitError`
- [ ] `_handle_success`: success → `DONE` + cleanup; conflict → `NEEDS_REVIEW` + no cleanup + `stats.failed`; auto_pr=False → `DONE`
- [ ] Existing orchestrator tests still green

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Spec coverage: reorder + gate DONE → Task 2 Step 3; `_merge_into_base` abort+raise → Task 1 Step 4; base-branch verify (incl. detached HEAD) → Task 1 Steps 1/4 (tests `test_wrong_branch_*`, `test_detached_head_*`); failure → NEEDS_REVIEW + no cleanup + stats.failed → Task 2 Step 3 + test; idempotent re-merge → Task 2 (success test simulates "Already up to date" via a non-raising monkeypatch); auto_pr=False pass-through → Task 2 test; docs → Task 3.
- Type consistency: `_merge_into_base(self, feature_branch: str) -> None` raising `GitError`/`MergeConflictError`; `_handle_success` catches `GitError` (base class). Consistent across Tasks 1/2.
- Real-git tests for the merge mechanics (Task 1) use the `git_repo` conftest fixture; real-DB tests for the state machine (Task 2) assert persisted status. Both avoid mock-call brittleness.
- Three tasks: Task 1 (git helper, real-git tests), Task 2 (state-machine restructure, real-DB tests), Task 3 (docs+PR) — each an independent reviewer gate.
