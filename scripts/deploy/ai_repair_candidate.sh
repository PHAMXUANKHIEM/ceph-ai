#!/usr/bin/env bash
# Deploy a pushed ai-repair branch to this staging checkout and roll back on
# any deployment or smoke-test failure.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

CANDIDATE_BRANCH="${1:-}"
if [[ ! "$CANDIDATE_BRANCH" =~ ^ai-repair/[A-Za-z0-9._/-]+$ ]]; then
  echo "Usage: $0 ai-repair/<candidate-branch>"
  exit 2
fi

PREVIOUS_SHA="$(git rev-parse HEAD)"
ROLLED_BACK=false
rollback() {
  local exit_code=$?
  if [ "$ROLLED_BACK" = false ]; then
    ROLLED_BACK=true
    echo "==> Candidate failed; rolling back to $PREVIOUS_SHA"
    DEPLOY_REF="$PREVIOUS_SHA" bash scripts/deploy/restart_services.sh || true
  fi
  exit "$exit_code"
}
trap rollback ERR INT TERM

echo "==> Fetching candidate $CANDIDATE_BRANCH"
git fetch origin "$CANDIDATE_BRANCH"
CANDIDATE_SHA="$(git rev-parse "origin/$CANDIDATE_BRANCH")"

echo "==> Running candidate test gate at $CANDIDATE_SHA"
REPAIR_TEST_DIR="$(mktemp -d)"
git worktree add --detach "$REPAIR_TEST_DIR" "$CANDIDATE_SHA"
ln -s "$REPO_DIR/.venv" "$REPAIR_TEST_DIR/.venv"
# These suites are CI-only on this host: migration tests require a newer
# SQLite than CentOS' Python 3.11 provides, while MQ topology tests require
# exclusive ownership of queues consumed by the live staging Worker. Two
# settings cases share the migration test's process-global SQLite engine and
# are run in CI instead. Candidate-specific tests already ran before push;
# this gate still executes the remaining ~2.5k application tests.
(cd "$REPAIR_TEST_DIR" && PYTHONPATH=. .venv/bin/pytest -q \
  --ignore=tests/test_migrations.py \
  --ignore=tests/test_mq.py \
  --deselect=tests/test_dashboard_settings.py::test_require_admin_privilege_rejects_unknown_username \
  --deselect=tests/test_dashboard_settings.py::test_migrate_database_route_adds_missing_table)
git worktree remove --force "$REPAIR_TEST_DIR"

echo "==> Deploying candidate"
DEPLOY_REF="origin/$CANDIDATE_BRANCH" bash scripts/deploy/restart_services.sh

echo "==> Verifying service processes"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
pgrep -f "$VENV_PYTHON -m watcher.main" >/dev/null
pgrep -f "$VENV_PYTHON -m worker.main" >/dev/null
pgrep -f "$VENV_PYTHON -m uvicorn dashboard.app:app" >/dev/null

echo "==> Verifying fresh Watcher heartbeat"
PYTHONPATH=. .venv/bin/python - <<'PY'
from datetime import datetime, timedelta
from shared.db import SessionLocal
from shared.models import WatcherHeartbeat

with SessionLocal() as session:
    row = session.query(WatcherHeartbeat).order_by(WatcherHeartbeat.polled_at.desc()).first()
    if row is None or not row.success or row.polled_at < datetime.utcnow() - timedelta(minutes=3):
        raise SystemExit("Watcher heartbeat is missing, failed, or stale")
PY

trap - ERR INT TERM
echo "==> Candidate $CANDIDATE_SHA passed staging deployment and smoke tests"
echo "==> Previous revision available for rollback: $PREVIOUS_SHA"
