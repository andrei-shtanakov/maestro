"""Pure scope-containment matcher (no git, no FS, no DB).

`find_escapes` answers one question: which of these changed paths is matched
by none of the declared scope globs? Callers pass already-normalized,
repo-relative POSIX paths and patterns (see `normalize`).
"""

from __future__ import annotations

import re


def normalize(paths: list[str]) -> list[str]:
    """Normalize to repo-relative POSIX form: backslash->slash, strip './'."""
    result: list[str] = []
    for raw in paths:
        p = raw.replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        result.append(p)
    return result


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a pathlib-glob-style pattern to an anchored regex.

    `**` matches any number of segments (including zero); `*` matches within a
    single segment (never crosses `/`); `?` matches one non-slash char; every
    other character is literal.
    """
    i, n = 0, len(pattern)
    out: list[str] = []
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                    out.append("(?:[^/]*/)*")  # '**/' -> zero+ leading dirs
                else:
                    out.append(".*")  # trailing '**' -> everything
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "/":
            out.append("/")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def find_escapes(changed_paths: list[str], scope: list[str]) -> list[str]:
    """Return the changed paths matched by no scope pattern.

    Empty result means containment holds. An empty `scope` returns `[]`
    (nothing to enforce — skip).
    """
    if not scope:
        return []
    matchers = [_glob_to_regex(p) for p in scope]
    return [path for path in changed_paths if not any(m.match(path) for m in matchers)]
