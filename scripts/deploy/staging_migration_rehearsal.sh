#!/usr/bin/env bash
# Run a migration rehearsal against an explicitly isolated staging PostgreSQL.
# The safety confirmations are mandatory so this cannot silently use the live
# host's default .env or a production database.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

: "${STAGING_DATABASE_URL:?Set STAGING_DATABASE_URL to the isolated staging PostgreSQL URL}"
: "${STAGING_TARGET_ID:?Set STAGING_TARGET_ID to a non-production target identifier}"
: "${CONFIRM_STAGING_MIGRATION:?Set CONFIRM_STAGING_MIGRATION=I_UNDERSTAND_STAGING_ONLY}"

if [ "$CONFIRM_STAGING_MIGRATION" != I_UNDERSTAND_STAGING_ONLY ]; then
  echo "Refusing: staging confirmation token is incorrect" >&2
  exit 2
fi
case "$STAGING_TARGET_ID" in
  ""|production|prod|live)
    echo "Refusing: STAGING_TARGET_ID does not identify an isolated staging target" >&2
    exit 2
    ;;
esac
case "$STAGING_DATABASE_URL" in
  postgresql://*|postgres://*|postgresql+psycopg://*|postgresql+psycopg2://*) ;;
  *) echo "Refusing: staging rehearsal requires PostgreSQL" >&2; exit 2 ;;
esac

backup_dir="${STAGING_BACKUP_DIR:-}"
report_path="${STAGING_REPORT_PATH:-$repo_root/artifacts/staging-migration-rehearsal.json}"
[ -n "$backup_dir" ] || { echo "Set STAGING_BACKUP_DIR to isolated staging storage" >&2; exit 2; }

export CEPH_AI_ENV_FILE=/dev/null
export CEPH_AI_ENVIRONMENT=staging
export DATABASE_URL="$STAGING_DATABASE_URL"
mkdir -p "$backup_dir" "$(dirname "$report_path")"
umask 077

before="$($repo_root/.venv/bin/alembic current 2>&1 | tail -n 1)"
backup_output="$(DATABASE_URL="$STAGING_DATABASE_URL" bash scripts/deploy/backup_database_before_migration.sh "$backup_dir")"
printf '%s\n' "$backup_output"
backup_path="$(printf '%s\n' "$backup_output" | sed -n 's/^Created PostgreSQL backup: //p')"
[ -n "$backup_path" ] || { echo "Backup path was not reported; refusing migration" >&2; exit 2; }

pg_restore_bin="${PG_RESTORE_BIN:-$(command -v pg_restore || true)}"
[ -x "$pg_restore_bin" ] || { echo "pg_restore is required to validate the backup" >&2; exit 2; }
"$pg_restore_bin" --list "$backup_path" >/dev/null

head="$($repo_root/.venv/bin/alembic heads | awk '/\(head\)/ {print $1; exit}')"
$repo_root/.venv/bin/alembic upgrade head
after="$($repo_root/.venv/bin/alembic current 2>&1 | tail -n 1)"

checksum="$(sha256sum "$backup_path" | awk '{print $1}')"
timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
tmp_report="${report_path}.tmp.$$"
REPORT_PATH="$tmp_report" \
  REPORT_FINAL_PATH="$report_path" \
  REHEARSAL_TARGET="$STAGING_TARGET_ID" \
  REHEARSAL_TIMESTAMP="$timestamp" \
  REHEARSAL_COMMIT="$($repo_root/.venv/bin/python -c 'import subprocess; print(subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())')" \
  REHEARSAL_BEFORE="$before" \
  REHEARSAL_AFTER="$after" \
  REHEARSAL_HEAD="$head" \
  REHEARSAL_BACKUP_PATH="$backup_path" \
  REHEARSAL_BACKUP_SHA256="$checksum" \
  "$repo_root/.venv/bin/python" - <<'PY'
import json
import os
from pathlib import Path

payload = {
    "schema": "ceph-ai.staging-migration-rehearsal.v1",
    "target_id": os.environ["REHEARSAL_TARGET"],
    "timestamp": os.environ["REHEARSAL_TIMESTAMP"],
    "commit_sha": os.environ["REHEARSAL_COMMIT"],
    "revision_before": os.environ["REHEARSAL_BEFORE"],
    "revision_after": os.environ["REHEARSAL_AFTER"],
    "alembic_head": os.environ["REHEARSAL_HEAD"],
    "backup_path": os.environ["REHEARSAL_BACKUP_PATH"],
    "backup_sha256": os.environ["REHEARSAL_BACKUP_SHA256"],
    "restore_validation": "pg_restore --list passed",
}
path = Path(os.environ["REPORT_PATH"])
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
path.chmod(0o600)
path.replace(Path(os.environ["REPORT_FINAL_PATH"]))
PY

echo "STAGING MIGRATION REHEARSAL PASSED: target=$STAGING_TARGET_ID head=$head report=$report_path"
