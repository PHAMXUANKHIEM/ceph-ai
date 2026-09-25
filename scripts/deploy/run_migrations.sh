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
backup_output="$(DATABASE_URL="$database_url" bash scripts/deploy/backup_database_before_migration.sh)"
printf '%s\n' "$backup_output"
backup_path="$(printf '%s\n' "$backup_output" | sed -n 's/^Created PostgreSQL backup: //p; s/^Created SQLite backup: //p')"
if [[ -z "$backup_path" ]]; then
  echo "Migration backup path was not reported; refusing to migrate" >&2
  exit 2
fi
# Prove the reported artifact is present, private, fresh and a real dump
# before any schema change; a missing or exposed backup stops the deploy.
.venv/bin/python scripts/deploy/verify_migration_backup.py "$backup_path"

revision() {
  DATABASE_URL="$database_url" .venv/bin/python -c \
    'from sqlalchemy import create_engine, text; from config.settings import settings; e=create_engine(settings.database_url); c=e.connect(); print(c.execute(text("select version_num from alembic_version order by version_num limit 1")).scalar() or ""); c.close(); e.dispose()'
}

revision_before="$(revision)"
head_revision="$(.venv/bin/python -m alembic heads | awk 'NF {print $1; exit}')"
migration_checksum="$(find alembic/versions -maxdepth 1 -type f -name '*.py' -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}')"

if [ -n "${CEPH_AI_IMAGE:-}" ]; then
  if [[ ! "$CEPH_AI_IMAGE" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]]; then
    echo "Migration image must be the approved registry digest" >&2
    exit 3
  fi
  image_head="$(podman run --rm --network host \
    --env-file "$CEPH_AI_ENV_FILE" \
    --volume "$CEPH_AI_ENV_FILE:$CEPH_AI_ENV_FILE:ro" \
    --env "CEPH_AI_ENV_FILE=$CEPH_AI_ENV_FILE" \
    "$CEPH_AI_IMAGE" python -m alembic heads | awk 'NF {print $1; exit}')"
  if [ "$image_head" != "$head_revision" ]; then
    echo "Migration head differs between checkout and approved image" >&2
    exit 3
  fi
  podman run --rm --network host \
    --env-file "$CEPH_AI_ENV_FILE" \
    --volume "$CEPH_AI_ENV_FILE:$CEPH_AI_ENV_FILE:ro" \
    --env "CEPH_AI_ENV_FILE=$CEPH_AI_ENV_FILE" \
    "$CEPH_AI_IMAGE" python -m alembic upgrade head
else
  .venv/bin/python -m alembic upgrade head
fi
revision_after="$(revision)"

MIGRATION_METADATA_PATH="${MIGRATION_METADATA_PATH:-/var/lib/ceph-ai/release-artifacts/migration-latest.json}" \
MIGRATION_ENVIRONMENT="$environment" \
MIGRATION_GIT_COMMIT="$(git rev-parse HEAD)" \
MIGRATION_HEAD="$head_revision" \
MIGRATION_REVISION_BEFORE="$revision_before" \
MIGRATION_REVISION_AFTER="$revision_after" \
MIGRATION_CHECKSUM_SHA256="$migration_checksum" \
MIGRATION_BACKUP_PATH="$backup_path" \
.venv/bin/python scripts/deploy/record_migration_metadata.py
