#!/usr/bin/env bash
set -euo pipefail

# Run this before every Alembic upgrade. The script never deletes or replaces
# an existing backup; it creates a new 0600 artifact for the current timestamp.
database_url="${DATABASE_URL:-${CEPH_AI_DATABASE_URL:-}}"
destination="${1:-${CEPH_AI_DATABASE_BACKUP_DIR:-/var/backups/ceph-ai}}"

if [[ -z "$database_url" ]]; then
  echo "DATABASE_URL or CEPH_AI_DATABASE_URL is required" >&2
  exit 2
fi

umask 077
mkdir -p "$destination"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"

case "$database_url" in
  postgresql://*|postgres://*|postgresql+psycopg://*|postgresql+psycopg2://*)
    if [[ -n "${PG_DUMP_BIN:-}" ]]; then
      pg_dump_bin="$PG_DUMP_BIN"
    elif [[ -x /usr/pgsql-18/bin/pg_dump ]]; then
      pg_dump_bin=/usr/pgsql-18/bin/pg_dump
    else
      pg_dump_bin="$(command -v pg_dump || true)"
    fi
    [[ -x "$pg_dump_bin" ]] || { echo "pg_dump is required" >&2; exit 2; }
    output="$destination/ceph-ai-$stamp.dump"
    # SQLAlchemy URLs include the driver name, while libpq tools expect a
    # plain PostgreSQL URL. Keep credentials/query parameters unchanged.
    pg_dump_url="${database_url/postgresql+psycopg:/postgresql:}"
    pg_dump_url="${pg_dump_url/postgresql+psycopg2:/postgresql:}"
    "$pg_dump_bin" --format=custom --file="$output" "$pg_dump_url"
    echo "Created PostgreSQL backup: $output"
    ;;
  sqlite:///*)
    command -v sqlite3 >/dev/null || { echo "sqlite3 is required" >&2; exit 2; }
    database_path="${database_url#sqlite:///}"
    output="$destination/ceph-ai-$stamp.sqlite3"
    sqlite3 "$database_path" ".backup '$output'"
    echo "Created SQLite backup: $output"
    ;;
  *)
    echo "Unsupported database URL scheme; backup manually before migration" >&2
    exit 2
    ;;
esac
