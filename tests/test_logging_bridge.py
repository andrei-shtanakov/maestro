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
        json.loads(line) for line in files[0].read_text().splitlines() if line.strip()
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
