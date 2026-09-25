import importlib.util
import os
import sqlite3
import time
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "verify_migration_backup.py"
spec = importlib.util.spec_from_file_location("verify_migration_backup", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _private_dir(tmp_path):
    directory = tmp_path / "backups"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def _pg_dump(directory, mode=0o600):
    path = directory / "ceph-ai-20260925T000000Z.dump"
    path.write_bytes(b"PGDMP\x01\x0e\x00" + b"\x00" * 64)
    os.chmod(path, mode)
    return path


def test_fresh_private_pg_dump_passes(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(module, "find_pg_restore", lambda: None)
    path = _pg_dump(_private_dir(tmp_path))
    assert module.check_backup(path) == []
    assert module.main([str(path)]) == 0
    assert "Verified migration backup" in capsys.readouterr().out


def test_missing_backup_stops_migration(tmp_path, capsys):
    assert module.main([str(_private_dir(tmp_path) / "absent.dump")]) == 2
    assert "BACKUP CHECK FAILED: backup file does not exist" in capsys.readouterr().err


def test_exposed_file_and_writable_directory_are_rejected(tmp_path):
    directory = _private_dir(tmp_path)
    path = _pg_dump(directory, mode=0o644)
    os.chmod(directory, 0o777)
    problems = module.check_backup(path)
    assert any("mode 0644 allows group/other access" in item for item in problems)
    assert any("directory mode 0777 is group/other writable" in item for item in problems)


def test_empty_stale_or_foreign_files_are_rejected(tmp_path):
    directory = _private_dir(tmp_path)
    empty = directory / "empty.dump"
    empty.write_bytes(b"")
    os.chmod(empty, 0o600)
    assert module.check_backup(empty) == ["backup file is empty"]

    stale = _pg_dump(directory)
    old = time.time() - 7200
    os.utime(stale, (old, old))
    assert any("expected one created for this migration" in item for item in module.check_backup(stale))

    foreign = directory / "notes.dump"
    foreign.write_text("pg_dump: error: connection refused\n")
    os.chmod(foreign, 0o600)
    assert module.check_backup(foreign) == ["backup is neither a pg_dump custom archive nor an SQLite database"]


def test_sqlite_backup_is_integrity_checked(tmp_path):
    directory = _private_dir(tmp_path)
    good = directory / "ceph-ai.sqlite3"
    connection = sqlite3.connect(good)
    connection.execute("create table incidents (id text)")
    connection.commit()
    connection.close()
    os.chmod(good, 0o600)
    assert module.check_backup(good) == []

    corrupt = directory / "corrupt.sqlite3"
    corrupt.write_bytes(good.read_bytes()[:100] + b"\xff" * 4000)
    os.chmod(corrupt, 0o600)
    problems = module.check_backup(corrupt)
    assert problems and "SQLite backup" in problems[0]


def test_symlink_is_not_accepted_as_backup(tmp_path):
    directory = _private_dir(tmp_path)
    target = _pg_dump(directory)
    link = directory / "latest.dump"
    link.symlink_to(target)
    assert module.check_backup(link) == [f"backup path is not a regular file: {link}"]


def test_pg_archive_is_listed_with_pg_restore(tmp_path):
    directory = _private_dir(tmp_path)
    path = _pg_dump(directory)
    ok = directory / "pg_restore_ok"
    ok.write_text("#!/bin/sh\nexit 0\n")
    ok.chmod(0o755)
    broken = directory / "pg_restore_broken"
    broken.write_text("#!/bin/sh\necho 'pg_restore: error: could not read input file' >&2\nexit 1\n")
    broken.chmod(0o755)
    assert module.check_backup(path, pg_restore=str(ok)) == []
    assert module.check_backup(path, pg_restore=str(broken)) == [
        "pg_restore cannot read the archive: pg_restore: error: could not read input file"
    ]
