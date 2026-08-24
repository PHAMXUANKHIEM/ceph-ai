#!/usr/bin/env bash
# Redeploys this exact checkout: pulls latest main, installs any new
# dependencies, applies pending DB migrations, and restarts the four
# long-running services (watcher, AI remediation watcher, worker, dashboard)
# the same way they've always been run here — plain `nohup ... & disown`
# background processes, no systemd unit exists on this host.
#
# Run from the repo root, as the same user the services already run as
# (root, matching this deployment's existing operational model — see the
# CI/CD workflow's Dev Notes for why this wasn't changed to a dedicated
# deploy user as part of adding CI/CD).
#
# Idempotent-ish: safe to re-run — pkill on a pattern that isn't currently
# running is a no-op (guarded by `|| true`), and starting the same service
# twice would just leave two processes running, so this always kills
# before starting.
#
# Multi-cluster note: this project is single-cluster-per-instance by design
# (one .env, one Settings() singleton, no cluster_id anywhere in the DB —
# see docs/multi-cluster-deployment.md). Monitoring a 2nd Ceph cluster means
# running a 2nd full checkout+.env+DB of this repo, either on its own
# server (nothing below matters, it's already isolated) or side-by-side on
# THIS SAME server (a different checkout directory, e.g.
# /root/source-code-vita/ceph-aiops-clusterb). The pkill/pgrep patterns and
# log filenames below are derived from $REPO_DIR specifically so a restart
# in one checkout can never kill or overwrite the logs of a sibling
# instance running from a different checkout — do not change them back to
# bare "python"/plain log names without re-reading this comment.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"
# Absolute interpreter path (not bare "python") and a log-file tag derived
# from the checkout's own directory name — both scope this instance's
# processes/logs away from any sibling ceph-aiops checkout running on the
# same host for a different cluster. For the existing single-instance
# deployment (checkout named "ceph-aiops") this reproduces the exact same
# /var/log/ceph-aiops-*.log paths as before, so it's a no-op there.
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
LOG_TAG="$(basename "$REPO_DIR")"

