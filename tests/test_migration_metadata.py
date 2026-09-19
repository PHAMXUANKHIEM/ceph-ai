import json

from scripts.deploy import record_migration_metadata


def test_metadata_writer_is_atomic_and_redacts_connection_details(tmp_path, monkeypatch):
    output = tmp_path / "release" / "migration.json"
    values = {
        "MIGRATION_METADATA_PATH": str(output),
        "MIGRATION_ENVIRONMENT": "production",
        "MIGRATION_GIT_COMMIT": "abc123",
        "MIGRATION_HEAD": "c8d9e0f1a2b4",
        "MIGRATION_REVISION_BEFORE": "b8c9d0e1f2a4",
        "MIGRATION_REVISION_AFTER": "c8d9e0f1a2b4",
        "MIGRATION_CHECKSUM_SHA256": "f" * 64,
        "MIGRATION_BACKUP_PATH": "/var/backups/ceph-ai/backup.dump",
        "DATABASE_URL": "postgresql+psycopg://user:secret@db.example/app",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    record_migration_metadata.main()

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["revision_before"] == "b8c9d0e1f2a4"
    assert payload["revision_after"] == "c8d9e0f1a2b4"
    assert "DATABASE_URL" not in output.read_text(encoding="utf-8")
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700
