"""Tests for the Agent Spawner module."""

from pathlib import Path

import pytest

from maestro.execution.models import ExecutionRequest
from maestro.models import AgentType, Task
from maestro.spawners import (
    AgentSpawner,
    AiderSpawner,
    AnnounceSpawner,
    ClaudeCodeSpawner,
    CodexSpawner,
)
from maestro.spawners.opencode import OpencodeSpawner, _qualify


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def sample_task(temp_dir: Path) -> Task:
    """Provide a sample task for testing."""
    return Task(
        id="test-task-1",
        title="Implement Feature X",
        prompt="Please implement feature X with the following requirements...",
        workdir=str(temp_dir),
        agent_type=AgentType.CLAUDE_CODE,
        scope=["src/module.py", "tests/test_module.py"],
    )


@pytest.fixture
def sample_task_no_scope(temp_dir: Path) -> Task:
    """Provide a sample task without scope for testing."""
    return Task(
        id="test-task-2",
        title="Fix Bug Y",
        prompt="Please fix the bug described in issue #123",
        workdir=str(temp_dir),
        agent_type=AgentType.CLAUDE_CODE,
    )


@pytest.fixture
def claude_spawner() -> ClaudeCodeSpawner:
    """Provide a Claude Code spawner instance."""
    return ClaudeCodeSpawner()


# =============================================================================
# Unit Tests: AgentSpawner ABC
# =============================================================================


class TestAgentSpawnerABC:
    """Tests for the AgentSpawner abstract base class.

    Only ``agent_type`` and ``build_request`` are abstract post-Task-7;
    the legacy ``spawn``/``is_available`` pair was removed once the
    scheduler, orchestrator, and benchmark responder all migrated onto
    ``build_request``/``ExecutionBackend``.
    """

    def test_cannot_instantiate_abc(self) -> None:
        """Test that AgentSpawner cannot be instantiated directly."""
        with pytest.raises(TypeError, match="abstract"):
            AgentSpawner()  # type: ignore[abstract]

    def test_concrete_spawner_must_implement_agent_type(self) -> None:
        """Test that concrete spawners must implement agent_type."""

        class IncompleteSpawner(AgentSpawner):
            def build_request(
                self,
                task: Task,
                context: str,
                workdir: Path,
                log_file: Path,
                run_id: str,
                retry_context: str = "",
                *,
                model: str | None = None,
            ) -> ExecutionRequest:
                raise NotImplementedError

        with pytest.raises(TypeError, match="agent_type"):
            IncompleteSpawner()  # type: ignore[abstract]

    def test_concrete_spawner_must_implement_build_request(self) -> None:
        """Test that concrete spawners must implement build_request."""

        class IncompleteSpawner(AgentSpawner):
            @property
            def agent_type(self) -> str:
                return "test"

        with pytest.raises(TypeError, match="build_request"):
            IncompleteSpawner()  # type: ignore[abstract]

    def test_can_build_request_defaults_true(self) -> None:
        """can_build_request() defaults to True when not overridden."""

        class MinimalSpawner(AgentSpawner):
            @property
            def agent_type(self) -> str:
                return "minimal"

            def build_request(
                self,
                task: Task,
                context: str,
                workdir: Path,
                log_file: Path,
                run_id: str,
                retry_context: str = "",
                *,
                model: str | None = None,
            ) -> ExecutionRequest:
                raise NotImplementedError

        assert MinimalSpawner().can_build_request() is True


# =============================================================================
# Unit Tests: Prompt Building
# =============================================================================


