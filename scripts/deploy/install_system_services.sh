#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ "$REPO_DIR" != "/root/ceph-ai" ]; then
  echo "ERROR: production units are scoped to /root/ceph-ai; got $REPO_DIR" >&2
  exit 2
fi

install -m 0644 "$REPO_DIR"/scripts/deploy/systemd/ceph-ai-*.service /etc/systemd/system/
install -m 0644 "$REPO_DIR/scripts/deploy/logrotate/ceph-ai" /etc/logrotate.d/ceph-ai
systemctl daemon-reload
systemctl enable ceph-ai-watcher ceph-ai-worker ceph-ai-dashboard
# Retire the legacy nohup processes before systemd takes ownership. Anchored
# command patterns cannot match this installer shell itself.
pkill -TERM -f '^/root/ceph-ai/.venv/bin/python -m watcher.main$' || true
pkill -TERM -f '^/root/ceph-ai/.venv/bin/python -m worker.main$' || true
pkill -TERM -f '^/root/ceph-ai/.venv/bin/python -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8000$' || true
sleep 3
systemctl restart ceph-ai-watcher ceph-ai-worker ceph-ai-dashboard
