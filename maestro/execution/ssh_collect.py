"""Baseline capture + two-phase transactional collect.

Phase 1 (plan_collect): pure preflight — zero worktree mutation. Detects the
remote's changes vs a pre-run baseline, rejects conflicts (parallel local
mutation on a remote-touched path), forbidden paths and symlink/traversal
escapes. Phase 2 (apply_collect): back up affected paths into a journal, apply
atomically per file, and on any error restore the whole journal.
"""

import fnmatch
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


class CollectConflict(Exception):
    """Preflight rejected the collect; no worktree changes were made."""


@dataclass
class CollectPlan:
    modified: list[str]
    deleted: list[str]


def _excluded(rel: str, excludes: list[str]) -> bool:
    parts = rel.split("/")
    for pat in excludes:
        if fnmatch.fnmatch(rel, pat) or any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    return False


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk(root: Path, excludes: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            abs_p = Path(dirpath) / name
            rel = abs_p.relative_to(root).as_posix()
            if _excluded(rel, excludes):
                continue
            if abs_p.is_symlink():
                continue  # symlinks handled/validated in plan_collect
            out[rel] = _sha(abs_p)
    return out


def capture_baseline(worktree: Path, *, excludes: list[str]) -> dict[str, str]:
    """{relpath: sha256} for all non-excluded regular files in the worktree."""
    return _walk(worktree, excludes)


def _rel_escapes(worktree: Path, rel: str) -> bool:
    target = (worktree / rel).resolve()
    root = worktree.resolve()
    return root != target and root not in target.parents


def _check_staging_sandboxed(worktree: Path, staging: Path) -> None:
    """Reject a staging area that a one-hop `..` write already escaped.

    A malicious/misbehaving remote sync can materialize a file via a
    relative name like ``"../escape.py"``, which physically lands as a
    *sibling* of ``staging`` rather than inside it — invisible to any walk
    rooted at ``staging``. The only place left to detect that is
    ``staging``'s own parent: it must contain nothing but the staging
    directory itself (and, when co-located, the worktree).
    """
    parent = staging.resolve().parent
    allowed = {staging.resolve()}
    worktree_r = worktree.resolve()
    if worktree_r.parent == parent:
        allowed.add(worktree_r)
    for entry in parent.iterdir():
        if entry.resolve() not in allowed:
            raise CollectConflict(f"unexpected path outside staging root: {entry}")


def plan_collect(
    worktree: Path,
    staging: Path,
    baseline: dict[str, str],
    *,
    forbidden: list[str],
) -> CollectPlan:
    """Preflight; raises CollectConflict on any violation. No side effects."""
    _check_staging_sandboxed(worktree, staging)
    remote = _walk(staging, forbidden)
    # Symlink / traversal guard over the raw staging tree.
    for dirpath, _dirs, files in os.walk(staging):
        for name in files:
            abs_p = Path(dirpath) / name
            rel = abs_p.relative_to(staging).as_posix()
            if abs_p.is_symlink():
                raise CollectConflict(f"symlink in staging rejected: {rel}")
            if ".." in rel.split("/") or _rel_escapes(worktree, rel):
                raise CollectConflict(f"path escapes worktree: {rel}")

    modified = sorted(r for r, sha in remote.items() if baseline.get(r) != sha)
    deleted = sorted(r for r in baseline if r not in remote)

    for rel in [*modified, *deleted]:
        if _excluded(rel, forbidden):
            raise CollectConflict(f"forbidden path in change set: {rel}")
        current_p = worktree / rel
        current = _sha(current_p) if current_p.is_file() else None
        if current != baseline.get(rel):
            raise CollectConflict(
                f"local worktree diverged from baseline on remote-touched path: {rel}"
            )
    return CollectPlan(modified=modified, deleted=deleted)


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.maestro-tmp"
    shutil.copyfile(src, tmp)
    tmp.replace(dst)


def apply_collect(
    worktree: Path,
    staging: Path,
    plan: CollectPlan,
    *,
    journal_dir: Path,
) -> None:
    """Apply with a rollback journal; restore everything on any error."""
    journal_dir.mkdir(parents=True, exist_ok=True)
    backed: list[tuple[str, Path | None]] = []  # (rel, backup_path or None if absent)
    try:
        for rel in [*plan.modified, *plan.deleted]:
            target = worktree / rel
            if target.is_file():
                bak = journal_dir / rel
                bak.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, bak)
                backed.append((rel, bak))
            else:
                backed.append((rel, None))
        for rel in plan.modified:
            _atomic_copy(staging / rel, worktree / rel)
        for rel in plan.deleted:
            (worktree / rel).unlink(missing_ok=True)
    except Exception:
        for rel, bak in backed:
            target = worktree / rel
            if bak is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(bak, target)
        raise
    shutil.rmtree(journal_dir, ignore_errors=True)
