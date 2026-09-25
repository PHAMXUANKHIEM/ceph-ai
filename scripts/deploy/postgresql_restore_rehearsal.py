#!/usr/bin/env python3
"""Restore a PostgreSQL backup into an explicitly isolated rehearsal target.

This command is intentionally strict: it never infers a staging target from
the application's default environment and refuses an identical source/target
connection. It produces evidence, but the evidence is not a production DR
approval by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
CONFIRMATION = "I_UNDERSTAND_STAGING_ONLY"
PRODUCTION_IDS = {"prod", "production", "live"}
CRITICAL_TABLES = ("alembic_version", "clusters", "incidents", "incident_outbox")
SECRET_RE = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)=([^&\s]+)")


class RehearsalError(RuntimeError):
    pass


def _redact(value: str) -> str:
    return SECRET_RE.sub(r"\1=[REDACTED]", value)


def _safe_url(value: str) -> tuple[str, str, int | None, str]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        raise RehearsalError("database URLs must use PostgreSQL")
    database = parsed.path.lstrip("/")
    if not database:
        raise RehearsalError("database URL must name a database")
    # Compare the endpoint/database, not credentials. Two URLs pointing to the
    # same database must be rejected even when they use different users.
    identity = f"{parsed.hostname.lower()}:{parsed.port or 5432}/{database}"
    return parsed.hostname.lower(), database, parsed.port, identity


def _command(name: str, override: str | None = None) -> str:
    value = override or shutil.which(name)
    if not value or not os.access(value, os.X_OK):
        raise RehearsalError(f"required command is unavailable: {name}")
    return value


def _run(command: list[str], *, env: dict[str, str], timeout: int) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RehearsalError(f"command failed to start or timed out: {command[0]}") from exc
    output = _redact((result.stdout or "") + (result.stderr or ""))[-4000:]
    if result.returncode != 0:
        raise RehearsalError(f"{command[0]} exited {result.returncode}: {output}")
    return output


def _write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def run_rehearsal(args: argparse.Namespace) -> dict:
    if args.confirm != CONFIRMATION:
        raise RehearsalError("staging confirmation token is incorrect")
    source_id = args.source_id.strip().lower()
    target_id = args.target_id.strip().lower()
    if not source_id or not target_id or target_id in PRODUCTION_IDS:
        raise RehearsalError("source and target IDs are required; target cannot be production")
    source_host, source_db, source_port, source_identity = _safe_url(args.source_url)
    target_host, target_db, target_port, target_identity = _safe_url(args.target_url)
    if source_identity == target_identity:
        raise RehearsalError("restore target must be different from the source database")

    pg_dump = _command("pg_dump", args.pg_dump_bin)
    pg_restore = _command("pg_restore", args.pg_restore_bin)
    psql = _command("psql", args.psql_bin)
    python = sys.executable
    alembic = [python, "-m", "alembic"]
    backup_dir = args.backup_dir.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    backup_path = backup_dir / f"ceph-ai-rehearsal-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.dump"
    started = time.monotonic()
    env = os.environ.copy()
    env.update({"CEPH_AI_ENV_FILE": "/dev/null", "CEPH_AI_ENVIRONMENT": "staging"})

    payload = {
        "schema": "ceph-ai.postgresql-restore-rehearsal.v1",
        "status": "FAILED",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source": {"id": source_id, "host": source_host, "port": source_port, "database": source_db},
        "target": {"id": target_id, "host": target_host, "port": target_port, "database": target_db},
        "failure_injection": args.inject_failure,
        "steps": [],
        "errors": [],
    }

    def step(name: str, status: str, detail: str = "") -> None:
        payload["steps"].append({"name": name, "status": status, "detail": detail[-1000:]})

    try:
        _run([pg_dump, "--format=custom", "--file", str(backup_path), args.source_url], env=env, timeout=args.timeout)
        checksum = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        payload["backup"] = {"path": str(backup_path), "sha256": checksum, "mode": oct(backup_path.stat().st_mode & 0o777)}
        step("backup", "PASS")
        _run([pg_restore, "--list", str(backup_path)], env=env, timeout=args.timeout)
        step("backup_validation", "PASS", "pg_restore --list")
        if args.inject_failure == "before_restore":
            raise RehearsalError("injected failure before restore")
        _run([
            pg_restore, "--exit-on-error", "--no-owner", "--no-privileges",
            "--clean", "--if-exists", "--dbname", args.target_url, str(backup_path),
        ], env=env, timeout=args.timeout)
        step("restore", "PASS")
        if args.inject_failure == "after_restore":
            raise RehearsalError("injected failure after restore")
        env["DATABASE_URL"] = args.target_url
        if args.inject_failure == "before_migration":
            raise RehearsalError("injected failure before migration")
        _run(alembic + ["upgrade", "head"], env=env, timeout=args.timeout)
        step("migration", "PASS")
        if args.inject_failure == "after_migration":
            raise RehearsalError("injected failure after migration")
        query = "SELECT current_database(), current_user;"
        identity = _run([psql, args.target_url, "-At", "-c", query], env=env, timeout=args.timeout)
        step("target_identity", "PASS", identity)
        counts = {}
        for table in CRITICAL_TABLES:
            output = _run(
                [psql, args.target_url, "-At", "-v", "ON_ERROR_STOP=1", "-c",
                 f"SELECT COUNT(*) FROM public.{table};"],
                env=env,
                timeout=args.timeout,
            )
            counts[table] = int(output.strip().splitlines()[-1])
        payload["critical_row_counts"] = counts
        step("critical_row_counts", "PASS")
        payload["status"] = "PASS"
    except RehearsalError as exc:
        payload["errors"].append(str(exc))
        payload["status"] = "INJECTED_FAILURE" if args.inject_failure != "none" else "FAILED"
        step("rehearsal", payload["status"], str(exc))
    payload["elapsed_seconds"] = round(max(0.0, time.monotonic() - started), 3)
    payload["completed_at"] = datetime.now(timezone.utc).isoformat()
    _write_report(args.report.resolve(), payload)
    if payload["status"] == "FAILED":
        raise RehearsalError(payload["errors"][-1])
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--inject-failure", choices=("none", "before_restore", "after_restore", "before_migration", "after_migration"), default="none")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--pg-dump-bin")
    parser.add_argument("--pg-restore-bin")
    parser.add_argument("--psql-bin")
    args = parser.parse_args()
    try:
        result = run_rehearsal(args)
    except RehearsalError as exc:
        print(f"POSTGRESQL RESTORE REHEARSAL FAILED: {exc}", file=sys.stderr)
        return 2
    print(f"POSTGRESQL RESTORE REHEARSAL {result['status']}: report={args.report}")
    return 0 if result["status"] in {"PASS", "INJECTED_FAILURE"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
