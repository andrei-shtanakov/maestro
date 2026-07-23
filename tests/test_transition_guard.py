"""AST guards for the transition-hooks dispatcher wiring (spec §9).

These are cheap tripwires, not a full control-flow analysis:
- `scheduler.py` may only call `db.update_task_status(...)` from inside
  `_transition` — every other real transition must route through it, so a
  new call site can't silently reintroduce an un-dispatched status write.
- `database.py` (the DB layer) must stay effect-free: it may never import
  `maestro.transitions`.
- Every atomic DB helper that can commit a FAILED->READY transition outside
  `update_task_status` (`reset_for_retry_atomic`, and
  `abandon_pending_outcome_and_release` discovered during Task 6 review —
  the spec/brief only named the first one) must be called from a function
  that also references `_dispatch_committed_transition`, so a future atomic
  helper — or a new call site of an existing one — can't silently skip the
  effect table.
"""

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent

# Atomic paths that commit a status transition outside `update_task_status`
# (so they're exempt from the `_transition`-only guard above) but whose
# success path must still dispatch the effect table via
# `_dispatch_committed_transition`. See spec §4.3/§9.
_ATOMIC_TRANSITION_HELPERS = frozenset(
    {"reset_for_retry_atomic", "abandon_pending_outcome_and_release"}
)


def _calls_inside(
    tree: ast.AST, method: str, allowed_enclosing: set[str]
) -> list[tuple[str, int]]:
    """Return (enclosing_function, lineno) for calls to `method` outside
    `allowed_enclosing` function bodies (module-level calls included)."""
    bad: list[tuple[str, int]] = []

    class V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(
            self, node: ast.FunctionDef | ast.AsyncFunctionDef
        ) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == method
                and not (self.stack and self.stack[-1] in allowed_enclosing)
            ):
                enclosing = self.stack[-1] if self.stack else "<module>"
                bad.append((enclosing, node.lineno))
            self.generic_visit(node)

    V().visit(tree)
    return bad


def _unwrap_await(value: ast.expr) -> ast.expr:
    """`x = await self._db.foo(...)` wraps the Call in an `ast.Await`."""
    return value.value if isinstance(value, ast.Await) else value


def _mentions_name(expr: ast.expr, name: str) -> bool:
    """Whether `name` appears as a bare identifier anywhere in `expr`
    (covers both `if ok:` and `if ok and other:`)."""
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(expr))


def _count_calls(tree: ast.AST, method: str) -> int:
    """Total attribute calls to `method` anywhere in the tree — a sanity
    floor so a renamed/removed helper can't make the gap check vacuous."""
    return sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == method
    )


def _calls_method(nodes: list[ast.stmt], method: str) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == method
        for stmt in nodes
        for n in ast.walk(stmt)
    )


def _atomic_helper_gaps(tree: ast.AST, method: str) -> list[tuple[str, int]]:
    """Find `var = await self._db.<method>(...)` assignments NOT followed,
    in the same statement list, by an `if <expr referencing var>:` block
    that itself calls `_dispatch_committed_transition` — the actual
    guard-then-dispatch pattern every current call site follows. This is
    call-site-level (not just "somewhere in the same function"), so two
    atomic helpers sharing one enclosing function (as
    `reset_for_retry_atomic` and `abandon_pending_outcome_and_release` do
    in `_outcome_reattempt_pass`) can't hide a missing dispatch on one of
    them behind the other's. Returns (enclosing_function, lineno) per gap.
    """
    gaps: list[tuple[str, int]] = []

    def body_fields(node: ast.AST) -> list[list[ast.stmt]]:
        out: list[list[ast.stmt]] = []
        for name in ("body", "orelse", "finalbody"):
            val = getattr(node, name, None)
            if isinstance(val, list) and val and isinstance(val[0], ast.stmt):
                out.append(val)
        for handler in getattr(node, "handlers", []):
            out.append(handler.body)
        return out

    def scan(node: ast.AST, enclosing: str) -> None:
        next_enclosing = enclosing
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            next_enclosing = node.name
        for stmts in body_fields(node):
            for i, stmt in enumerate(stmts):
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                ):
                    call = _unwrap_await(stmt.value)
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == method
                    ):
                        var = stmt.targets[0].id
                        guard = stmts[i + 1] if i + 1 < len(stmts) else None
                        dispatched = (
                            isinstance(guard, ast.If)
                            and _mentions_name(guard.test, var)
                            and _calls_method(
                                guard.body, "_dispatch_committed_transition"
                            )
                        )
                        if not dispatched:
                            gaps.append((next_enclosing, stmt.lineno))
                scan(stmt, next_enclosing)

    scan(tree, "<module>")
    return gaps


def test_scheduler_update_task_status_only_in_transition() -> None:
    """Every `update_task_status(...)` call in scheduler.py must live inside
    `_transition` — the one place that also dispatches the effect table."""
    tree = ast.parse((_REPO_ROOT / "maestro" / "scheduler.py").read_text())
    assert _calls_inside(tree, "update_task_status", {"_transition"}) == []


def test_transitions_not_imported_by_database() -> None:
    """The DB layer stays effect-free: it never imports the dispatcher."""
    src = (_REPO_ROOT / "maestro" / "database.py").read_text()
    assert "import transitions" not in src
    assert "from maestro.transitions" not in src


def test_atomic_transition_helpers_dispatch_effects() -> None:
    """Every atomic FAILED->READY DB helper's success path must dispatch
    the effect table: each `var = await self._db.<helper>(...)` call must
    be immediately followed by an `if var:`-style guard that itself calls
    `_dispatch_committed_transition` — checked per call site, not merely
    "somewhere in the enclosing function" (two helpers, e.g.
    `reset_for_retry_atomic` and `abandon_pending_outcome_and_release`,
    can share one enclosing function without sharing a guard). The real
    guarantee is the behavioral tests that TASK_RETRYING fires on success
    and not on failure for each site; this is the cheap tripwire (spec §9).
    """
    tree = ast.parse((_REPO_ROOT / "maestro" / "scheduler.py").read_text())
    for helper in _ATOMIC_TRANSITION_HELPERS:
        assert _count_calls(tree, helper) > 0, (
            f"expected at least one call site for {helper}"
        )
        gaps = _atomic_helper_gaps(tree, helper)
        assert gaps == [], (
            f"{helper} call(s) not immediately guarded by an "
            f"`if <result>:` block calling _dispatch_committed_transition: "
            f"{gaps}"
        )
