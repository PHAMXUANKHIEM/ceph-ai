"""Story 9.7, Task 2/5 — `restore_cluster_from_backup`'s 3 new phases
(`_phase_restore_metadata`, `_phase_restore_rbd_images`,
`_phase_verify_integrity`) plus the full `_PHASES_BY_ACTION_ID` entry that
reuses `deploy_cluster_ceph_deploy`'s phase list unchanged. Same
fake-SSH-via-`execute_command`-mock pattern as `tests/test_cluster_deploy.py`
(that file's own docstring/precedent — kept self-contained here rather
than importing its private helpers, same posture `test_restore_drill.py`
already established relative to `test_backup_engine.py`)."""

import base64
import copy
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

import worker.executor.cluster_deploy as cluster_deploy_module
from worker.backup.restore import RestoreResult
from worker.executor.cluster_deploy import run
from worker.policy.gate import VALID_CLUSTER_DEPLOY_ACTION_IDS

_ROCKY_OS_RELEASE = 'ID="rocky"\nVERSION_ID="9.3"\nPRETTY_NAME="Rocky Linux 9.3"\n'

_NODES = [
    {"ip": "10.20.1.112", "roles": ["mon", "mgr"]},
    {"ip": "10.20.1.95", "roles": ["mon", "mgr", "osd"], "osd_disk": "/dev/vdc"},
    {"ip": "10.20.1.21", "roles": ["mon", "osd"], "osd_disk": "/dev/vdb"},
]


def _restore_params(**overrides):
    params = {"version": "18.2.8", "nodes": copy.deepcopy(_NODES)}
    params.update(overrides)
    return params


def _make_recording_progress_writer():
    calls = []

    def write_progress(action_pk, progress):
        calls.append((action_pk, copy.deepcopy(progress)))

    return write_progress, calls


def _never_blocked(incident_id):
    return False


def _default_fake_execute(host, command):
    if command == "true":
        return ""
    if command == "cat /etc/os-release":
        return _ROCKY_OS_RELEASE
    if "blkid" in command:
        return ""
    if "lsblk" in command:
        return ""
    if command.startswith("hostname"):
        return host.replace(".", "-") + ".lab"
    if "ceph -s --format json" in command:
        return json.dumps({"health": {"status": "HEALTH_OK"}})
    if command.startswith("base64 "):
        return base64.b64encode(b"fake-binary-content").decode()
    if "quorum_status" in command:
        mon_hostnames = [n["ip"].replace(".", "-") + ".lab" for n in _NODES if "mon" in n["roles"]]
        return json.dumps({"quorum_names": mon_hostnames})
    if "ceph osd pool ls" in command:
        return ""  # no pre-existing pools
    if "rbd info" in command and "--format json" in command:
        return json.dumps({"size": RESTORED_IMAGE_SIZE_BYTES})
    return ""


RESTORED_IMAGE_SIZE_BYTES = 42_949_672_960  # 40 GiB, arbitrary


