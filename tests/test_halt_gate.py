"""DarkFactory halt check (opt-in, contracts/halt-admission/v1 vendored)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from maestro import halt_gate
from maestro.run_bootstrap import bootstrap_run


CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "halt-admission" / "v1"
DATA = json.loads((CONTRACT / "vectors.json").read_text())


@pytest.mark.parametrize("v", DATA["vectors"], ids=[v["name"] for v in DATA["vectors"]])
def test_every_vector(v: dict) -> None:
    assert halt_gate.decide(v["listing"], v["detail"])[:2] == (v["admit"], v["code"])


def test_the_vendored_copy_is_the_pinned_one() -> None:
    lines = (CONTRACT / "PINNED.txt").read_text().splitlines()
    pinned = {
        ln.split("  ")[1]: ln.split("  ")[0] for ln in lines if len(ln.split("  ")) == 2
    }
    for name in ("README.md", "vectors.json"):
        digest = hashlib.sha256((CONTRACT / name).read_bytes()).hexdigest()
        assert pinned[name] == digest, name


@pytest.mark.parametrize(
    ("listing", "detail", "expected"),
    [
        ("", None, (True, "admit_missing")),
        ('[7,"darkfactory-halt"]', '{"enforcement":"active"}', (False, "refuse_on")),
        ('[7,"darkfactory-halt"]', '{"enforcement":"disabled"}', (True, "admit_off")),
        (None, None, (False, "refuse_unknown")),
    ],
)
def test_check(monkeypatch, listing, detail, expected) -> None:
    def fake(*args: str) -> str | None:
        return listing if "--jq" in args else detail

    monkeypatch.setattr(halt_gate, "_gh", fake)
    assert halt_gate.check("acme", "app")[:2] == expected


def test_inert_without_the_flag(monkeypatch) -> None:
    monkeypatch.delenv("DARKFACTORY_HALT_CHECK", raising=False)
    monkeypatch.setattr(halt_gate, "check", lambda *_: pytest.fail("must not check"))
    halt_gate.refuse_if_halted("github.com", "acme", "app", local=False)


@pytest.mark.parametrize(
    ("host", "local"), [("github.com", True), ("gitlab.com", False)]
)
def test_local_or_non_github_admits_without_asking(monkeypatch, host, local) -> None:
    monkeypatch.setenv("DARKFACTORY_HALT_CHECK", "1")
    monkeypatch.setattr(halt_gate, "check", lambda *_: pytest.fail("must not check"))
    halt_gate.refuse_if_halted(host, "acme", "app", local=local)


@pytest.mark.parametrize(
    ("decision", "code"),
    [((False, "refuse_on", "on"), 6), ((False, "refuse_unknown", "x"), 2)],
)
def test_refusal_carries_the_exit_code(monkeypatch, decision, code) -> None:
    monkeypatch.setenv("DARKFACTORY_HALT_CHECK", "1")
    monkeypatch.setattr(halt_gate, "check", lambda *_: decision)
    with pytest.raises(halt_gate.HaltRefused) as exc:
        halt_gate.refuse_if_halted("github.com", "acme", "app", local=False)
    assert exc.value.exit_code == code


class _Config:
    repo_url = "https://github.com/acme/app"


async def test_an_admitted_fresh_run_starts(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DARKFACTORY_HALT_CHECK", "1")
    monkeypatch.setattr(halt_gate, "check", lambda *_: (True, "admit_off", "off"))
    result = await bootstrap_run(
        _Config(), resume=False, run_id_override=None, home=tmp_path
    )
    assert result.fresh is True


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_the_flag_accepts_obvious_spellings(raw: str) -> None:
    assert halt_gate.enabled({"DARKFACTORY_HALT_CHECK": raw}) is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off"])
def test_the_flag_off_spellings(raw: str) -> None:
    assert halt_gate.enabled({"DARKFACTORY_HALT_CHECK": raw}) is False


def test_tests_run_without_the_flag() -> None:
    """conftest strips it, whatever the agent environment exports (#248)."""
    import os

    assert "DARKFACTORY_HALT_CHECK" not in os.environ


@pytest.mark.parametrize(
    "v",
    DATA["origin_vectors"],
    ids=[v["origin"] or "<empty>" for v in DATA["origin_vectors"]],
)
def test_origin_vectors_through_maestros_identity(v: dict, tmp_path) -> None:
    """maestro decides github vs not by RepoKey.host; the contract's origin
    vectors pin that this host comes out right for each origin (#248)."""
    from maestro.repo_identity import IdentityError, identity_from_config

    class _Cfg:
        repo_url = v["origin"]

    try:
        key = identity_from_config(_Cfg())
    except IdentityError:
        assert v["github"] is False
        return
    github = (not key.local) and key.host.lower() == "github.com"
    assert github is v["github"], key


@pytest.mark.parametrize(
    "v",
    DATA["origin_vectors"],
    ids=[v["origin"] or "<empty>" for v in DATA["origin_vectors"]],
)
def test_every_origin_vector_directly(v: dict) -> None:
    assert halt_gate.is_github_origin(v["origin"]) is v["github"]


def test_a_non_github_origin_is_admitted_not_unread(monkeypatch) -> None:
    """Review #248: the contract pins gitlab as admit_not_github."""
    monkeypatch.setenv("DARKFACTORY_HALT_CHECK", "1")
    monkeypatch.setattr(halt_gate, "check", lambda *_: pytest.fail("must not ask"))

    class _Gitlab:
        repo_url = "https://gitlab.com/o/r.git"

    halt_gate.refuse_for_config(_Gitlab())


def test_the_reads_pin_the_github_host(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake(*args: str) -> str | None:
        calls.append(args)
        return (
            '[7,"darkfactory-halt"]' if "--jq" in args else '{"enforcement":"active"}'
        )

    monkeypatch.setattr(halt_gate, "_gh", fake)
    halt_gate.check("o", "r")
    assert all(c[:2] == ("--hostname", "github.com") for c in calls)


def test_refuse_for_config_unresolvable_is_unread(monkeypatch) -> None:
    monkeypatch.setenv("DARKFACTORY_HALT_CHECK", "1")

    class _Bad:
        # GitHub, but no owner/name to read: unread — not "not GitHub".
        repo_url = "https://github.com/"
        repo = None

    with pytest.raises(halt_gate.HaltRefused) as exc:
        halt_gate.refuse_for_config(_Bad())
    assert exc.value.exit_code == 2  # unread, never admitted
    assert "identity unresolved" in str(exc.value)


# --- review round 3 on #248: the halt is asked at every ENTRY ------------
#
# Deciding "is this a new run" from flags let three paths slip past the
# check (`--db`, `--db` before the run-branch gate, `--resume` over an empty
# `--db`). The mechanism now asks at the entry of every command that starts
# or resumes agent work, before the PID lock, the resolver and the database.
# This table is every combination; each must refuse with 6 touching nothing.


def _tasks_yaml(base: Path) -> Path:
    path = base / "tasks.yaml"
    path.write_text(
        "project: demo\n"
        f"repo: {base / 'checkout'}\n"
        "tasks:\n  - id: t1\n    title: T\n    prompt: p\n    agent_type: announce\n",
        encoding="utf-8",
    )
    return path


def _project_yaml(base: Path) -> Path:
    path = base / "project.yaml"
    path.write_text(
        "project: demo\n"
        "repo_url: https://github.com/acme/app\n"
        f"repo_path: {base / 'checkout'}\n"
        f"workspace_base: {base / 'ws'}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def halted_cli(monkeypatch):
    from maestro import cli

    def halted(config: object) -> None:
        raise halt_gate.HaltRefused("DarkFactory halt (refuse_on): on", unread=False)

    def never(*a, **k):
        raise AssertionError("touched before the halt was asked")

    monkeypatch.setattr(cli, "refuse_for_config", halted)
    monkeypatch.setattr(cli, "_acquire_pid_lock", never)
    monkeypatch.setattr(cli, "bootstrap_run", never)
    monkeypatch.setattr(cli, "create_database", never)
    return cli


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--resume"],
        ["--db", "{db}"],
        ["--db", "{db}", "--resume"],
        ["--db", "{missing}", "--resume"],
    ],
    ids=["fresh", "resume", "db", "db-resume", "db-missing-resume"],
)
@pytest.mark.parametrize("command", ["run", "orchestrate"])
def test_every_start_combination_asks_the_halt_first(
    halted_cli, tmp_path, command, extra
) -> None:
    from typer.testing import CliRunner

    import subprocess

    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "remote",
            "add",
            "origin",
            "https://github.com/acme/app",
        ],
        check=True,
    )
    cfg = _tasks_yaml(tmp_path) if command == "run" else _project_yaml(tmp_path)
    db = tmp_path / "x.db"
    db.write_bytes(b"")
    args = [a.format(db=db, missing=tmp_path / "nope.db") for a in extra]
    result = CliRunner().invoke(halted_cli.app, [command, str(cfg), *args])
    assert result.exit_code == 6, result.output
    assert "touched before" not in repr(result.exception)


