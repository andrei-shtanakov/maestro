"""Tests for arbiter client version constants and _handshake() protocol checks.

R-06b M4 Task 3.1: MIN_ARBITER_PROTOCOL range check + version constant bumps.
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from maestro.coordination.arbiter_client import (
    ARBITER_MCP_REQUIRED_VERSION,
    ARBITER_PROTOCOL_VERSION,
    ARBITER_VENDORED_FROM_SHA,
    MIN_ARBITER_PROTOCOL,
    ArbiterClient,
    ArbiterClientConfig,
)
from maestro.coordination.arbiter_errors import (
    ArbiterContractError,
    ArbiterStartupError,
)


def test_constants_present_and_consistent():
    """Vendored constant pair: declared current + minimum supported + SHA pin."""
    assert isinstance(MIN_ARBITER_PROTOCOL, tuple) and len(MIN_ARBITER_PROTOCOL) == 2
    cur_major, cur_minor = map(int, ARBITER_PROTOCOL_VERSION.split(".")[:2])
    assert (cur_major, cur_minor) >= MIN_ARBITER_PROTOCOL
    assert MIN_ARBITER_PROTOCOL == (1, 1), "M4 sets MIN at 1.1 (report_benchmark added)"
    assert ARBITER_MCP_REQUIRED_VERSION == "0.2.0", "bumped for arbiter Phase 1"
    assert len(ARBITER_VENDORED_FROM_SHA) == 40, "SHA should be full 40-char hex"


def _mock_init_response(server_version: str, protocol_version: str) -> dict:
    return {
        "protocolVersion": protocol_version,
        "serverInfo": {"name": "arbiter-mcp", "version": server_version},
        "capabilities": {},
    }


@pytest.mark.asyncio
async def test_handshake_passes_when_versions_match():
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    with (
        patch.object(client, "_spawn_process", new=AsyncMock()),
        patch.object(
            client,
            "_send_request",
            new=AsyncMock(return_value=_mock_init_response("0.2.0", "1.5")),
        ),
        patch.object(client, "_send_notification", new=AsyncMock()),
    ):
        await client.start()


@pytest.mark.asyncio
async def test_handshake_raises_on_serverinfo_version_mismatch():
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    with (
        patch.object(client, "_spawn_process", new=AsyncMock()),
        patch.object(
            client,
            "_send_request",
            new=AsyncMock(return_value=_mock_init_response("0.99.0", "1.5")),
        ),
        patch.object(client, "_send_notification", new=AsyncMock()),
    ):
        with pytest.raises(ArbiterStartupError) as exc:
            await client.start()
        assert "version mismatch" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_handshake_warns_on_protocol_minor_below_min(caplog):
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    # Same Cargo version, but protocolVersion 1.0 < required (1, 1).
    with (
        patch.object(client, "_spawn_process", new=AsyncMock()),
        patch.object(
            client,
            "_send_request",
            new=AsyncMock(return_value=_mock_init_response("0.2.0", "1.0")),
        ),
        patch.object(client, "_send_notification", new=AsyncMock()),
    ):
        caplog.set_level(logging.WARNING)
        await client.start()
    assert (
        "protocol minor" in caplog.text.lower()
        or "report_benchmark may be missing" in caplog.text
    )


@pytest.mark.asyncio
async def test_handshake_raises_contract_error_on_protocol_major_mismatch():
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    with (
        patch.object(client, "_spawn_process", new=AsyncMock()),
        patch.object(
            client,
            "_send_request",
            new=AsyncMock(return_value=_mock_init_response("0.2.0", "2.0")),
        ),
        patch.object(client, "_send_notification", new=AsyncMock()),
    ):
        with pytest.raises(ArbiterContractError) as exc:
            await client.start()
        assert "major" in str(exc.value).lower()
