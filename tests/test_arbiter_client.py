"""Tests for ArbiterClient methods."""

from unittest.mock import AsyncMock, patch

import pytest

from maestro.coordination.arbiter_client import ArbiterClient, ArbiterClientConfig


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
