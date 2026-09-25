from pathlib import Path


def test_staging_rehearsal_records_revision_without_mixing_logging_stderr():
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts/deploy/staging_migration_rehearsal.sh"
    ).read_text(encoding="utf-8")
    assert "2>&1 | tail -n 1" not in source
    assert "BASELINE_EMPTY" in source
    assert "pg_restore_bin" in source
    assert "restore_validation" in source
