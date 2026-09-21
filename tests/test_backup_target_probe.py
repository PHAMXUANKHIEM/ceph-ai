from types import SimpleNamespace

from worker.backup import target_probe
from worker.backup.storage.base import UploadResult


def _settings(**overrides):
    values = {
        "backup_target_a_transport": "s3",
        "backup_target_a_label": "Primary",
        "backup_target_a_s3_endpoint": "https://minio.example.test:9000",
        "backup_target_a_s3_access_key": "AKIA-SECRET",
        "backup_target_a_s3_secret_key": "very-secret",
        "backup_target_a_s3_bucket": "backup-a",
        "backup_target_a_ssh_key_path": "/root/.ssh/backup-a",
        "backup_target_b_transport": "",
        "backup_target_b_s3_endpoint": "",
        "backup_target_b_s3_bucket": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeBackend:
    def __init__(self, *, verify=True, fail_delete=False):
        self.verify_result = verify
        self.fail_delete = fail_delete
        self.deleted = []
        self.payload = b""

    def probe_metadata(self):
        return {"capacity_bytes": 1000, "free_bytes": 900, "object_lock_enabled": False}

    def upload(self, stream, remote_key):
        payload = stream.read()
        self.payload = payload
        import hashlib
        return UploadResult(remote_key, len(payload), hashlib.sha256(payload).hexdigest())

    def verify(self, remote_key, expected_size, expected_sha256):
        return self.verify_result

    def download(self, remote_key, dest):
        dest.write(self.payload)

    def delete(self, remote_key):
        if self.fail_delete:
            raise RuntimeError("secret=very-secret delete denied")
        self.deleted.append(remote_key)


def test_probe_runs_write_verify_download_and_cleanup(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(target_probe, "get_backend", lambda *args, **kwargs: backend)

    result = target_probe.probe_target("a", _settings())

    assert result["status"] == "passed"
    assert result["cleanup"] == {"status": "passed"}
    assert [step["name"] for step in result["steps"]] == [
        "connection_and_metadata", "write_probe_object", "stat_and_verify",
        "download_probe_object", "delete_probe_object",
    ]
    assert backend.deleted
    assert "very-secret" not in str(result)


def test_probe_redacts_cleanup_failure_and_warns_duplicate_target(monkeypatch):
    backend = FakeBackend(verify=False, fail_delete=True)
    monkeypatch.setattr(target_probe, "get_backend", lambda *args, **kwargs: backend)
    settings = _settings(
        backup_target_b_transport="s3",
        backup_target_b_s3_endpoint="https://minio.example.test:9000",
        backup_target_b_s3_bucket="backup-a",
    )

    result = target_probe.probe_target("a", settings)

    assert result["status"] == "failed"
    assert result["warnings"] == ["SLOT_B_TRUNG_CÙNG_TARGET"]
    assert result["cleanup"]["status"] == "failed"
    assert result["cleanup"]["key"].startswith("[probe artifact")
    assert "very-secret" not in str(result)


def test_probe_does_not_touch_unconfigured_target(monkeypatch):
    called = []
    monkeypatch.setattr(target_probe, "get_backend", lambda *args, **kwargs: called.append(True))

    result = target_probe.probe_target("a", _settings(backup_target_a_transport=""))

    assert result["status"] == "failed"
    assert result["steps"][0]["name"] == "configuration"
    assert called == []
