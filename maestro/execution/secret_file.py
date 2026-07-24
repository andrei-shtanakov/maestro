"""Shared 0600 env-file writer with control-char validation.

Extracted from DockerIsolator.materialize so the SSH backend reuses the exact
same secret-file discipline (values never in argv; forbidden control chars
rejected so a value cannot corrupt the KEY=value format or inject lines).
"""

import os
from collections.abc import Mapping
from pathlib import Path


def validate_secret_value(name: str, value: str) -> None:
    """Raise ValueError if a secret value has a forbidden control char."""
    if any(c in value for c in ("\n", "\r", "\x00")):
        raise ValueError(f"secret {name} value has a forbidden control char")


def write_env_file(path: Path, names: list[str], source_env: Mapping[str, str]) -> Path:
    """Write a 0600 env-file of `KEY=value` lines for names present in env."""
    lines: list[str] = []
    for name in names:
        if name not in source_env:
            continue
        value = source_env[name]
        validate_secret_value(name, value)
        lines.append(f"{name}={value}")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))
    path.chmod(0o600)
    return path
