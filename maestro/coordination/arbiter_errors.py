"""Maestro-native exception types for the Arbiter integration.

Kept as a separate module (not inside the vendored arbiter_client.py)
so consumers and tests can import these without pulling in the full
vendored client transitive surface.
"""

from __future__ import annotations


class ArbiterError(Exception):
    """Base class for all Arbiter-integration errors."""


class ArbiterStartupError(ArbiterError):
    """Raised at startup when the Arbiter subprocess cannot be brought up.

    Covers: missing/non-executable binary, failed handshake, version
    mismatch against ARBITER_MCP_REQUIRED_VERSION. Fail-fast by default;
    caller can opt into graceful fallback via ArbiterConfig.optional=True.
    """

    def __init__(self, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


class ArbiterUnavailable(ArbiterError):
    """Raised at runtime when a live Arbiter call fails.

    Covers: broken pipe on subprocess stdio, read timeout, JSON parse
    failure. ArbiterRouting catches this for route-path (delegates to
    static fallback); report_outcome path re-raises so the scheduler
    can apply mode-dependent retry gating.
    """

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class ArbiterContractError(ArbiterError):
    """JSON-RPC error from arbiter indicating schema or protocol mismatch.

    Always means: vendored client diverged from server, payload bug, or
    payload_version mismatch. Never transient — retry is meaningless.
    Sibling to ArbiterUnavailable (which IS transient).
    """

    def __init__(self, code: int, message: str, data: dict | None = None) -> None:
        self.code = code
        self.message = message
        self.data = data or {}
        super().__init__(f"contract error {code}: {message}")
