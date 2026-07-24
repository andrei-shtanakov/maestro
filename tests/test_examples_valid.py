"""Targeted example-config tests that assert on resolved values, not just
that the config loads. Complements the generic schema-drift smoke in
test_examples_smoke.py.
"""

from __future__ import annotations

from maestro.config import load_orchestrator_config


def test_with_ssh_example_loads_and_resolves() -> None:
    cfg = load_orchestrator_config("examples/with-ssh.yaml")
    assert cfg.execution is not None
    reg = cfg.execution.normalized()
    assert reg["gpu-box"].transport.type == "ssh"
    assert "spec-runner" not in reg  # sanity: only declared backends + local
