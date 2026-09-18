#!/usr/bin/env bash
# Deploy an exact Git revision to the canonical Podman Compose stack.
#
# This intentionally differs from restart_services.sh, which is retained for
# the legacy bare-metal services. Running both owners creates duplicate
# workers against the same database and incident queue.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEPLOY_REF="${DEPLOY_REF:-origin/main}"
SERVICES=(dashboard-web full-executor watcher worker code-repair telegram-ai)

cd "$REPO_DIR"
if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: refusing to replace a dirty checkout" >&2
  exit 3
fi

if [[ "$DEPLOY_REF" == origin/* ]]; then
  git fetch --prune origin "${DEPLOY_REF#origin/}"
elif [[ ! "$DEPLOY_REF" =~ ^[0-9a-f]{40}$ ]]; then
  echo "ERROR: DEPLOY_REF must be origin/<branch> or a full commit SHA" >&2
  exit 2
else
  git fetch --prune origin main
  git cat-file -e "${DEPLOY_REF}^{commit}"
fi

if [ "$DEPLOY_REF" = "origin/main" ]; then
  git checkout -B main origin/main
else
  git checkout --detach "$DEPLOY_REF"
fi
git reset --hard "$DEPLOY_REF"

# Keep the narrow per-container restart helper in sync with the deployed
# checkout. The Dashboard talks to its Unix socket; no host D-Bus is exposed
# to the web container.
install -m 0644 "$REPO_DIR/scripts/deploy/systemd/ceph-ai-container-restart.service" /etc/systemd/system/
install -m 0644 "$REPO_DIR/scripts/deploy/systemd/ceph-ai-container-restart.socket" /etc/systemd/system/
install -m 0755 "$REPO_DIR/scripts/deploy/container_restart_helper.py" /usr/local/libexec/ceph-ai-container-restart
install -d -m 0750 /run/ceph-ai
for heartbeat in worker watcher; do
  if [ ! -e "/run/ceph-ai/$heartbeat.json" ]; then
    install -m 0640 /dev/null "/run/ceph-ai/$heartbeat.json"
  fi
done
# Remove the legacy template before reloading units; it granted the old
# Dashboard path broader host systemd/D-Bus control.
while read -r legacy_unit; do
  [ -n "$legacy_unit" ] || continue
  systemctl disable --now "$legacy_unit" || true
done < <(systemctl list-units --all --plain --no-legend 'ceph-ai-container-restart@*.service' | awk '{print $1}')
rm -f /etc/systemd/system/ceph-ai-container-restart@.service
systemctl daemon-reload
systemctl reset-failed ceph-ai-container-restart.service || true
systemctl enable --now ceph-ai-container-restart.socket

# Migrate before a newly-built process can query a table/column introduced by
# this revision. ``heads`` safely applies all pending migration branches.
"$REPO_DIR/.venv/bin/alembic" upgrade heads

# Dependencies are baked into the application image. Build before restart so
# a changed pyproject.toml cannot leave newly-started processes importing an
# older dependency set.
podman-compose build

# The container service's launcher disables conflicting legacy service units
# and force-recreates the Python processes that import mounted application
# source.
systemctl restart ceph-ai-containers.service

for _attempt in $(seq 1 24); do
  all_healthy=true
  for service in "${SERVICES[@]}"; do
    status="$(podman inspect "ceph-ai_${service}_1" --format '{{.State.Healthcheck.Status}}' 2>/dev/null || true)"
    # podman-compose preserves hyphens in service names.
    if [ "$status" != "healthy" ]; then
      all_healthy=false
      break
    fi
  done
  consumer_count="$(podman exec rabbitmq rabbitmqctl list_queues -q name consumers | awk '$1 == "incidents" {print $2}')"
  if [ "$all_healthy" = true ] && [ "${consumer_count:-0}" -ge 1 ]; then
    break
  fi
  sleep 5
done

if [ "$all_healthy" != true ] || [ "${consumer_count:-0}" -lt 1 ]; then
  echo "ERROR: container health or Worker queue consumption did not recover" >&2
  podman ps --format '{{.Names}} {{.Status}}' >&2 || true
  exit 1
fi

curl -fsS --max-time 10 http://127.0.0.1:8000/login >/dev/null
echo "Deploy complete: $(git rev-parse HEAD); incident consumers=${consumer_count}"