@pytest.mark.parametrize("unread", [False, True])
def test_a_halted_service_tick_skips_and_an_unread_one_fails(
    halted_cli, monkeypatch, tmp_path, unread
) -> None:
    """Review r3: a deliberate halt is a handled skip (0), not a red tick;
    an unread halt is an infrastructure failure (1)."""
    from typer.testing import CliRunner

    def gate(config: object) -> None:
        raise halt_gate.HaltRefused("DarkFactory halt (x): y", unread=unread)

    monkeypatch.setattr(halted_cli, "refuse_for_config", gate)
    result = CliRunner().invoke(
        halted_cli.app,
        ["service", "run", str(_project_yaml(tmp_path)), "--stage", "orchestrate"],
    )
    assert result.exit_code == (1 if unread else 0), result.output
    if not unread:
        assert "halted -> skip" in result.output


def test_bootstrap_run_no_longer_carries_its_own_check() -> None:
    """One mechanism: entries ask; bootstrap_run does not ask a second time."""
    import inspect

    from maestro import run_bootstrap

    assert "halt" not in inspect.getsource(run_bootstrap.bootstrap_run)


def test_an_unparseable_repo_url_is_unread_not_admitted(monkeypatch) -> None:
    monkeypatch.setenv("DARKFACTORY_HALT_CHECK", "1")

    class _Garbled:
        repo_url = "not a url"

    with pytest.raises(halt_gate.HaltRefused) as exc:
        halt_gate.refuse_for_config(_Garbled())
    assert exc.value.exit_code == 2
