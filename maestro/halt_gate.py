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
#: Exit codes, as devtools merge-pr.sh: 6 halted, 2 halt unread (retry fits);
#: 1 a configuration that cannot name a repository (retrying will not help).
EXIT_HALTED = 6
EXIT_UNREAD = 2
EXIT_CONFIG = 1

Decision = tuple[bool, str, str]
_GITHUB_HOSTS = ("github.com", "www.github.com")


def origin_host(url: str) -> str | None:
    """The host of an origin URL, or None when none can be read from it."""
    url = url.strip()
    if "://" not in url and ":" in url and not url.startswith("/"):
        head = url.split(":", 1)[0]
        return None if "/" in head else (head.rsplit("@", 1)[-1] or None)
    if "://" in url:
        rest = url.split("://", 1)[1]
        return rest.split("/", 1)[0].rsplit("@", 1)[-1].split(":", 1)[0] or None
    return None


def is_github_origin(url: str) -> bool:
    """Whether an origin URL's host is exactly github.com (admit_not_github;
    the contract's origin_vectors pin it)."""
    host = origin_host(url)
    return host is not None and host.lower() in _GITHUB_HOSTS


class HaltRefused(Exception):
    """A new run refused by the DarkFactory halt."""

    def __init__(
        self, message: str, *, unread: bool, config_error: bool = False
    ) -> None:
        super().__init__(message)
        self.unread = unread or config_error
        self.config_error = config_error

    @property
    def exit_code(self) -> int:
        """6 halt in force; 2 halt unread (retry fits); 1 config cannot name
        a repository (retry will not help — review #248, round 4)."""
        if self.config_error:
            return EXIT_CONFIG
        return EXIT_UNREAD if self.unread else EXIT_HALTED


_TRUE = frozenset({"1", "true", "yes", "on"})


def enabled(env: dict[str, str] | None = None) -> bool:
    """Whether the opt-in flag is set — `1`/`true`/`yes`/`on`, any case: a
    spelling that silently switched the halt off would be the worst
    failure of an opt-in safety check (review #248)."""
    raw = (env if env is not None else os.environ).get(ENV_FLAG, "")
    return raw.strip().lower() in _TRUE


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
        "--hostname",
        "github.com",
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
            body = _gh(
                "--hostname",
                "github.com",
                f"repos/{owner}/{repo}/rulesets/{named[0]['id']}",
            )
            try:
                parsed = json.loads(body) if body is not None else None
            except json.JSONDecodeError:
                parsed = None
            detail = parsed if isinstance(parsed, dict) else None
    return decide(listing, detail)


def refuse_for_config(config: object) -> None:
    """The halt for a run started WITHOUT bootstrap_run (explicit `--db`):
    the identity is resolved here. Unresolvable under the flag is an unread
    halt (exit 2), never an admitted run (review #248)."""
    if not enabled():
        return
    from maestro.repo_identity import IdentityError, identity_from_config

    repo_url = getattr(config, "repo_url", None)
    if isinstance(repo_url, str) and repo_url:
        host = origin_host(repo_url)
        if host is None:
            # Unparseable is UNKNOWN, never "not GitHub" (review #248, r3).
            raise HaltRefused(
                f"DarkFactory halt (refuse_unknown): cannot read a host from "
                f"repo_url {repo_url!r}",
                unread=True,
                config_error=True,
            )
        if host.lower() not in _GITHUB_HOSTS:
            return  # admit_not_github: the contract admits a non-GitHub origin
    try:
        key = identity_from_config(config)
    except IdentityError as err:
        raise HaltRefused(
            f"DarkFactory halt (refuse_unknown): identity unresolved: {err}",
            unread=True,
            config_error=True,
        ) from err
    refuse_if_halted(key.host, key.owner, key.repo, local=key.local)


def refuse_if_halted(host: str, owner: str, repo: str, *, local: bool) -> None:
    """Raise HaltRefused when a new run must not start; no-op when off."""
    if not enabled():
        return
    if local or host.lower() not in _GITHUB_HOSTS:
        return  # admit_not_github
    admit, code, reason = check(owner, repo)
    if not admit:
        raise HaltRefused(
            f"DarkFactory halt ({code}): {reason}", unread=code == "refuse_unknown"
        )
