#!/usr/bin/env python3
"""Refuse a migration unless its pre-migration backup is usable.

``backup_database_before_migration.sh`` reports the path it wrote; this check
proves the artifact behind that path before ``alembic upgrade`` runs:

* it is a regular, non-empty file created for this run (fresh);
* it and its directory are private (no group/other access to the file, no
  group/other write on the directory), so a credential-bearing dump is not
  exposed and cannot be swapped;
* its format is what the database kind produces: a pg_dump custom archive
  whose table of contents ``pg_restore --list`` can read (header only when no
  pg_restore is installed), or an SQLite database that passes
  ``integrity_check``.

Every failure prints one ``BACKUP CHECK FAILED: <reason>`` line and exits 2,
which stops ``run_migrations.sh`` (``set -e``) before any schema change.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import stat
# Fixed pg_restore argv, no shell.
import subprocess  # nosec B404
import sys
import time
from pathlib import Path


PG_CUSTOM_MAGIC = b"PGDMP"
SQLITE_MAGIC = b"SQLite format 3\x00"
DEFAULT_MAX_AGE_SECONDS = 3600


def find_pg_restore() -> str | None:
    """Same lookup order as the backup script uses for pg_dump."""
    configured = os.environ.get("PG_RESTORE_BIN", "").strip()
    if configured:
        return configured
    if os.access("/usr/pgsql-18/bin/pg_restore", os.X_OK):
        return "/usr/pgsql-18/bin/pg_restore"
    return shutil.which("pg_restore")


def pg_archive_problems(path: Path, pg_restore: str | None) -> list[str]:
    if not pg_restore:
        return []
    completed = subprocess.run(  # nosec B603
        [pg_restore, "--list", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=300,
    )
    if completed.returncode:
        detail = (completed.stderr or "").strip().splitlines()
        return [f"pg_restore cannot read the archive: {detail[-1] if detail else completed.returncode}"]
    return []


def _metadata_problems(path: Path, info: os.stat_result, *, max_age_seconds: int, now: float) -> list[str]:
    problems = []
    if info.st_size == 0:
        problems.append("backup file is empty")
    if info.st_mode & 0o077:
        problems.append(f"backup file mode {stat.S_IMODE(info.st_mode):04o} allows group/other access (expected 0600)")
    directory = path.parent.stat()
    if directory.st_mode & 0o022:
        problems.append(f"backup directory mode {stat.S_IMODE(directory.st_mode):04o} is group/other writable")
    age = now - info.st_mtime
    if age > max_age_seconds:
        problems.append(f"backup is {int(age)}s old; expected one created for this migration")
    return problems


def _sqlite_problems(path: Path) -> list[str]:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return [f"SQLite backup cannot be opened: {exc}"]
    if not result or result[0] != "ok":
        return [f"SQLite backup failed integrity_check: {result[0] if result else 'no result'}"]
    return []


def _format_problems(path: Path, *, empty: bool, pg_restore: str | None) -> list[str]:
    try:
        with path.open("rb") as handle:
            header = handle.read(len(SQLITE_MAGIC))
    except PermissionError:
        return ["backup file is not readable by the migration user"]
    if header.startswith(PG_CUSTOM_MAGIC):
        return pg_archive_problems(path, pg_restore)
    if header == SQLITE_MAGIC:
        return _sqlite_problems(path)
    return [] if empty else ["backup is neither a pg_dump custom archive nor an SQLite database"]


def check_backup(
    path: Path,
    *,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: float | None = None,
    pg_restore: str | None = None,
) -> list[str]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return [f"backup file does not exist: {path}"]
    except PermissionError:
        return [f"backup file is not accessible: {path}"]
    if not stat.S_ISREG(info.st_mode):
        return [f"backup path is not a regular file: {path}"]
    problems = _metadata_problems(
        path, info, max_age_seconds=max_age_seconds, now=time.time() if now is None else now
    )
    return problems + _format_problems(path, empty=info.st_size == 0, pg_restore=pg_restore)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path)
    parser.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    args = parser.parse_args(argv)
    problems = check_backup(args.path, max_age_seconds=args.max_age_seconds, pg_restore=find_pg_restore())
    if problems:
        for problem in problems:
            print(f"BACKUP CHECK FAILED: {problem}", file=sys.stderr)
        return 2
    print(f"Verified migration backup: {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
