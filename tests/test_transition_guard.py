"""AST guards for the transition-hooks dispatcher wiring (spec §9).

These are cheap tripwires, not a full control-flow analysis:
- `scheduler.py` may only call `db.update_task_status(...)` from inside
  `_transition` — every other real transition must route through it, so a
  new call site can't silently reintroduce an un-dispatched status write.
- `database.py` (the DB layer) must stay effect-free: it may never import
  `maestro.transitions`.
"""

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent


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
