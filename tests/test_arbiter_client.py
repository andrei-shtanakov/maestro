"""Tests for ArbiterClient methods."""

from unittest.mock import AsyncMock, patch

import pytest

from maestro.coordination.arbiter_client import ArbiterClient, ArbiterClientConfig
from maestro.coordination.arbiter_errors import ArbiterContractError, ArbiterUnavailable


@pytest.mark.asyncio
async def test_report_benchmark_raw_delegates_to_call_tool():
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    payload_dict = {
        "payload_version": "1.0.0",
        "run_id": "x",
        "benchmark_id": "b",
        "agent_id": "a",
        "ts": "2026-05-23T12:00:00Z",
        "score": 0.5,
        "score_components": {},
        "total_tokens": None,
        "total_cost_usd": None,
        "duration_seconds": 1.0,
        "per_task": [],
        "per_task_total_count": 0,
        "per_task_truncated": False,
    }
    with patch.object(
        client,
        "_call_tool",
        new=AsyncMock(return_value={"status": "created", "run_id": "x"}),
    ) as mock_call:
        result = await client.report_benchmark_raw(payload_dict)
    mock_call.assert_awaited_once_with("report_benchmark", payload_dict)
    assert result == {"status": "created", "run_id": "x"}


@pytest.mark.asyncio
async def test_send_and_receive_raises_contract_error_on_invalid_params():
    """JSON-RPC code -32602 (invalid params) → contract."""
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    fake_response = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": -32602,
            "message": "missing agent_id",
            "data": {"field": "agent_id"},
        },
    }
    with (
        patch.object(client, "_write_message", new=AsyncMock()),
        patch.object(
            client, "_read_response", new=AsyncMock(return_value=fake_response)
        ),
        pytest.raises(ArbiterContractError) as exc,
    ):
        await client._send_and_receive(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}
        )
    assert exc.value.code == -32602
    assert exc.value.message == "missing agent_id"
    assert exc.value.data == {"field": "agent_id"}


@pytest.mark.asyncio
async def test_send_and_receive_raises_contract_error_on_invalid_request():
    """JSON-RPC code -32600 (invalid request) → contract."""
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    fake_response = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32600, "message": "malformed request"},
    }
    with (
        patch.object(client, "_write_message", new=AsyncMock()),
        patch.object(
            client, "_read_response", new=AsyncMock(return_value=fake_response)
        ),
        pytest.raises(ArbiterContractError),
    ):
        await client._send_and_receive(
            {"jsonrpc": "2.0", "id": 1, "method": "x", "params": {}}
        )


@pytest.mark.asyncio
async def test_send_and_receive_raises_contract_error_on_internal_error():
    """JSON-RPC code -32603 (internal) → contract (we treat server-side internal as contract drift)."""
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    fake_response = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32603, "message": "internal"},
    }
    with (
        patch.object(client, "_write_message", new=AsyncMock()),
        patch.object(
            client, "_read_response", new=AsyncMock(return_value=fake_response)
        ),
        pytest.raises(ArbiterContractError),
    ):
        await client._send_and_receive(
            {"jsonrpc": "2.0", "id": 1, "method": "x", "params": {}}
        )


@pytest.mark.asyncio
async def test_send_and_receive_raises_unavailable_on_other_codes():
    """Non-contract JSON-RPC errors (e.g. -32000 server error) → unavailable (transient)."""
    client = ArbiterClient(ArbiterClientConfig(binary_path="/fake"))
    fake_response = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32000, "message": "server error"},
    }
    with (
        patch.object(client, "_write_message", new=AsyncMock()),
        patch.object(
            client, "_read_response", new=AsyncMock(return_value=fake_response)
        ),
        pytest.raises(ArbiterUnavailable),
    ):
        await client._send_and_receive(
            {"jsonrpc": "2.0", "id": 1, "method": "x", "params": {}}
        )
