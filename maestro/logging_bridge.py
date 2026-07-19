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
                    "stacktrace": "".join(traceback.format_exception(*record.exc_info)),
                }
            log = obs.get_logger(record.name)
            getattr(log, _method_for_level(record.levelno))("log.stdlib", **kwargs)
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
    stderr_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root.addHandler(stderr_handler)

    level_name = (level or os.environ.get("ORCHESTRA_LOG_LEVEL") or "info").lower()
    root.setLevel(_STDLIB_LEVELS.get(level_name, logging.INFO))
