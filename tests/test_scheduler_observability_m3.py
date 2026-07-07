"""Observability M3 — scheduler.tick (emit-on-change) and task.route span.

Mirrors tests/test_scheduler_observability.py: the scheduler binds `_obs_log`
at import, so we reload obs into a tmp ORCHESTRA_LOG_DIR and rebind the
scheduler module's `_obs_log` to a fresh logger; `obs.span` uses the reloaded
module directly.
"""

from __future__ import annotations

import importlib
import json
from typing import TYPE_CHECKING

from maestro.dag import DAG


# NOTE: SchedulerConfig lives in maestro.scheduler (not maestro.models); the
# tests build it from the reloaded module as sched_mod.SchedulerConfig(...).

if TYPE_CHECKING:
    from pathlib import Path


def _reload_obs_and_scheduler(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import maestro._vendor.obs as obs

    importlib.reload(obs)
    obs.init_logging("maestro")
    import maestro.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_obs_log", obs.get_logger("maestro.scheduler"))
    return obs, sched_mod


def _read_records(tmp_path: Path) -> list[dict]:
    files = list(tmp_path.glob("maestro-*.jsonl"))
    assert len(files) == 1, f"expected 1 jsonl file, got {len(files)}: {files}"
    return [json.loads(line) for line in files[0].read_text().splitlines()]


def _make_scheduler(sched_mod, tmp_path):
    return sched_mod.Scheduler(
        db=object(),  # _emit_tick / _route_task never touch db
        dag=DAG([]),
        spawners={},
        config=sched_mod.SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
    )


def _ticks(tmp_path: Path) -> list[dict]:
    return [
        r
        for r in _read_records(tmp_path)
        if r["Attributes"].get("event") == "scheduler.tick"
    ]


def test_emit_tick_first_and_on_change_only(tmp_path, monkeypatch) -> None:
    _obs, sched_mod = _reload_obs_and_scheduler(monkeypatch, tmp_path)
    sched = _make_scheduler(sched_mod, tmp_path)

    sched._emit_tick(2, 1, 0)  # first → emits
    sched._emit_tick(2, 1, 0)  # identical → skipped
    sched._emit_tick(1, 2, 0)  # changed → emits

    ticks = _ticks(tmp_path)
    assert len(ticks) == 2
    assert ticks[0]["Attributes"]["ready"] == 2
    assert ticks[0]["Attributes"]["running"] == 1
    assert ticks[0]["Attributes"]["completed"] == 0
    assert ticks[1]["Attributes"]["ready"] == 1
    assert ticks[1]["Attributes"]["running"] == 2
    assert ticks[0]["Resource"]["service.name"] == "maestro"
