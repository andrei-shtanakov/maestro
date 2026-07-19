# Stdlib→Obs Logging Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the ~93 stdlib `logging` call sites across ~16 maestro modules into the vendored obs OTel-JSONL pipeline without touching those modules or the vendored `obs.py`.

**Architecture:** A `logging.Handler` bridge forwards every stdlib record into `obs.get_logger(...)`, so records flow through the existing structlog pipeline (contextvars merge → redaction → OTel reshape → JSONL file) and automatically pick up `TraceId`/`SpanId`/`pipeline_id`. A `setup_logging()` wrapper calls `obs.init_logging()` and attaches the bridge plus a WARNING+ stderr passthrough (preserving today's `logging.lastResort` operator visibility). The three `init_logging("maestro")` call sites in `cli.py` switch to `setup_logging("maestro")`.

**Tech Stack:** Python 3.12+ (per `pyproject.toml` `requires-python = ">=3.12"`), stdlib `logging`, vendored structlog obs (`maestro/_vendor/obs.py` — DO NOT MODIFY, it is a pinned copy of the spec-runner contract), pytest.

## Global Constraints

- Package management: **only `uv`** (`uv run pytest`, `uv run ruff ...`).
- Line length 88, type hints required, docstrings on public APIs.
- `maestro/_vendor/obs.py` is a pinned vendored contract copy — never edit it; build on top.
- After changes: `uv run ruff format . && uv run ruff check . --fix`, then `uv run pyrefly check`.
- Git: branch `feat/stdlib-obs-bridge` off `master`; changes land only via PR; merge is done by a human.
- Behavior guarantee: WARNING+ records must still reach stderr (today they get there via `logging.lastResort`; attaching handlers disables lastResort, so the bridge setup adds an explicit stderr handler).

## Key vendored-obs facts (for the implementer)

- `obs.init_logging(project, *, level=None, log_dir=None, redact_keys=None)` — configures structlog: JSONL file `<log_dir>/<project>-<pid>.jsonl`, level from `ORCHESTRA_LOG_LEVEL` (default info), binds `pipeline_id`/`_trace_id`/`_span_id` contextvars (`obs.py:148-202`).
- `obs.get_logger(module)` → `structlog.get_logger(module=module)`; each call returns a fresh lazy proxy, so re-`init_logging` (tests) is safe — no stale cached config.
- The final reshape processor (`obs.py:108-137`): `event` kwarg → `Attributes.event`; `_body` kwarg → `Body` (defaults to event name); `module` is promoted into `Attributes`; severity comes from the method name (`info`/`warning`/...).
- Test pattern (from `tests/test_scheduler_observability.py:23-34`): `monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))` or pass `log_dir=`, call init, then `tmp_path.glob("maestro-*.jsonl")`.

## File Structure

| File | Role |
|---|---|
| `maestro/logging_bridge.py` | **Create**: `ObsBridgeHandler`, `_BridgeStderrHandler`, `setup_logging()` |
| `maestro/cli.py` | Modify: import (line 43) + 3 call sites (`cli.py:826`, `cli.py:1110`, `cli.py:1441`) |
| `tests/test_logging_bridge.py` | **Create**: bridge unit tests |
| `TODO.md` | Modify: record the closed unification item in the observability block |

---

### Task 1: The bridge module

**Files:**
- Create: `maestro/logging_bridge.py`
- Test: `tests/test_logging_bridge.py`

**Interfaces:**
- Consumes: `obs.init_logging(project, *, level, log_dir)`, `obs.get_logger(module)` from `maestro/_vendor/obs.py`.
- Produces: `setup_logging(project: str = "maestro", *, level: str | None = None, log_dir: Path | None = None) -> None`; `ObsBridgeHandler(logging.Handler)`; `_BridgeStderrHandler(logging.StreamHandler)`. Task 2 imports `setup_logging` only.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_logging_bridge.py`:

```python
"""Tests for the stdlib logging -> obs JSONL bridge."""

import json
import logging
from pathlib import Path

import pytest

from maestro.logging_bridge import (
    ObsBridgeHandler,
    _BridgeStderrHandler,
    setup_logging,
)


@pytest.fixture(autouse=True)
def clean_root_handlers():
    """Remove bridge handlers from the root logger after each test."""
    root = logging.getLogger()
    saved_level = root.level
    yield
    for handler in root.handlers[:]:
        if isinstance(handler, (ObsBridgeHandler, _BridgeStderrHandler)):
            root.removeHandler(handler)
    root.setLevel(saved_level)


def _read_records(log_dir: Path) -> list[dict]:
    files = list(log_dir.glob("maestro-*.jsonl"))
    assert len(files) == 1, f"expected 1 jsonl file, got {files}"
    return [
        json.loads(line)
        for line in files[0].read_text().splitlines()
        if line.strip()
    ]


def test_stdlib_record_lands_in_jsonl_with_trace_context(tmp_path) -> None:
    setup_logging("maestro", log_dir=tmp_path)
    logging.getLogger("maestro.retry").info("hello %s", "world")

    recs = [r for r in _read_records(tmp_path) if r["Body"] == "hello world"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["SeverityText"] == "INFO"
    assert rec["Attributes"]["event"] == "log.stdlib"
    assert rec["Attributes"]["module"] == "maestro.retry"
    assert rec["TraceId"] != "0" * 32
    assert rec["Attributes"]["pipeline_id"]


def test_level_filtering_respects_explicit_level(tmp_path) -> None:
    setup_logging("maestro", level="warning", log_dir=tmp_path)
    log = logging.getLogger("maestro.retry")
    log.info("dropped")
    log.warning("kept")

    bodies = [r["Body"] for r in _read_records(tmp_path)]
    assert "kept" in bodies
    assert "dropped" not in bodies


def test_exception_info_is_captured(tmp_path) -> None:
    setup_logging("maestro", log_dir=tmp_path)
    log = logging.getLogger("maestro.validator")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("task exploded")

    recs = [r for r in _read_records(tmp_path) if r["Body"] == "task exploded"]
    assert len(recs) == 1
    exc = recs[0]["Attributes"]["exception"]
    assert exc["type"] == "ValueError"
    assert exc["message"] == "boom"
    assert "ValueError: boom" in exc["stacktrace"]
    assert recs[0]["SeverityText"] == "ERROR"


def test_warning_passthrough_to_stderr(tmp_path, capsys) -> None:
    setup_logging("maestro", log_dir=tmp_path)
    log = logging.getLogger("maestro.pr_manager")
    log.info("quiet info")
    log.warning("loud warning")

    err = capsys.readouterr().err
    assert "loud warning" in err
    assert "quiet info" not in err


def test_setup_is_idempotent(tmp_path) -> None:
    setup_logging("maestro", log_dir=tmp_path)
    setup_logging("maestro", log_dir=tmp_path)

    root = logging.getLogger()
    bridges = [h for h in root.handlers if isinstance(h, ObsBridgeHandler)]
    stderrs = [h for h in root.handlers if isinstance(h, _BridgeStderrHandler)]
    assert len(bridges) == 1
    assert len(stderrs) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_logging_bridge.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'maestro.logging_bridge'`.

- [ ] **Step 3: Implement `maestro/logging_bridge.py`**

```python
"""Route stdlib logging records into the vendored obs structlog pipeline.

~16 maestro modules log via stdlib ``logging.getLogger(__name__)``. Without
configuration those records never reach the OTel JSONL sink (only WARNING+
leak to stderr via ``logging.lastResort``). This bridge forwards every
stdlib record through ``obs.get_logger`` so it flows through the standard
pipeline (contextvars merge -> redaction -> OTel reshape -> JSONL) and
picks up TraceId/SpanId/pipeline_id automatically.

The vendored ``maestro/_vendor/obs.py`` is a pinned contract copy and is
deliberately not modified; this module builds on top of it.
"""

import logging
import os
import traceback
from pathlib import Path
from typing import Any

from maestro._vendor import obs

_STDLIB_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _method_for_level(levelno: int) -> str:
    """Map a stdlib level number to a structlog method name."""
    if levelno >= logging.CRITICAL:
        return "critical"
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    if levelno >= logging.INFO:
        return "info"
    return "debug"


class ObsBridgeHandler(logging.Handler):
    """Forward stdlib records into the obs JSONL pipeline."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            kwargs: dict[str, Any] = {"_body": record.getMessage()}
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                kwargs["exception"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "stacktrace": "".join(
                        traceback.format_exception(*record.exc_info)
                    ),
                }
            log = obs.get_logger(record.name)
            getattr(log, _method_for_level(record.levelno))(
                "log.stdlib", **kwargs
            )
        except Exception:
            self.handleError(record)


class _BridgeStderrHandler(logging.StreamHandler):
    """WARNING+ stderr passthrough, replacing logging.lastResort behavior."""


def setup_logging(
    project: str = "maestro",
    *,
    level: str | None = None,
    log_dir: Path | None = None,
) -> None:
    """Initialize obs logging and route stdlib records into it.

    Calls ``obs.init_logging`` and attaches to the root logger:
    an :class:`ObsBridgeHandler` (all levels, filtered by the root level)
    and a stderr handler for WARNING+ so operator-visible warnings keep
    appearing on the console. Idempotent: re-running replaces previously
    attached bridge handlers instead of duplicating them.
    """
    obs.init_logging(project, level=level, log_dir=log_dir)

    root = logging.getLogger()
    for handler in root.handlers[:]:
        if isinstance(handler, (ObsBridgeHandler, _BridgeStderrHandler)):
            root.removeHandler(handler)

    root.addHandler(ObsBridgeHandler())

    stderr_handler = _BridgeStderrHandler()
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(
        logging.Formatter("%(levelname)s:%(name)s:%(message)s")
    )
    root.addHandler(stderr_handler)

    level_name = (
        level or os.environ.get("ORCHESTRA_LOG_LEVEL") or "info"
    ).lower()
    root.setLevel(_STDLIB_LEVELS.get(level_name, logging.INFO))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_logging_bridge.py -v`
Expected: 5 PASS. If `test_exception_info_is_captured` fails on redaction (the redact processor walks dicts; `stacktrace`/`type`/`message` are not in the redact key set) inspect the actual record with `print` — but per `obs.py:74-85` none of these keys are redacted.

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format maestro/logging_bridge.py tests/test_logging_bridge.py
uv run ruff check maestro/logging_bridge.py --fix
uv run pyrefly check
git add maestro/logging_bridge.py tests/test_logging_bridge.py
git commit -m "feat(obs): bridge stdlib logging into the obs JSONL pipeline"
```

---

### Task 2: Wire the CLI entry points

**Files:**
- Modify: `maestro/cli.py:43` (import), `maestro/cli.py:826`, `maestro/cli.py:1110`, `maestro/cli.py:1441` (call sites)

**Interfaces:**
- Consumes: `setup_logging(project)` from Task 1.
- Produces: every maestro CLI entry point now routes stdlib logs to JSONL.

- [ ] **Step 1: Replace the import**

`maestro/cli.py:43` currently reads:

```python
from maestro._vendor.obs import init_logging
```

Replace with:

```python
from maestro.logging_bridge import setup_logging
```

(If the line imports more names than `init_logging`, keep the others and remove only `init_logging`.)

- [ ] **Step 2: Replace the three call sites**

Each of `cli.py:826`, `cli.py:1110`, `cli.py:1441` reads:

```python
    init_logging("maestro")
```

Replace each with:

```python
    setup_logging("maestro")
```

Verify no call sites remain: `grep -n "init_logging" maestro/cli.py` → no matches (and `grep -rn "init_logging" maestro/ --include="*.py" | grep -v _vendor` shows no other callers).

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest tests/ -q`
Expected: PASS (same pass/fail set as `master`; the scheduler observability tests must stay green).

- [ ] **Step 4: Smoke test**

```bash
uv run maestro --help
ORCHESTRA_LOG_DIR=/tmp/maestro-bridge-smoke uv run maestro status 2>/dev/null || true
ls /tmp/maestro-bridge-smoke/ 2>/dev/null || echo "no log dir (command may not init logging) — OK if --help works"
```

Expected: `--help` prints usage without tracebacks; if the invoked command passes through an `setup_logging` call site, a `maestro-<pid>.jsonl` appears under the smoke dir.

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format maestro/cli.py && uv run ruff check maestro/cli.py --fix
uv run pyrefly check
git add maestro/cli.py
git commit -m "feat(cli): use setup_logging bridge at all init_logging call sites"
```

---

### Task 3: TODO.md record + PR

**Files:**
- Modify: `TODO.md` (observability block, after the `M3 (runtime-decision instrumentation)` line at `TODO.md:127`)

- [ ] **Step 1: Record the closed item**

Insert after the M3 runtime-decision line in the observability block:

```markdown
- [x] **M-obs stdlib bridge** (2026-07-19): все stdlib `logging` вызовы (~93 call-sites в ~16 модулях) маршрутизируются в obs OTel JSONL через `maestro/logging_bridge.py` (`ObsBridgeHandler` + `setup_logging` в cli.py); WARNING+ дублируются в stderr (замена lastResort). Vendored `_vendor/obs.py` не тронут.
```

- [ ] **Step 2: Full verification**

```bash
uv run pytest tests/ -q
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
```

Expected: suite green (same set as master), no lint/type errors.

- [ ] **Step 3: Commit, push, PR**

```bash
git add TODO.md
git commit -m "docs: record stdlib->obs bridge in TODO observability block"
git push -u origin feat/stdlib-obs-bridge
gh pr create --title "obs: route stdlib logging into the OTel JSONL pipeline" --body "..."
```

PR body must mention: bridge handler design (no changes to 93 call sites, vendored obs untouched), stderr WARNING+ passthrough preserving lastResort behavior, per-emit `obs.get_logger` proxies making re-init safe, and that human merges per umbrella policy. Track Copilot review.

---

## Self-Review (completed at plan time)

- Coverage: bridge (T1), CLI wiring (T2), docs/PR (T3). Out of scope by design: migrating call sites to structured events (follow-up, per-module, optional), console formatting beyond `LEVEL:name:message`, dashboard visualization (separate TODO item M3-dashboards).
- Known trade-off: `obs.get_logger` per emit creates a lazy proxy per record — negligible at maestro's log volume, and it guarantees re-init safety in tests.
- Type consistency: `setup_logging(project, *, level, log_dir)` used identically in T1 tests and T2; handler class names consistent.
