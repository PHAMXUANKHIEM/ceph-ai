import json

from worker.executor import rbd_move


def _params():
    return {
        "pool_name": "vms",
        "image": "vm-old",
        "dest_pool": "images",
        "dest_image": "vm-new",
        "size_bytes": 1024,
        "delete_source": True,
        "move_token": "move-token-20260925-01",
        "checksum": "rbd-export-diff-sha256",
    }


def test_move_executor_persists_durable_phases(monkeypatch):
    writes = []
    monkeypatch.setattr(
        rbd_move,
        "execute_command",
        lambda *args, **kwargs: json.dumps({
            "name": "vm-new", "size": 1024,
            "source_deleted": True, "checksum_verified": True,
        }),
    )

    succeeded, command, output = rbd_move.run(
        "action-1", _params(), "10.0.0.1", "ceph", "/key", "none", "",
        lambda action_pk, progress: writes.append((action_pk, json.loads(json.dumps(progress)))),
    )

    assert succeeded is True
    assert command
    assert json.loads(output)["checksum_verified"] is True
    assert len(writes) == 4
    assert writes[0][1][0]["status"] == "running"
    assert writes[1][1][0]["status"] == "done"
    assert writes[1][1][1]["status"] == "running"
    assert writes[-1][1][-1]["status"] == "done"


def test_move_executor_records_failed_post_check(monkeypatch):
    writes = []
    monkeypatch.setattr(
        rbd_move,
        "execute_command",
        lambda *args, **kwargs: '{"name":"wrong","size":1024}',
    )

    succeeded, _command, _output = rbd_move.run(
        "action-2", _params(), "10.0.0.1", "ceph", "/key", "none", "",
        lambda action_pk, progress: writes.append((action_pk, json.loads(json.dumps(progress)))),
    )

    assert succeeded is False
    assert writes[-1][1][-1]["status"] == "failed"
    assert "destination" in writes[-1][1][-1]["error"]
