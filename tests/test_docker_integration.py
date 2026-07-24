"""Opt-in end-to-end tests for the Docker execution backend.

These exercise the FULL stack (`BackendResolver` -> `LocalBackend(DockerIsolator)`
-> `DockerTaskHandle`) against a real docker daemon. They auto-skip (never
fail/error) when docker is unavailable.

Two independent gates keep collection cheap and daemon-free:
  - `skip_no_docker` (collection-time, via `@pytest.mark.skipif`) only checks
    `shutil.which("docker")` — a plain PATH lookup, no subprocess. A machine
    without the docker CLI skips here with zero shelling out at collection.
  - `_require_daemon()` (test-execution-time, called as the first line of
    each test body) runs `docker version` and calls `pytest.skip(...)` if it
    fails. A machine with the CLI but no running daemon skips here instead,
    after collection has already completed.

Every OTHER setup call that shells out to docker (image pull, network
create) lives inside a test body gated by both of the above, so a skipped
run never touches docker beyond the single `docker version` daemon probe.

Network test note: the only network reachability asserted here is between
two containers on a locally created docker network — never the public
internet.
"""

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from maestro.execution.backend import ExecutionBackend
from maestro.execution.exec_config import DockerConfig
from maestro.execution.finalize import finalize_handle
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.execution.resolver import BackendResolver, ExecutionConfig


pytestmark = pytest.mark.anyio

IMAGE = "python:3.12-slim"  # a small public image with python; no agent CLIs needed


skip_no_docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker CLI not installed"
)


def _require_daemon() -> None:
    """Skip the current test if the docker CLI can't reach a daemon.

    Cheap collection-time gating (`skip_no_docker`) only confirms the CLI
    binary exists; this confirms the daemon actually answers, and must be
    called at test-execution time (never at import/collection time) so a
    CLI-present-but-daemon-down machine skips per-test instead of hanging
    or failing collection.
    """
    result = subprocess.run(["docker", "version"], capture_output=True)
    if result.returncode != 0:
        pytest.skip("docker daemon unavailable")


def _pull_image() -> None:
    """Pull IMAGE; a cheap no-op when it's already present locally."""
    subprocess.run(["docker", "pull", IMAGE], check=True, capture_output=True)


def _ps_by_execution_id(execution_id: str) -> str:
    """Return `docker ps -a` stdout filtered to a maestro.execution_id label."""
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=maestro.execution_id={execution_id}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _resolve_docker_backend(image: str, network: str) -> ExecutionBackend:
    """Build a BackendResolver's docker backend for the given image/network."""
    resolver = BackendResolver(
        ExecutionConfig(docker=DockerConfig(image=image, network=network))
    )
    return resolver.resolve("docker")


def _create_network(network_name: str) -> None:
    """Create a local docker network for the network-isolation test."""
    subprocess.run(
        ["docker", "network", "create", network_name], check=True, capture_output=True
    )


def _run_server_container(network_name: str, server_name: str) -> None:
    """Start a detached sibling container on network_name serving on :8000."""
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--network",
            network_name,
            "--name",
            server_name,
            IMAGE,
            "python",
            "-m",
            "http.server",
            "8000",
        ],
        check=True,
        capture_output=True,
    )


def _cleanup_network(network_name: str, server_name: str) -> None:
    """Best-effort teardown of the sibling container and its network."""
    subprocess.run(
        ["docker", "rm", "-f", server_name], capture_output=True, check=False
    )
    subprocess.run(
        ["docker", "network", "rm", network_name], capture_output=True, check=False
    )


