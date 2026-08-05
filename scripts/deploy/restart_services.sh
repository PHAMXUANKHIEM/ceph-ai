#!/usr/bin/env bash
# Redeploys this exact checkout: pulls latest main, installs any new
# dependencies, applies pending DB migrations, and restarts the four
# long-running services (watcher, worker, dashboard, test-runner-frontend)
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
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

echo "==> Pulling latest main"
git fetch origin main
git reset --hard origin/main

echo "==> Installing dependencies"
source .venv/bin/activate
pip install -e . --quiet
npm --prefix ceph-upgrade-test-runner-frontend install --silent

echo "==> Applying DB migrations"
alembic upgrade head

echo "==> Stopping existing services (if running)"
pkill -f "python -m watcher.main" || true
pkill -f "python -m worker.main" || true
pkill -f "uvicorn dashboard.app:app" || true
pkill -f "ceph-upgrade-test-runner-frontend/node_modules/.bin/vite" || true
sleep 2

echo "==> Starting services"
# Optional server-local override (gitignored, never committed — this repo
# may end up on a public remote and must not hardcode this box's real bind
# address) — e.g. `echo 'DASHBOARD_HOST=103.69.193.220' >
# scripts/deploy/deploy.local.env` once, on this server only.
if [ -f "$REPO_DIR/scripts/deploy/deploy.local.env" ]; then
  # shellcheck disable=SC1091
  source "$REPO_DIR/scripts/deploy/deploy.local.env"
fi
DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"

nohup python -m watcher.main >> /var/log/ceph-aiops-watcher.log 2>&1 &
disown
nohup python -m worker.main >> /var/log/ceph-aiops-worker.log 2>&1 &
disown
nohup python -m uvicorn dashboard.app:app --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT" \
  >> /var/log/ceph-aiops-dashboard.log 2>&1 &
disown

# Test Runner UI (ceph-upgrade-test-runner-frontend) is a Vite dev server
# whose /api proxy target is DASHBOARD_HOST/DASHBOARD_PORT (see
# ceph-upgrade-test-runner-frontend/vite.config.js) -- it MUST get the same
# values just used for uvicorn above, exported via env rather than hardcoded
# `localhost`. When DASHBOARD_HOST is overridden to a specific bind address
# (deploy.local.env, e.g. 103.69.193.220) the dashboard isn't reachable on
# localhost at all, and a mismatched proxy target here silently breaks every
# /api call with ECONNREFUSED -- surfaced in the UI as a misleading "Không
# có test case nào" empty state that looks like a Group/Priority filter bug.
export DASHBOARD_HOST
export DASHBOARD_PORT
(
  cd ceph-upgrade-test-runner-frontend
  nohup ./node_modules/.bin/vite --host 0.0.0.0 --port 5173 \
    >> /var/log/ceph-aiops-test-runner-frontend.log 2>&1 &
  disown
)

sleep 3
echo "==> Deploy complete: $(date -u +%FT%TZ)"
echo "==> Running processes:"
pgrep -fa "watcher.main|worker.main|uvicorn dashboard.app|test-runner-frontend/node_modules/.bin/vite" || echo "WARNING: no matching processes found after restart"