class TestPromptBuilding:
    """Tests for prompt building functionality."""

    def test_build_prompt_with_context_and_scope(
        self,
        claude_spawner: ClaudeCodeSpawner,
        sample_task: Task,
    ) -> None:
        """Test prompt building with context and scope."""
        context = "[task-0]: Completed initial setup successfully."
        prompt = claude_spawner.build_prompt(sample_task, context)

        # Should contain task title
        assert sample_task.title in prompt
        # Should contain task prompt
        assert sample_task.prompt in prompt
        # Should contain context
        assert context in prompt
        # Should contain scope
        assert "src/module.py" in prompt
        assert "tests/test_module.py" in prompt

    def test_build_prompt_without_context(
        self,
        claude_spawner: ClaudeCodeSpawner,
        sample_task: Task,
    ) -> None:
        """Test prompt building without context."""
        prompt = claude_spawner.build_prompt(sample_task, "")

        # Should contain task title
        assert sample_task.title in prompt
        # Should contain fallback message for no context
        assert "No prior context available" in prompt

    def test_build_prompt_without_scope(
        self,
        claude_spawner: ClaudeCodeSpawner,
        sample_task_no_scope: Task,
    ) -> None:
        """Test prompt building without scope restriction."""
        context = "Some context"
        prompt = claude_spawner.build_prompt(sample_task_no_scope, context)

        # Should indicate any files can be modified
        assert "any" in prompt.lower()

    def test_build_prompt_structure(
        self,
        claude_spawner: ClaudeCodeSpawner,
        sample_task: Task,
    ) -> None:
        """Test that prompt has expected structure."""
        context = "Previous task output"
        prompt = claude_spawner.build_prompt(sample_task, context)

        # Verify sections exist in order
        task_pos = prompt.find("Task:")
        prompt_pos = prompt.find(sample_task.prompt)
        context_pos = prompt.find("Context from completed dependencies:")
        scope_pos = prompt.find("Scope (files you can modify):")

        assert task_pos >= 0
        assert prompt_pos > task_pos
        assert context_pos > prompt_pos
        assert scope_pos > context_pos


# =============================================================================
# Unit Tests: ClaudeCodeSpawner
# =============================================================================


class TestClaudeCodeSpawner:
    """Tests for ClaudeCodeSpawner."""

    def test_agent_type(self, claude_spawner: ClaudeCodeSpawner) -> None:
        """Test that agent_type returns correct value."""
        assert claude_spawner.agent_type == "claude_code"


# =============================================================================
# Tests: Spawner Inheritance
# =============================================================================


class TestSpawnerInheritance:
    """Tests for spawner inheritance patterns."""

    def test_custom_spawner_inherits_build_prompt(self) -> None:
        """Test that custom spawners inherit build_prompt method."""

        class CustomSpawner(AgentSpawner):
            @property
            def agent_type(self) -> str:
                return "custom"

            def build_request(
                self,
                task: Task,
                context: str,
                workdir: Path,
                log_file: Path,
                run_id: str,
                retry_context: str = "",
                *,
                model: str | None = None,
            ) -> ExecutionRequest:
                raise NotImplementedError

        spawner = CustomSpawner()
        task = Task(
            id="test",
            title="Test Task",
            prompt="Test prompt",
            workdir="/tmp",
        )

        # Should be able to use inherited build_prompt
        prompt = spawner.build_prompt(task, "context")
        assert "Test Task" in prompt
        assert "Test prompt" in prompt

    def test_custom_spawner_can_override_build_prompt(self) -> None:
        """Test that custom spawners can override build_prompt."""

        class CustomSpawner(AgentSpawner):
            @property
            def agent_type(self) -> str:
                return "custom"

            def build_prompt(
                self,
                task: Task,
                context: str,
                retry_context: str = "",
            ) -> str:
                return f"CUSTOM: {task.title}"

            def build_request(
                self,
                task: Task,
                context: str,
                workdir: Path,
                log_file: Path,
                run_id: str,
                retry_context: str = "",
                *,
                model: str | None = None,
            ) -> ExecutionRequest:
                raise NotImplementedError

        spawner = CustomSpawner()
        task = Task(
            id="test",
            title="Test Task",
            prompt="Test prompt",
            workdir="/tmp",
        )

        prompt = spawner.build_prompt(task, "context")
        assert prompt == "CUSTOM: Test Task"


# =============================================================================
# Test Fixtures: Additional Spawners
# =============================================================================


@pytest.fixture
def codex_spawner() -> CodexSpawner:
    """Provide a Codex spawner instance."""
    return CodexSpawner()


@pytest.fixture
def aider_spawner() -> AiderSpawner:
    """Provide an Aider spawner instance."""
    return AiderSpawner()


@pytest.fixture
def announce_spawner() -> AnnounceSpawner:
    """Provide an Announce spawner instance."""
    return AnnounceSpawner()


