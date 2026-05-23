"""Tests for REST API workstreams endpoints.

This module contains unit tests for the workstreams-related REST API endpoints:
GET /workstreams, GET /workstreams/{workstream_id}, and POST /workstreams/{workstream_id}/callback.
"""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from maestro.coordination.rest_api import create_app_with_lifespan
from maestro.database import WorkstreamNotFoundError
from maestro.models import Workstream, WorkstreamStatus


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_db() -> AsyncMock:
    """Provide a mock database with async methods."""
    db = AsyncMock()
    db.is_connected = True
    return db


@pytest.fixture
def sample_workstream() -> Workstream:
    """Provide a sample workstream for testing."""
    return Workstream(
        id="workstream-001",
        title="Implement auth module",
        description="Add authentication to the API",
        branch="agent/workstream-001",
        workspace_path="/tmp/worktree/workstream-001",
        status=WorkstreamStatus.RUNNING,
        scope=["src/auth/**/*.py"],
        priority=10,
        pr_url=None,
        subtask_progress="2/5 done",
        error_message=None,
        retry_count=0,
        max_retries=2,
    )


@pytest.fixture
def sample_workstream_done() -> Workstream:
    """Provide a completed workstream for testing."""
    return Workstream(
        id="workstream-002",
        title="Fix database migration",
        description="Update schema migration scripts",
        branch="agent/workstream-002",
        workspace_path="/tmp/worktree/workstream-002",
        status=WorkstreamStatus.DONE,
        scope=["migrations/**/*.sql"],
        priority=5,
        pr_url="https://github.com/test/repo/pull/42",
        subtask_progress="7/7 done",
        error_message=None,
        retry_count=0,
        max_retries=2,
    )


@pytest.fixture
async def client(mock_db: AsyncMock) -> AsyncGenerator[AsyncClient, None]:
    """Provide an async HTTP client for testing workstreams endpoints."""
    app = create_app_with_lifespan()
    transport = ASGITransport(app=app)
    with patch("maestro.coordination.rest_api._db", mock_db):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# =============================================================================
# Unit Tests: List Workstreams
# =============================================================================


class TestListWorkstreams:
    """Tests for GET /workstreams endpoint."""

    @pytest.mark.anyio
    async def test_list_workstreams_returns_list(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
        sample_workstream: Workstream,
        sample_workstream_done: Workstream,
    ) -> None:
        """Test that GET /workstreams returns a list of workstreams."""
        mock_db.get_all_workstreams.return_value = [
            sample_workstream,
            sample_workstream_done,
        ]

        response = await client.get("/workstreams")

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert len(data["workstreams"]) == 2
        assert data["workstreams"][0]["id"] == "workstream-001"
        assert data["workstreams"][0]["title"] == "Implement auth module"
        assert data["workstreams"][0]["status"] == "running"
        assert data["workstreams"][0]["scope"] == ["src/auth/**/*.py"]
        assert data["workstreams"][0]["priority"] == 10
        assert data["workstreams"][0]["subtask_progress"] == "2/5 done"
        assert data["workstreams"][1]["id"] == "workstream-002"
        assert data["workstreams"][1]["status"] == "done"
        assert (
            data["workstreams"][1]["pr_url"] == "https://github.com/test/repo/pull/42"
        )
        mock_db.get_all_workstreams.assert_awaited_once()

    @pytest.mark.anyio
    async def test_list_workstreams_empty(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
    ) -> None:
        """Test that GET /workstreams returns empty list when no workstreams exist."""
        mock_db.get_all_workstreams.return_value = []

        response = await client.get("/workstreams")

        assert response.status_code == 200
        data = response.json()
        assert data["workstreams"] == []
        assert data["count"] == 0
        mock_db.get_all_workstreams.assert_awaited_once()


# =============================================================================
# Unit Tests: Get Workstream Detail
# =============================================================================