@skip_no_docker
async def test_bind_mount_collect_is_noop_file_visible_on_host(
    tmp_path: Path,
) -> None:
    """A file written under /work is visible on the host even though collect()
    is a no-op, because the workdir is bind-mounted (not copied) into the
    container.
    """
    _require_daemon()
    _pull_image()
    wd = tmp_path / "wd"
    wd.mkdir()
    backend = _resolve_docker_backend(IMAGE, "none")
    req = ExecutionRequest(
        run_id="t1",
        execution_id="itest-1",
        entity_kind="task",
        attempt=1,
        backend_id="docker",
        argv=["python", "-c", "open('/work/out.txt','w').write('from-container')"],
        workdir=wd,
        log_path=wd / "log",
        collect=CollectPolicy(mode="none"),
    )
    handle = await backend.run(req)
    fin = await finalize_handle(handle)
    assert fin.execution.exit_code == 0
    # collect is a no-op, yet the file exists on the host (bind mount)
    assert (wd / "out.txt").read_text() == "from-container"
    assert fin.cleaned is True


@skip_no_docker
async def test_success_leaves_no_container(tmp_path: Path) -> None:
    """A successful run's container is fully removed by finalize's cleanup."""
    _require_daemon()
    _pull_image()
    wd = tmp_path / "wd"
    wd.mkdir()
    backend = _resolve_docker_backend(IMAGE, "none")
    req = ExecutionRequest(
        run_id="t2",
        execution_id="itest-2",
        entity_kind="task",
        attempt=1,
        backend_id="docker",
        argv=["python", "-c", "print('ok')"],
        workdir=wd,
        log_path=wd / "log",
        collect=CollectPolicy(mode="none"),
    )
    handle = await backend.run(req)
    fin = await finalize_handle(handle)
    assert fin.execution.exit_code == 0
    assert fin.cleaned is True
    assert _ps_by_execution_id("itest-2") == ""


@skip_no_docker
async def test_timeout_kills_and_removes_container(tmp_path: Path) -> None:
    """A run that exceeds timeout_seconds is reported as timed out, and its
    container is stopped/removed with no leftover after finalize.
    """
    _require_daemon()
    _pull_image()
    wd = tmp_path / "wd"
    wd.mkdir()
    backend = _resolve_docker_backend(IMAGE, "none")
    req = ExecutionRequest(
        run_id="t3",
        execution_id="itest-3",
        entity_kind="task",
        attempt=1,
        backend_id="docker",
        argv=["python", "-c", "import time; time.sleep(30)"],
        workdir=wd,
        log_path=wd / "log",
        collect=CollectPolicy(mode="none"),
        timeout_seconds=2.0,
    )
    handle = await backend.run(req)
    result = await handle.wait()
    assert result.timed_out is True
    fin = await finalize_handle(handle)
    assert fin.cleaned is True
    assert _ps_by_execution_id("itest-3") == ""


@skip_no_docker
async def test_opt_in_network_via_local_docker_network(tmp_path: Path) -> None:
    """A container run through the docker backend on a locally created
    network can reach ANOTHER container attached to that same local
    network — this test never asserts reachability to the public internet.
    """
    _require_daemon()
    _pull_image()
    suffix = uuid.uuid4().hex[:8]
    network_name = f"maestro-itest-net-{suffix}"
    server_name = f"maestro-itest-srv-{suffix}"
    execution_id = f"itest-4-{suffix}"

    _create_network(network_name)
    try:
        _run_server_container(network_name, server_name)

        wd = tmp_path / "wd"
        wd.mkdir()
        backend = _resolve_docker_backend(IMAGE, network_name)
        probe_script = (
            "import time, urllib.request\n"
            f"url = 'http://{server_name}:8000/'\n"
            "for _ in range(20):\n"
            "    try:\n"
            "        urllib.request.urlopen(url, timeout=2)\n"
            "        raise SystemExit(0)\n"
            "    except SystemExit:\n"
            "        raise\n"
            "    except Exception:\n"
            "        time.sleep(1)\n"
            "raise SystemExit(1)\n"
        )
        req = ExecutionRequest(
            run_id="t4",
            execution_id=execution_id,
            entity_kind="task",
            attempt=1,
            backend_id="docker",
            argv=["python", "-c", probe_script],
            workdir=wd,
            log_path=wd / "log",
            collect=CollectPolicy(mode="none"),
        )
        handle = await backend.run(req)
        fin = await finalize_handle(handle)
        assert fin.execution.exit_code == 0
        assert fin.cleaned is True
    finally:
        _cleanup_network(network_name, server_name)