class FakeStorageBackend:
    def download(self, remote_key, dest):
        dest.write(b"fake metadata artifact bytes")

    def verify(self, remote_key, expected_size, expected_sha256):
        return True


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(cluster_deploy_module, "execute_command", _default_fake_execute)
    monkeypatch.setattr(cluster_deploy_module, "_QUORUM_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(cluster_deploy_module, "get_backend", lambda slot, settings: FakeStorageBackend())
    monkeypatch.setattr(
        cluster_deploy_module,
        "load_backup_policy",
        lambda: {"tracked_images": [{"pool": "vms", "image": "web01"}]},
    )
    monkeypatch.setattr(
        cluster_deploy_module.backup_metadata,
        "latest_successful_metadata_job",
        lambda: SimpleNamespace(backup_target_slot="a", remote_key="metadata/20260731T000000Z"),
    )
    monkeypatch.setattr(
        cluster_deploy_module.backup_restore, "latest_backup_target_slot", lambda pool, image: "a"
    )
    monkeypatch.setattr(
        cluster_deploy_module.backup_restore,
        "latest_full_backup_job",
        lambda pool, image: SimpleNamespace(id="full-1", size_bytes=RESTORED_IMAGE_SIZE_BYTES),
    )
    monkeypatch.setattr(
        cluster_deploy_module.backup_restore,
        "restore_image",
        lambda pool, image, storage, dest_pool, dest_image: RestoreResult(
            success=True, full_job_id="full-1", applied_diff_job_ids=[], size_bytes=RESTORED_IMAGE_SIZE_BYTES
        ),
    )
    written_fields = {}
    monkeypatch.setattr(cluster_deploy_module.env_config, "update_env_file_batch", written_fields.update)
    yield SimpleNamespace(written_fields=written_fields)


def test_restore_cluster_from_backup_registered_in_policy():
    assert "restore_cluster_from_backup" in VALID_CLUSTER_DEPLOY_ACTION_IDS


def test_restore_cluster_from_backup_phase_list_reuses_ceph_deploy_then_appends_three():
    ceph_deploy_keys = [k for k, _l, _p, _f in cluster_deploy_module._PHASES_BY_ACTION_ID["deploy_cluster_ceph_deploy"]]
    restore_keys = [k for k, _l, _p, _f in cluster_deploy_module._PHASES_BY_ACTION_ID["restore_cluster_from_backup"]]
    assert restore_keys == ceph_deploy_keys + ["restore_metadata", "restore_rbd_images", "verify_integrity"]


def test_restore_cluster_from_backup_happy_path_runs_every_phase(fakes):
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "restore_cluster_from_backup", _restore_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    final_progress = calls[-1][1]
    assert [step["step"] for step in final_progress] == [
        "ssh_check",
        "dependencies",
        "repo",
        "packages",
        "mon_init",
        "wait_quorum",
        "mon_security",
        "mgr_create",
        "osd_create",
        "verify",
        "restore_metadata",
        "restore_rbd_images",
        "verify_integrity",
    ]
    assert all(step["status"] == "done" for step in final_progress)
    assert fakes.written_fields["CEPH_EXEC_MODE"] == "none"


def test_restore_metadata_fails_clearly_when_no_metadata_backup_exists(monkeypatch, fakes):
    monkeypatch.setattr(cluster_deploy_module.backup_metadata, "latest_successful_metadata_job", lambda: None)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "restore_cluster_from_backup", _restore_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    final_progress = calls[-1][1]
    restore_metadata_step = next(s for s in final_progress if s["step"] == "restore_metadata")
    assert restore_metadata_step["status"] == "failed"
    assert "metadata" in restore_metadata_step["message"]
    # Later phases in the SAME action never ran.
    rbd_images_step = next(s for s in final_progress if s["step"] == "restore_rbd_images")
    assert rbd_images_step["status"] == "pending"


def test_restore_metadata_monmap_inject_failure_is_non_fatal(monkeypatch, fakes):
    """The monmap `--inject-monmap` sub-step is documented best-effort
    (fsid mismatch risk, see `_restore_monmap_on_mon`'s own docstring) — a
    failure there must be logged, not turn the whole phase (or action)
    into a failure, since auth/CRUSH map already succeeded."""

    def flaky_execute(host, command):
        if "inject-monmap" in command:
            raise cluster_deploy_module.ExecutorError("simulated fsid mismatch")
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", flaky_execute)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "restore_cluster_from_backup", _restore_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    final_progress = calls[-1][1]
    assert next(s for s in final_progress if s["step"] == "restore_metadata")["status"] == "done"


def test_restore_rbd_images_creates_missing_pool_before_restoring(monkeypatch, fakes):
    seen_commands = []

    def recording_execute(host, command):
        seen_commands.append(command)
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", recording_execute)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "restore_cluster_from_backup", _restore_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    assert any("ceph osd pool create vms" in c and "rbd pool init vms" in c for c in seen_commands)


def test_restore_rbd_images_skips_pool_create_when_pool_already_exists(monkeypatch, fakes):
    def fake_execute(host, command):
        if "ceph osd pool ls" in command:
            return "vms\nother_pool"
        if "pool create" in command:
            raise AssertionError("must not create a pool that already exists")
        return _default_fake_execute(host, command)

    monkeypatch.setattr(cluster_deploy_module, "execute_command", fake_execute)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "restore_cluster_from_backup", _restore_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True


def test_restore_rbd_images_fails_when_no_full_backup_for_tracked_image(monkeypatch, fakes):
    monkeypatch.setattr(cluster_deploy_module.backup_restore, "latest_backup_target_slot", lambda pool, image: None)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "restore_cluster_from_backup", _restore_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    final_progress = calls[-1][1]
    step = next(s for s in final_progress if s["step"] == "restore_rbd_images")
    assert step["status"] == "failed"
    assert "vms/web01" in step["message"]


def test_restore_rbd_images_fails_when_restore_image_reports_failure(monkeypatch, fakes):
    monkeypatch.setattr(
        cluster_deploy_module.backup_restore,
        "restore_image",
        lambda pool, image, storage, dest_pool, dest_image: RestoreResult(
            success=False, error_message="rbd import exited 1: no space left on device"
        ),
    )
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "restore_cluster_from_backup", _restore_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    final_progress = calls[-1][1]
    step = next(s for s in final_progress if s["step"] == "restore_rbd_images")
    assert step["status"] == "failed"
    assert "no space left on device" in step["message"]


def test_verify_integrity_fails_on_size_mismatch(monkeypatch, fakes):
    monkeypatch.setattr(
        cluster_deploy_module.backup_restore,
        "latest_full_backup_job",
        lambda pool, image: SimpleNamespace(id="full-1", size_bytes=RESTORED_IMAGE_SIZE_BYTES + 1),
    )
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "restore_cluster_from_backup", _restore_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    final_progress = calls[-1][1]
    step = next(s for s in final_progress if s["step"] == "verify_integrity")
    assert step["status"] == "failed"
    assert "checksum" in step["message"].lower() or "kích thước" in step["message"]


def test_verify_integrity_fails_when_no_full_backup_metadata_to_compare(monkeypatch, fakes):
    monkeypatch.setattr(cluster_deploy_module.backup_restore, "latest_full_backup_job", lambda pool, image: None)
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "restore_cluster_from_backup", _restore_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is False
    final_progress = calls[-1][1]
    assert next(s for s in final_progress if s["step"] == "verify_integrity")["status"] == "failed"


def test_restore_phases_are_skipped_cleanly_when_no_tracked_images_configured(monkeypatch, fakes):
    monkeypatch.setattr(cluster_deploy_module, "load_backup_policy", lambda: {"tracked_images": []})
    write_progress, calls = _make_recording_progress_writer()

    result = run(
        "action-1", "restore_cluster_from_backup", _restore_params(), "incident-1", write_progress, _never_blocked
    )

    assert result is True
    final_progress = calls[-1][1]
    assert next(s for s in final_progress if s["step"] == "restore_rbd_images")["status"] == "done"
    assert next(s for s in final_progress if s["step"] == "verify_integrity")["status"] == "done"
