#!/usr/bin/env bash
set -euo pipefail

# Canonical production migration entrypoint. Application containers do not
# run migrations on startup; one operator/deployment job runs this script.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

export CEPH_AI_ENV_FILE="${CEPH_AI_ENV_FILE:-/var/lib/ceph-ai/config/.env}"
database_url="$(.venv/bin/python -c 'from config.settings import settings; print(settings.database_url)')"
environment="$(.venv/bin/python -c 'from config.settings import settings; print(settings.ceph_ai_environment)')"

if [[ "$environment" == "production" && "$database_url" != postgresql://* && "$database_url" != postgres://* && "$database_url" != postgresql+psycopg://* && "$database_url" != postgresql+psycopg2://* ]]; then
  echo "Production migrations require a PostgreSQL DATABASE_URL" >&2
  exit 2
fi

# The backup script creates a new 0600 artifact and never overwrites an older
# backup. It also normalizes SQLAlchemy's postgresql+psycopg URL for pg_dump.
DATABASE_URL="$database_url" bash scripts/deploy/backup_database_before_migration.sh

exec .venv/bin/python -m alembic upgrade head
