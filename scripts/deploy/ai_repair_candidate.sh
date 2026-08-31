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
if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: refusing candidate deployment because $REPO_DIR has uncommitted changes."
  echo "Create a commit or explicitly preserve/remove the local changes before deploying."
  exit 3
fi
ROLLED_BACK=false
REPAIR_TEST_DIR=""
CONTAINER_RUNTIME=false
if [ "${CEPH_AI_CONTAINERIZED:-false}" != "true" ] && \
   systemctl is-active --quiet ceph-ai-containers.service && \
   command -v podman-compose >/dev/null 2>&1; then
  CONTAINER_RUNTIME=true
fi

# Written by dashboard/telegram_chat.py::_mark_full_run_started /
# _mark_full_run_finished, bind-mounted into every container at
# /var/lib/ceph-ai (see compose.yaml) — the same recovery marker that lets
# a restart notify the user their Single Full run was interrupted. Reading
# it from the host lets this deploy avoid causing that interruption in the
# first place, instead of only reporting it after the fact.
SINGLE_FULL_RUN_STATE_FILE="/var/lib/ceph-ai/telegram-single-full-runs.json"
SINGLE_FULL_DEPLOY_WAIT_TIMEOUT_SECONDS="${AI_REPAIR_SINGLE_FULL_WAIT_TIMEOUT_SECONDS:-600}"
SINGLE_FULL_DEPLOY_POLL_SECONDS=5

wait_for_single_full_idle() {
  if [ ! -f "$SINGLE_FULL_RUN_STATE_FILE" ] || ! command -v jq >/dev/null 2>&1; then
    return 0
  fi
  local waited=0 in_flight
  while true; do
    in_flight="$(jq 'length' "$SINGLE_FULL_RUN_STATE_FILE" 2>/dev/null || echo 0)"
    if ! [[ "$in_flight" =~ ^[0-9]+$ ]] || [ "$in_flight" -eq 0 ]; then
      return 0
    fi
    if [ "$waited" -ge "$SINGLE_FULL_DEPLOY_WAIT_TIMEOUT_SECONDS" ]; then
      echo "WARNING: Telegram Single Full still running after ${SINGLE_FULL_DEPLOY_WAIT_TIMEOUT_SECONDS}s wait; deploying anyway (the running session will be interrupted and its owner notified on restart)."
      return 0
    fi
    echo "==> Waiting for $in_flight in-flight Telegram Single Full run(s) to finish before recreating containers (${waited}s/${SINGLE_FULL_DEPLOY_WAIT_TIMEOUT_SECONDS}s)..."
    sleep "$SINGLE_FULL_DEPLOY_POLL_SECONDS"
    waited=$((waited + SINGLE_FULL_DEPLOY_POLL_SECONDS))
  done
}

deploy_container_services() {
  local ref="$1"
  echo "==> Deploying $ref through the Podman stack"
  git checkout -B main "$ref"
  git reset --hard "$ref"
  wait_for_single_full_idle
  # The calling nightly supervisor intentionally stays alive in its host
  # process.  Recreating code-repair here would kill the process that owns
  # promotion/rollback; it continues using its already imported code.
  podman-compose up -d --no-deps dashboard-web telegram-ai full-executor watcher worker
}

wait_for_container_health() {
  local service status attempt
  for service in dashboard-web telegram-ai full-executor watcher worker; do
    for attempt in $(seq 1 30); do
      status="$(podman inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}starting{{end}}' "ceph-ai_${service}_1" 2>/dev/null || true)"
      if [ "$status" = "healthy" ]; then
        break
      fi
      sleep 2
    done
    if [ "$status" != "healthy" ]; then
      echo "ERROR: container ceph-ai_${service}_1 is not healthy (status=${status:-missing})"
      return 1
    fi
  done
}

deploy_candidate() {
  if [ "$CONTAINER_RUNTIME" = true ]; then
    deploy_container_services "origin/$CANDIDATE_BRANCH"
  else
    DEPLOY_REF="origin/$CANDIDATE_BRANCH" bash scripts/deploy/restart_services.sh
  fi
}

rollback_deployment() {
  if [ "$CONTAINER_RUNTIME" = true ]; then
    deploy_container_services "$PREVIOUS_SHA"
  else
    DEPLOY_REF="$PREVIOUS_SHA" bash scripts/deploy/restart_services.sh
  fi
}
cleanup_worktree() {
  if [ -n "$REPAIR_TEST_DIR" ] && [ -d "$REPAIR_TEST_DIR" ]; then
    git worktree remove --force "$REPAIR_TEST_DIR" >/dev/null 2>&1 || true
  fi
}
rollback() {
  local exit_code=$?
  cleanup_worktree
  if [ "$ROLLED_BACK" = false ]; then
    ROLLED_BACK=true
    echo "==> Candidate failed; rolling back to $PREVIOUS_SHA"
    rollback_deployment || true
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
DEFAULT_CANDIDATE_TEST_COMMAND="PYTHONPATH=. .venv/bin/pytest -q --ignore=tests/test_migrations.py --ignore=tests/test_mq.py --deselect=tests/test_dashboard_settings.py::test_require_admin_privilege_rejects_unknown_username --deselect=tests/test_dashboard_settings.py::test_migrate_database_route_adds_missing_table"
CANDIDATE_TEST_COMMAND="${AI_REPAIR_CANDIDATE_TEST_COMMAND:-$DEFAULT_CANDIDATE_TEST_COMMAND}"
# These suites are CI-only on this host: migration tests require a newer
# SQLite than CentOS' Python 3.11 provides, while MQ topology tests require
# exclusive ownership of queues consumed by the live staging Worker. Two
# settings cases share the migration test's process-global SQLite engine and
# are run in CI instead. Candidate-specific tests already ran before push;
# this gate still executes the remaining ~2.5k application tests.
(cd "$REPAIR_TEST_DIR" && timeout "${AI_REPAIR_CANDIDATE_TEST_TIMEOUT_SECONDS:-900}" \
  bash -lc "$CANDIDATE_TEST_COMMAND")
cleanup_worktree
REPAIR_TEST_DIR=""

echo "==> Deploying candidate"
deploy_candidate

echo "==> Verifying service processes"
if [ "$CONTAINER_RUNTIME" = true ]; then
  wait_for_container_health
  curl -fsS http://127.0.0.1:8000/login >/dev/null
else
  VENV_PYTHON="$REPO_DIR/.venv/bin/python"
  pgrep -f "$VENV_PYTHON -m watcher.main" >/dev/null
  pgrep -f "$VENV_PYTHON -m worker.main" >/dev/null
  pgrep -f "$VENV_PYTHON -m uvicorn dashboard.app:app" >/dev/null
fi

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