DEPLOY_REF="${DEPLOY_REF:-origin/main}"
echo "==> Deploying $DEPLOY_REF"
if [[ "$DEPLOY_REF" == origin/* ]]; then
  git fetch origin "${DEPLOY_REF#origin/}"
elif [[ ! "$DEPLOY_REF" =~ ^[0-9a-f]{40}$ ]]; then
  echo "ERROR: DEPLOY_REF must be origin/<branch> or a full commit SHA"
  exit 2
fi
if [ "$DEPLOY_REF" = "origin/main" ]; then
  git checkout -B main origin/main
else
  # Candidate staging deployments must not move the local main branch.
  git checkout --detach "$DEPLOY_REF"
fi
git reset --hard "$DEPLOY_REF"

echo "==> Installing dependencies"
source .venv/bin/activate
pip install -e . --quiet
if npm --version >/dev/null 2>&1; then
  npm --prefix ceph-health-dashboard install --silent
  npm --prefix ceph-health-dashboard run build --silent
else
  echo "WARNING: npm is unavailable; keeping the existing dashboard frontend build"
fi

echo "==> Applying DB migrations"
# "heads" (plural), not "head" — 2026-08-07: this repo can legitimately have
# more than one migration branch tip at once when unrelated feature work
# lands its own migration file before an earlier one is merged/reconciled
# (exactly what happened with the CRUSH-monitor and multi-cluster
# migrations). Bare `alembic upgrade head` refuses to guess which branch
# you meant and raises CommandError — which, under `set -euo pipefail`,
# aborted this ENTIRE script before it ever reached the "stop/start
# services" section below, silently leaving the OLD processes running
# while the checkout had already moved to new code that assumed the new
# migration's tables existed. `upgrade heads` applies every outstanding
# branch instead of guessing, and is a no-op for the normal single-head
# case, so this is strictly safer with no downside when there's only one
# head.
alembic upgrade heads

echo "==> Stopping existing services (if running)"
pkill -f "$VENV_PYTHON -m watcher.main" || true
pkill -f "$VENV_PYTHON -m watcher.remediation_main" || true
pkill -f "$VENV_PYTHON -m worker.main" || true
pkill -f "$VENV_PYTHON -m uvicorn dashboard.app:app" || true
sleep 2
# Uvicorn can remain alive briefly while its Telegram listener winds down.
# Never launch a second copy while an old process can still own the port.
pkill -9 -f "$VENV_PYTHON -m watcher.main" || true
pkill -9 -f "$VENV_PYTHON -m watcher.remediation_main" || true
pkill -9 -f "$VENV_PYTHON -m worker.main" || true
pkill -9 -f "$VENV_PYTHON -m uvicorn dashboard.app:app" || true

echo "==> Starting services"
# Optional server-local override (gitignored, never committed — this repo
# may end up on a public remote and must not hardcode this box's real bind
# address) — e.g. `echo 'DASHBOARD_HOST=103.69.193.220' >
# scripts/deploy/deploy.local.env` once, on this server only. When running
# a 2nd cluster's checkout on the SAME server, its own deploy.local.env
# MUST set a different DASHBOARD_PORT —
# otherwise the two instances fight over the same port.
if [ -f "$REPO_DIR/scripts/deploy/deploy.local.env" ]; then
  # shellcheck disable=SC1091
  source "$REPO_DIR/scripts/deploy/deploy.local.env"
fi
DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"

nohup "$VENV_PYTHON" -m watcher.main >> "/var/log/${LOG_TAG}-watcher.log" 2>&1 &
disown
nohup "$VENV_PYTHON" -m watcher.remediation_main >> "/var/log/${LOG_TAG}-remediation-watcher.log" 2>&1 &
disown
nohup "$VENV_PYTHON" -m worker.main >> "/var/log/${LOG_TAG}-worker.log" 2>&1 &
disown
nohup "$VENV_PYTHON" -m uvicorn dashboard.app:app --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT" \
  >> "/var/log/${LOG_TAG}-dashboard.log" 2>&1 &
disown

# The repair supervisor must survive candidate deployments because it owns the
# test/deploy/promote decision. Start it only when absent; never kill it in the
# stop section above. Starting an extra copy is safe: its advisory lock makes
# the newcomer exit immediately when a supervisor already owns the pipeline.
# This avoids brittle pgrep matching during an SSH-driven deployment.
nohup "$VENV_PYTHON" -m worker.code_repair_supervisor \
  >> "/var/log/${LOG_TAG}-code-repair-supervisor.log" 2>&1 &
disown

sleep 3

# A code pull can update static/app.js immediately while an old Uvicorn
# process still serves the previously imported FastAPI router table. That
# mismatch is especially confusing for newly-added pages: the navigation
# link appears, but clicking it returns 404. Prove that the NEW Dashboard
# process loaded the PG route before declaring this deployment successful.
# `/pgs` is login-protected, so an unauthenticated 303 is the expected
# healthy response; 200 is accepted for deployments whose auth policy was
# intentionally customized.
if [ "$DASHBOARD_HOST" = "0.0.0.0" ]; then
  DASHBOARD_CHECK_HOST="127.0.0.1"
else
  DASHBOARD_CHECK_HOST="$DASHBOARD_HOST"
fi
DASHBOARD_CHECK_URL="http://${DASHBOARD_CHECK_HOST}:${DASHBOARD_PORT}"
DASHBOARD_ROUTE_READY=false
for _attempt in 1 2 3 4 5 6 7 8 9 10; do
  DASHBOARD_ROUTE_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "$DASHBOARD_CHECK_URL/pgs" || true)"
  if [ "$DASHBOARD_ROUTE_STATUS" = "303" ] || [ "$DASHBOARD_ROUTE_STATUS" = "200" ]; then
    DASHBOARD_ROUTE_READY=true
    break
  fi
  sleep 1
done
if [ "$DASHBOARD_ROUTE_READY" != "true" ]; then
  echo "ERROR: Dashboard did not load /pgs after restart (HTTP ${DASHBOARD_ROUTE_STATUS:-000})."
  echo "Check /var/log/${LOG_TAG}-dashboard.log — an old process may still own port ${DASHBOARD_PORT}."
  exit 1
fi

echo "==> Deploy complete: $(date -u +%FT%TZ)"
echo "==> Dashboard route check: /pgs HTTP $DASHBOARD_ROUTE_STATUS"
echo "==> Running processes:"
pgrep -fa "$VENV_PYTHON -m watcher\.(main|remediation_main)|$VENV_PYTHON -m worker\.(main|code_repair_supervisor)|$VENV_PYTHON -m uvicorn dashboard.app" || echo "WARNING: no matching processes found after restart"
