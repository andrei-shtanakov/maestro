import json

from maestro.execution.ssh_launch import (
    build_descriptor,
    encode_transport_ref,
    remote_layout,
)


def test_remote_layout_paths_are_under_tmp():
    lay = remote_layout("/var/tmp/maestro", "e1")
    assert lay.root == "/var/tmp/maestro/maestro-exec-e1"
    assert lay.repo == "/var/tmp/maestro/maestro-exec-e1/repo"
    assert lay.status.endswith("/e1.status")
    assert lay.owner_marker.endswith("/.maestro-owner")


def test_descriptor_carries_argv_verbatim():
    lay = remote_layout("/w", "e1")
    d = build_descriptor("e1", lay, ["spec-runner", "run", "--all"], "/w")
    assert d["v"] == 1
    assert d["argv"] == ["spec-runner", "run", "--all"]
    assert d["cwd"] == lay.repo
    # round-trips as JSON
    assert json.loads(json.dumps(d))["execution_id"] == "e1"


def test_transport_ref_is_opaque_versioned_json():
    ref = encode_transport_ref(
        "gpu", 2222, "/w/maestro-exec-e1", "/w/maestro-exec-e1/e1.status"
    )
    obj = json.loads(ref)
    assert obj["v"] == 1 and obj["transport"] == "ssh" and obj["host"] == "gpu"
