from pathlib import Path

import pytest

from maestro.execution.ssh_collect import (
    CollectConflict,
    apply_collect,
    capture_baseline,
    plan_collect,
)


EXCL = [".git", ".maestro", "*.log"]
FORBIDDEN = [".git", ".maestro"]


def _w(p: Path, name: str, body: str) -> None:
    (p / name).parent.mkdir(parents=True, exist_ok=True)
    (p / name).write_text(body)


def test_modified_new_and_deleted_detected(tmp_path):
    wt, st = tmp_path / "wt", tmp_path / "st"
    wt.mkdir()
    st.mkdir()
    _w(wt, "a.py", "orig")
    _w(wt, "gone.py", "x")
    base = capture_baseline(wt, excludes=EXCL)
    _w(st, "a.py", "changed")
    _w(st, "new.py", "n")  # gone.py absent -> deleted
    plan = plan_collect(wt, st, base, forbidden=FORBIDDEN)
    assert set(plan.modified) == {"a.py", "new.py"}
    assert plan.deleted == ["gone.py"]
    apply_collect(wt, st, plan, journal_dir=tmp_path / "j")
    assert (wt / "a.py").read_text() == "changed"
    assert (wt / "new.py").read_text() == "n"
    assert not (wt / "gone.py").exists()


def test_local_divergence_on_remote_touched_path_conflicts(tmp_path):
    wt, st = tmp_path / "wt", tmp_path / "st"
    wt.mkdir()
    st.mkdir()
    _w(wt, "a.py", "orig")
    base = capture_baseline(wt, excludes=EXCL)
    _w(wt, "a.py", "LOCALLY CHANGED DURING RUN")  # parallel local mutation
    _w(st, "a.py", "remote changed")
    with pytest.raises(CollectConflict):
        plan_collect(wt, st, base, forbidden=FORBIDDEN)


def test_preflight_conflict_leaves_worktree_untouched(tmp_path):
    wt, st = tmp_path / "wt", tmp_path / "st"
    wt.mkdir()
    st.mkdir()
    _w(wt, "a.py", "orig")
    _w(st, "a.py", "orig")
    outside = tmp_path / "etc_passwd_like"
    outside.write_text("secret")
    (st / "evil.py").symlink_to(outside)  # symlink escaping the worktree
    base = capture_baseline(wt, excludes=EXCL)
    before = (wt / "a.py").read_text()
    with pytest.raises(CollectConflict):
        plan_collect(wt, st, base, forbidden=FORBIDDEN)
    assert (wt / "a.py").read_text() == before


def test_file_dir_structural_conflict_detected_in_preflight(tmp_path):
    """Baseline has a file `a`; remote wants to nest `a/b` under it."""
    wt, st = tmp_path / "wt", tmp_path / "st"
    wt.mkdir()
    st.mkdir()
    _w(wt, "a", "a is a file")
    base = capture_baseline(wt, excludes=EXCL)
    _w(st, "a/b", "nested")
    before = (wt / "a").read_text()
    with pytest.raises(CollectConflict):
        plan_collect(wt, st, base, forbidden=FORBIDDEN)
    assert (wt / "a").read_text() == before
    assert (wt / "a").is_file()  # zero mutation: still a file, not a dir


def test_dir_file_structural_conflict_detected_in_preflight(tmp_path):
    """Baseline has a dir `a/` (with `a/b`); remote replaces it with file `a`."""
    wt, st = tmp_path / "wt", tmp_path / "st"
    wt.mkdir()
    st.mkdir()
    _w(wt, "a/b", "nested orig")
    base = capture_baseline(wt, excludes=EXCL)
    _w(st, "a", "a is now a file")
    with pytest.raises(CollectConflict):
        plan_collect(wt, st, base, forbidden=FORBIDDEN)
    assert (wt / "a").is_dir()  # zero mutation: still a directory
    assert (wt / "a" / "b").read_text() == "nested orig"


def test_rollback_restores_on_apply_error(tmp_path, monkeypatch):
    wt, st = tmp_path / "wt", tmp_path / "st"
    wt.mkdir()
    st.mkdir()
    _w(wt, "a.py", "orig")
    _w(wt, "b.py", "orig-b")
    base = capture_baseline(wt, excludes=EXCL)
    _w(st, "a.py", "A2")
    _w(st, "b.py", "B2")
    plan = plan_collect(wt, st, base, forbidden=FORBIDDEN)
    # Force a failure after the first file is applied.
    import maestro.execution.ssh_collect as mod

    calls = {"n": 0}
    real = mod._atomic_copy

    def boom(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(src, dst)

    monkeypatch.setattr(mod, "_atomic_copy", boom)
    with pytest.raises(OSError):
        apply_collect(wt, st, plan, journal_dir=tmp_path / "j")
    assert (wt / "a.py").read_text() == "orig"  # rolled back
    assert (wt / "b.py").read_text() == "orig-b"
