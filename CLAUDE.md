# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## `../_cowork_output/` is dev-only — never a code/runtime resource

`../_cowork_output/` (the polyrepo **sibling** workspace — not to be confused with this repo's own local `./_cowork_output/` scratch directory) is the development-time coordination area (cross-team ADRs, status notes, contract drafts, PM/dev tooling). Users and teams installing or cloning this project do NOT have it. Rules:

- Shipped/runtime code must never read, import, or resolve paths under `../_cowork_output/`.
- Canonical shippable facts live inside the owning repo: the ecosystem agents-catalog SSOT is `atp-platform/method/agents-catalog.toml` (ADR-ECO-003, canon confirmed 2026-07-03); Maestro's spawner model defaults are (to be) generated from that catalog (ADR-ECO-003 action #4), never resolved from `../_cowork_output/` at runtime.
- Vendoring a pinned copy INTO a repo is the correct pattern; referencing OUT to `../_cowork_output/` from shipped code is the antipattern.
- Only workspace-local dev tooling (e.g. the conformance check in `../_cowork_output/devtools/`) and documentation may reference it.

## Project Overview

Maestro is an AI Agent Orchestrator with two operation modes:

1. **Task Scheduler** (`maestro run`) — coordinates multiple AI coding agents (Claude Code, Codex, opencode, Aider) on tasks defined in a single YAML config. All tasks share one directory.

2. **Multi-Process Orchestrator** (`maestro orchestrate`) — decomposes a project into independent work units ("workstreams"), runs each in an isolated git worktree via spec-runner, and creates PRs on completion.

## Development Commands

```bash
# === Task Scheduler (original mode) ===
uv run maestro run <config.yaml>
uv run maestro run config.yaml --resume  # Resume after crash
uv run maestro status --db maestro.db
uv run maestro retry <task-id> --db maestro.db
uv run maestro stop                          # Stop the running scheduler
uv run maestro approve <task-id> --db maestro.db  # Approve an AWAITING_APPROVAL task

# === Multi-Process Orchestrator (new mode) ===
uv run maestro orchestrate <project.yaml>   # Run orchestrator
uv run maestro workstreams --db maestro.db       # Show workstreams status
uv run maestro workspaces <project.yaml>     # List active worktrees

# === Log utilities ===
uv run maestro merge-logs <pipeline-dir>     # Time-sort per-pid JSONL into merged.jsonl

# === Tests ===
uv run pytest
uv run pytest tests/test_models.py -v       # Single file
uv run pytest -k "test_dag" -v              # By pattern

# === Type checking ===
uv run pyrefly check

# === Linting and formatting ===
uv run ruff format .
uv run ruff check .
uv run ruff check . --fix

# === Dependencies (NEVER use pip) ===
uv add <package>
uv add --dev <package>
```

## Architecture

### Core modules in `maestro/`

**Shared infrastructure:**
- **models.py**: Pydantic models (Task, TaskStatus, Workstream, WorkstreamStatus, OrchestratorConfig)
- **config.py**: YAML parsing with defaults merging, env var substitution, `load_orchestrator_config()`
- **catalog.py**: Model catalog loader (ADR-ECO-003b). `resolve_model()` applies the precedence `routed > MAESTRO_<H>_MODEL > catalog-default > fail-loud`; the catalog (loaded from `$ATP_CATALOG`, no baked default) supplies only the last-resort *default* layer, used when neither a routed model nor the env var provides one. Also emits a status-graded coherence warning. Fault taxonomy by blast radius: `CatalogError` (global — halts the run) vs `HarnessModelUnresolved` (per-task — sends that task to `NEEDS_REVIEW`)
- **database.py**: SQLite layer with async CRUD, WAL mode (tasks + workstreams tables)
- **dag.py**: DAG building, cycle detection, topological sort, scope overlap warnings
- **git.py**: Git operations (branch, rebase, push, worktree, merge)
- **cli.py**: Typer CLI (run, status, retry, stop, approve, orchestrate, workstreams, workspaces, merge-logs)
- **scheduler.py**: Main scheduler loop — polls DAG, spawns agents, monitors completion
- **validator.py**: Post-task validation (run validation_cmd)
- **retry.py**: Exponential backoff retry logic with jitter
- **recovery.py**: State recovery after crash
- **cost_tracker.py**: Token usage parsing and cost calculation
- **event_log.py**: Structured event logging for task lifecycle
- **merge_logs.py**: Standalone merge-logs CLI — time-sorts per-pid JSONL into merged.jsonl
- **spec_runner.py**: Integration boundary between Maestro and the external spec-runner

**Multi-process orchestration (new):**
- **orchestrator.py**: Main async loop — decompose, spawn, monitor, PR creation
- **workspace.py**: Git worktree lifecycle (create, setup, cleanup)
- **decomposer.py**: Project decomposition via Claude CLI into workstreams + spec generation
- **pr_manager.py**: GitHub PR creation via `gh` CLI

**Subpackages:**
- **spawners/**: AgentSpawner ABC + implementations (claude_code, codex_cli, opencode, aider, announce) + registry; opencode is the first open-model agentic harness (`opencode run -m opencode/<model>`, ADR-ECO-003c)
- **coordination/**: MCP server (FastMCP) + REST API (FastAPI) with /workstreams endpoints; Arbiter routing (`routing.py` strategies, vendored `arbiter_client.py` MCP client, `arbiter_errors.py`)
- **benchmark/**: R-06b/R-07 benchmark-aware routing — async runner, ATP client, spawner→responder adapter, and Arbiter feedback wiring (`arbiter_report.py`)
- **notifications/**: Desktop notifications (macOS/Linux)
- **dashboard/**: Web UI with DAG visualization (Mermaid.js) + SSE updates
- **schemas/**: JSON-schema generation for config/contract artifacts
- **_vendor/**: Vendored observability lib (`obs.py`) — structlog-based spans, trace propagation, and `child_env()` for cross-process trace continuity

### Task State Machine (scheduler mode)

```
PENDING -> READY -> RUNNING -> VALIDATING -> DONE
             |        |  |         |
             |        |  |         └-> FAILED -> READY (retry)
             |        |  |              |
             |        |  └──FAILED──────┴-> NEEDS_REVIEW -> READY
             |        |                      |
             |        └──NEEDS_REVIEW────────┘   (catalog default unresolved for harness)
             |                            |
             |                            └-> ABANDONED
             |
             └-> AWAITING_APPROVAL -> READY   (requires_approval; `maestro approve
                       |             <task-id>` sets READY, then scheduler runs it)
                       └-> ABANDONED
```

### Workstream State Machine (orchestrator mode)

```
PENDING -> DECOMPOSING -> READY -> RUNNING -> MERGING -> PR_CREATED -> DONE
                            |         |
                            |         └-> FAILED -> READY (retry)
                            |               |
                            |               └-> NEEDS_REVIEW -> READY
                            |
                            └-> ABANDONED
```

### Key Design Decisions

- **Two modes**: Scheduler for single-process tasks, Orchestrator for multi-process isolation
- **Workspace isolation**: git worktree per workstream (lightweight, shares .git)
- **Two-level hierarchy**: Orchestrator manages workstreams, spec-runner manages subtasks within each
- **Git strategy**: `feature/<workstream-id>` branch per workstream, subtask branches merge into it, then PR to main
- **Communication**: REST API callbacks from spec-runner (state file polling deprecated)
- **Conflict prevention**: Workstreams define `scope` (file/dir globs), decomposer validates non-overlap
- **Storage**: SQLite (single file, no external services)
- **Spec-runner**: External package (PyPI) handles subtask execution within a worktree

### Orchestrator Flow

```
1. Load project.yaml
2. Decompose project into workstreams (Claude CLI or manual config)
3. For each ready workstream:
   a. Create git worktree + branch
   b. Generate spec/tasks.md via Claude CLI (read-only tools)
   c. Write spec-runner.config.yaml
   d. Commit spec in feature branch
   e. Spawn `spec-runner run --all` subprocess
4. Monitor processes (poll returncode + callbacks)
5. On success: auto-merge feature branch into base, create PR (if auto_pr), cleanup worktree
6. On failure: retry or mark NEEDS_REVIEW
```

## Tech Stack

- Python 3.12+, uv for package management
- FastAPI + uvicorn for REST API and dashboard
- FastMCP for MCP server
- SQLite (aiosqlite) for state persistence
- PyYAML for configuration
- Pydantic for data models
- Typer + Rich for CLI
- git worktree for workspace isolation
- gh CLI for PR creation
- spec-runner (external) for subtask execution

## Ideas and Docs

- Направление/идеи (авторинг workstream'ов, scaffold/SDK): см. `docs/idea-workstream-framework.md`
