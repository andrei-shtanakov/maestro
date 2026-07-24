import json
import subprocess
import sys
import time
from pathlib import Path


SUP = Path("maestro/execution/resources/maestro_supervisor.py").resolve()


def _descriptor(tmp: Path, argv: list[str]) -> Path:
    root = tmp / "maestro-exec-e1"
    (root / "repo").mkdir(parents=True)
    d = {
        "v": 1,
        "execution_id": "e1",
        "cwd": str(root / "repo"),
        "argv": argv,
        "env_file": str(root / "env"),
        "workdir_root": str(tmp),
        "owner_marker": str(root / ".maestro-owner"),
        "pid_file": str(root / "e1.pid"),
        "status_file": str(root / "e1.status"),
        "log_file": str(root / "e1.log"),
    }
    dp = root / "descriptor.json"
    dp.write_text(json.dumps(d))
    return dp


def _wait_status(path: Path, timeout=10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.05)
    raise AssertionError("status marker never appeared")


def test_supervisor_handshake_and_atomic_status(tmp_path):
    dp = _descriptor(tmp_path, [sys.executable, "-c", "print('hi')"])
    # Launch returns quickly after the handshake (parent exits post-fork).
    out = subprocess.run(
        [sys.executable, str(SUP), str(dp)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.returncode == 0
    assert out.stdout.strip().startswith("MAESTRO-SUPERVISOR-READY e1")
    status = _wait_status(tmp_path / "maestro-exec-e1" / "e1.status")
    assert status["exit_code"] == 0
    assert (tmp_path / "maestro-exec-e1" / ".maestro-owner").read_text().strip() == "e1"


def test_supervisor_preserves_argv_boundaries(tmp_path):
    marker = tmp_path / "argmark.txt"
    argv = [
        sys.executable,
        "-c",
        "import sys; open(sys.argv[1],'w').write(sys.argv[2])",
        str(marker),
        'a b\t"c" $(x)',
    ]
    dp = _descriptor(tmp_path, argv)
    subprocess.run([sys.executable, str(SUP), str(dp)], timeout=10)
    _wait_status(tmp_path / "maestro-exec-e1" / "e1.status")
    assert marker.read_text() == 'a b\t"c" $(x)'
