"""DarkFactory halt check before a NEW run (opt-in, halt D2b).

maestro is used outside DarkFactory, so the check runs only when
``DARKFACTORY_HALT_CHECK=1`` is set — dispatcher sets it when it spawns
maestro, and agent environments can set it. The rule is github-checker's
``contracts/halt-admission/v1`` (vendored with a pin; ``decide`` is tested
against its vectors):

- a local or non-github.com repository identity → admit;
- ruleset ``darkfactory-halt`` absent or ``disabled`` → admit;
- ``active``, a duplicate name, another enforcement, anything unreadable →
  refuse.

Reads with the ambient ``gh`` profile. Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any


ENV_FLAG = "DARKFACTORY_HALT_CHECK"
HALT_RULESET = "darkfactory-halt"
#: Exit codes, as devtools merge-pr.sh: 6 halted, 2 halt unread (retry fits).
EXIT_HALTED = 6
EXIT_UNREAD = 2

Decision = tuple[bool, str, str]


class HaltRefused(Exception):
    """A new run refused by the DarkFactory halt."""

    def __init__(self, message: str, *, unread: bool) -> None:
        super().__init__(message)
        self.unread = unread

    @property
    def exit_code(self) -> int:
        """6 when the halt is in force, 2 when it could not be read."""
        return EXIT_UNREAD if self.unread else EXIT_HALTED


def enabled(env: dict[str, str] | None = None) -> bool:
    """Whether the opt-in flag is set."""
    return (env if env is not None else os.environ).get(ENV_FLAG) == "1"


def decide(
    listing: list[dict[str, Any]] | None, detail: dict[str, Any] | None
) -> Decision:
    """(admit, code, reason) — the halt-admission/v1 table."""
    if listing is None:
        return False, "refuse_unknown", "rulesets could not be listed"
    named = [r for r in listing if r.get("name") == HALT_RULESET]
    if not named:
        return True, "admit_missing", "no darkfactory-halt ruleset (not armed)"
    if len(named) > 1:
        return False, "refuse_duplicate", f"{len(named)} rulesets named {HALT_RULESET}"
    if detail is None:
        return False, "refuse_unknown", "the halt ruleset could not be read"
    enforcement = detail.get("enforcement")
    if enforcement == "disabled":
        return True, "admit_off", "halt is off"
    if enforcement == "active":
        return False, "refuse_on", "the DarkFactory halt is ON for this repository"
    return False, "refuse_enforcement", f"halt enforcement {enforcement!r}"


def _gh(*args: str) -> str | None:
    try:
        done = subprocess.run(
            ["gh", "api", *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def check(owner: str, repo: str) -> Decision:
    """Read the halt of github.com/<owner>/<repo> and decide."""
    out = _gh(
        "--paginate",
        f"repos/{owner}/{repo}/rulesets?includes_parents=false",
        "--jq",
        ".[] | [.id, .name] | @json",
    )
    listing: list[dict[str, Any]] | None = None
    if out is not None:
        try:
            pairs = [json.loads(line) for line in out.splitlines() if line.strip()]
            listing = [{"id": rid, "name": name} for rid, name in pairs]
        except (json.JSONDecodeError, TypeError, ValueError):
            listing = None
    detail = None
    if listing is not None:
        named = [r for r in listing if r.get("name") == HALT_RULESET]
        if len(named) == 1:
            body = _gh(f"repos/{owner}/{repo}/rulesets/{named[0]['id']}")
            try:
                parsed = json.loads(body) if body is not None else None
            except json.JSONDecodeError:
                parsed = None
            detail = parsed if isinstance(parsed, dict) else None
    return decide(listing, detail)


def refuse_if_halted(host: str, owner: str, repo: str, *, local: bool) -> None:
    """Raise HaltRefused when a new run must not start; no-op when off."""
    if not enabled():
        return
    if local or host.lower() != "github.com":
        return  # admit_not_github
    admit, code, reason = check(owner, repo)
    if not admit:
        raise HaltRefused(
            f"DarkFactory halt ({code}): {reason}", unread=code == "refuse_unknown"
        )
