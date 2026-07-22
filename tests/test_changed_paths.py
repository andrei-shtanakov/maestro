import asyncio
from pathlib import Path

from maestro.changed_paths import changed_paths_since


async def _git(repo: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return out.decode()


async def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    await _git(repo, "init", "-b", "main")
    await _git(repo, "config", "user.email", "t@t.t")
    await _git(repo, "config", "user.name", "t")
    (repo / "base.py").write_text("x\n")
    await _git(repo, "add", ".")
    await _git(repo, "commit", "-m", "base")
    return repo


async def test_reports_added_and_deleted_paths(tmp_path):
    repo = await _init_repo(tmp_path)
    await _git(repo, "checkout", "-b", "feature")
    (repo / "src").mkdir()
    (repo / "src" / "new.py").write_text("y\n")
    (repo / "base.py").unlink()
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "work")
    paths = await changed_paths_since("main", "HEAD", repo)
    assert sorted(paths) == ["base.py", "src/new.py"]


async def test_branch_point_isolation_ignores_advanced_base(tmp_path):
    # base advances with an out-of-scope commit AFTER feature branched;
    # changed_paths_since must still report only feature's own change.
    repo = await _init_repo(tmp_path)
    await _git(repo, "checkout", "-b", "feature")
    (repo / "mine.py").write_text("f\n")
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "feature work")
    await _git(repo, "checkout", "main")
    (repo / "sibling.py").write_text("s\n")
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "sibling merged into base")
    paths = await changed_paths_since("main", "feature", repo)
    assert paths == ["mine.py"]  # NOT ['mine.py','sibling.py'] and no false escape


async def test_no_renames_under_hostile_config(tmp_path):
    repo = await _init_repo(tmp_path)
    await _git(repo, "config", "diff.renames", "true")
    await _git(repo, "checkout", "-b", "feature")
    (repo / "base.py").rename(repo / "renamed.py")
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "rename")
    paths = await changed_paths_since("main", "HEAD", repo)
    assert sorted(paths) == ["base.py", "renamed.py"]  # delete + add, not a rename


async def test_orchestrator_managed_filtered(tmp_path):
    from maestro.models import SPEC_PREFIX

    repo = await _init_repo(tmp_path)
    await _git(repo, "checkout", "-b", "feature")
    (repo / "spec").mkdir()
    (repo / "spec" / f"{SPEC_PREFIX}tasks.md").write_text("t\n")
    (repo / "keep.py").write_text("k\n")
    await _git(repo, "add", "-A")
    await _git(repo, "commit", "-m", "work + harness")
    paths = await changed_paths_since("main", "HEAD", repo)
    assert paths == ["keep.py"]
