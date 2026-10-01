"""DarkFactory halt check (opt-in, contracts/halt-admission/v1 vendored)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from maestro import halt_gate
from maestro.repo_identity import RepoKey
from maestro.run_bootstrap import bootstrap_run
from maestro.run_registry import resolve_runs


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


async def test_a_halted_fresh_run_is_refused_before_it_exists(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("DARKFACTORY_HALT_CHECK", "1")
    monkeypatch.setattr(halt_gate, "check", lambda *_: (False, "refuse_on", "on"))
    with pytest.raises(halt_gate.HaltRefused):
        await bootstrap_run(
            _Config(), resume=False, run_id_override=None, home=tmp_path
        )
    key = RepoKey(host="github.com", owner="acme", repo="app")
    assert await resolve_runs(key, home=tmp_path, lock_root=tmp_path) == []


async def test_an_admitted_fresh_run_starts(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DARKFACTORY_HALT_CHECK", "1")
    monkeypatch.setattr(halt_gate, "check", lambda *_: (True, "admit_off", "off"))
    result = await bootstrap_run(
        _Config(), resume=False, run_id_override=None, home=tmp_path
    )
    assert result.fresh is True
