#!/usr/bin/env bash
# Redeploys this exact checkout: pulls latest main, installs any new
# dependencies, applies pending DB migrations, and restarts the three
# long-running services (watcher, worker, dashboard) the same way they've
# always been run here — plain `nohup ... & disown` background processes,
# no systemd unit exists on this host.
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

echo "==> Applying DB migrations"
alembic upgrade head

echo "==> Stopping existing services (if running)"
pkill -f "python -m watcher.main" || true
pkill -f "python -m worker.main" || true
pkill -f "uvicorn dashboard.app:app" || true
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

sleep 3
echo "==> Deploy complete: $(date -u +%FT%TZ)"
echo "==> Running processes:"
pgrep -fa "watcher.main|worker.main|uvicorn dashboard.app" || echo "WARNING: no matching processes found after restart"