class TestGetWorkstreamDetail:
    """Tests for GET /workstreams/{workstream_id} endpoint."""

    @pytest.mark.anyio
    async def test_get_workstream_returns_detail(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
        sample_workstream: Workstream,
    ) -> None:
        """Test that GET /workstreams/{id} returns workstream details."""
        mock_db.get_workstream.return_value = sample_workstream

        response = await client.get("/workstreams/workstream-001")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "workstream-001"
        assert data["title"] == "Implement auth module"
        assert data["description"] == "Add authentication to the API"
        assert data["branch"] == "agent/workstream-001"
        assert data["workspace_path"] == "/tmp/worktree/workstream-001"
        assert data["status"] == "running"
        assert data["scope"] == ["src/auth/**/*.py"]
        assert data["priority"] == 10
        assert data["pr_url"] is None
        assert data["subtask_progress"] == "2/5 done"
        assert data["error_message"] is None
        assert data["retry_count"] == 0
        assert data["max_retries"] == 2
        mock_db.get_workstream.assert_awaited_once_with("workstream-001")

    @pytest.mark.anyio
    async def test_get_workstream_not_found(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
    ) -> None:
        """Test that GET /workstreams/{id} returns 404 when not found."""
        mock_db.get_workstream.side_effect = WorkstreamNotFoundError(
            "Workstream 'nonexistent' not found"
        )

        response = await client.get("/workstreams/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
        mock_db.get_workstream.assert_awaited_once_with("nonexistent")


# =============================================================================
# Unit Tests: Workstream Callback
# =============================================================================


class TestWorkstreamCallback:
    """Tests for POST /workstreams/{workstream_id}/callback endpoint."""

    @pytest.mark.anyio
    async def test_callback_updates_workstream(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
        sample_workstream: Workstream,
    ) -> None:
        """Test that valid callback updates workstream status."""
        mock_db.get_workstream.return_value = sample_workstream
        mock_db.update_workstream_status.return_value = sample_workstream

        response = await client.post(
            "/workstreams/workstream-001/callback",
            json={
                "task_id": "subtask-3",
                "status": "completed",
                "duration_seconds": 42.5,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Updated workstream-001" in data["message"]
        mock_db.get_workstream.assert_awaited_once_with("workstream-001")
        mock_db.update_workstream_status.assert_awaited_once_with(
            "workstream-001",
            WorkstreamStatus.RUNNING,
            subtask_progress="subtask-3: completed",
        )

    @pytest.mark.anyio
    async def test_callback_workstream_not_found(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
    ) -> None:
        """Test that callback returns failure when workstream not found."""
        mock_db.get_workstream.side_effect = WorkstreamNotFoundError(
            "Workstream 'missing' not found"
        )

        response = await client.post(
            "/workstreams/missing/callback",
            json={
                "task_id": "subtask-1",
                "status": "failed",
                "error": "Build error",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["message"].lower()

    @pytest.mark.anyio
    async def test_callback_invalid_payload(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
    ) -> None:
        """Test that invalid callback payload returns 422."""
        response = await client.post(
            "/workstreams/workstream-001/callback",
            json={"invalid": "payload"},
        )

        assert response.status_code == 422

    @pytest.mark.anyio
    async def test_callback_missing_required_fields(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
    ) -> None:
        """Test that missing required fields in callback returns 422."""
        response = await client.post(
            "/workstreams/workstream-001/callback",
            json={"task_id": "subtask-1"},
        )

        assert response.status_code == 422

    @pytest.mark.anyio
    async def test_callback_with_error_field(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
        sample_workstream: Workstream,
    ) -> None:
        """Test callback with optional error field."""
        mock_db.get_workstream.return_value = sample_workstream
        mock_db.update_workstream_status.return_value = sample_workstream

        response = await client.post(
            "/workstreams/workstream-001/callback",
            json={
                "task_id": "subtask-5",
                "status": "failed",
                "duration_seconds": 10.0,
                "error": "Timeout exceeded",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        mock_db.update_workstream_status.assert_awaited_once_with(
            "workstream-001",
            WorkstreamStatus.RUNNING,
            subtask_progress="subtask-5: failed",
        )

    @pytest.mark.anyio
    async def test_callback_default_duration(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
        sample_workstream: Workstream,
    ) -> None:
        """Test callback uses default duration_seconds when omitted."""
        mock_db.get_workstream.return_value = sample_workstream
        mock_db.update_workstream_status.return_value = sample_workstream

        response = await client.post(
            "/workstreams/workstream-001/callback",
            json={
                "task_id": "subtask-1",
                "status": "started",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