@pytest.fixture
def opencode_spawner() -> OpencodeSpawner:
    """Provide an opencode spawner instance."""
    return OpencodeSpawner()


# =============================================================================
# Unit Tests: CodexSpawner
# =============================================================================


class TestCodexSpawner:
    """Tests for CodexSpawner."""

    def test_agent_type(self, codex_spawner: CodexSpawner) -> None:
        """Test that agent_type returns correct value."""
        assert codex_spawner.agent_type == "codex_cli"


# =============================================================================
# Unit Tests: _qualify (opencode model prefix)
# =============================================================================


class TestQualify:
    """_qualify: bare model ids get opencode's provider prefix."""

    def test_bare_id_gets_prefix(self) -> None:
        assert _qualify("glm-5.1") == "opencode/glm-5.1"

    def test_provider_qualified_id_passes_through(self) -> None:
        assert _qualify("zai/glm-5.1") == "zai/glm-5.1"


# =============================================================================
# Unit Tests: OpencodeSpawner
# =============================================================================


class TestOpencodeSpawner:
    """Tests for OpencodeSpawner."""

    def test_agent_type(self, opencode_spawner: OpencodeSpawner) -> None:
        """Test that agent_type returns correct value."""
        assert opencode_spawner.agent_type == "opencode"


# =============================================================================
# Unit Tests: AiderSpawner
# =============================================================================


class TestAiderSpawner:
    """Tests for AiderSpawner."""

    def test_agent_type(self, aider_spawner: AiderSpawner) -> None:
        """Test that agent_type returns correct value."""
        assert aider_spawner.agent_type == "aider"


# =============================================================================
# Unit Tests: AnnounceSpawner
# =============================================================================


class TestAnnounceSpawner:
    """Tests for AnnounceSpawner."""

    def test_agent_type(self, announce_spawner: AnnounceSpawner) -> None:
        """Test that agent_type returns correct value."""
        assert announce_spawner.agent_type == "announce"


# =============================================================================
# build_request() / can_build_request() golden-argv tests (Task 4)
# =============================================================================


def _mk_task() -> Task:
    return Task(
        id="t1",
        title="T",
        prompt="do it",
        workdir="/tmp/wd",
        agent_type=AgentType.CLAUDE_CODE,
        scope=["a.py"],
    )


def test_claude_build_request_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAESTRO_CLAUDE_MODEL", "claude-sonnet-5")
    req = ClaudeCodeSpawner().build_request(
        _mk_task(),
        context="ctx",
        workdir=Path("/tmp/wd"),
        log_file=Path("/tmp/wd/t1.log"),
        run_id="run-1",
    )
    assert isinstance(req, ExecutionRequest)
    assert req.argv[0] == "claude"
    assert req.argv[1:4] == ["--model", "claude-sonnet-5", "--print"]
    assert req.argv[-2] == "-p"
    assert req.argv[-1].startswith("Task: T")
    assert req.inherit_env is True
    assert req.capture_output is False
    assert req.collect.mode == "none"
    assert req.required_tools == ["claude"]
    assert req.workdir == Path("/tmp/wd")
    assert req.log_path == Path("/tmp/wd/t1.log")


def test_announce_build_request_argv() -> None:
    req = AnnounceSpawner().build_request(
        _mk_task(),
        context="ctx",
        workdir=Path("/tmp/wd"),
        log_file=Path("/tmp/wd/t1.log"),
        run_id="run-1",
    )
    assert req.argv[0] == "echo"
    assert req.argv[1].startswith("Task: T")
    assert req.required_tools == []  # echo is a shell builtin/coreutil; no gate


def test_aider_build_request_appends_scope() -> None:
    req = AiderSpawner().build_request(
        _mk_task(),
        context="ctx",
        workdir=Path("/tmp/wd"),
        log_file=Path("/tmp/wd/t1.log"),
        run_id="run-1",
    )
    assert req.argv[0] == "aider"
    assert req.argv[-1] == "a.py"  # scope file appended last
    assert req.required_tools == ["aider"]


def test_can_build_request_true() -> None:
    assert ClaudeCodeSpawner().can_build_request() is True
    assert AnnounceSpawner().can_build_request() is True
    assert CodexSpawner().can_build_request() is True
    assert OpencodeSpawner().can_build_request() is True
