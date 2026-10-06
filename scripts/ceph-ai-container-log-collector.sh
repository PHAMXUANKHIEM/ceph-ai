#!/usr/bin/env bash
set -uo pipefail

log_dir=/var/log/ceph-ai-server
install -d -o root -g 10001 -m 2770 "$log_dir"
umask 0007

follow_container() {
  local container="$1" log_file="$2"
  local attempts=0
  while (( attempts < 10 )) && ! podman container exists "$container"; do
    sleep 2
    ((attempts += 1))
  done
  if ! podman container exists "$container"; then
    echo "Container not found after startup wait: $container" >&2
    return 0
  fi
  podman logs --timestamps --tail 500 "$container" > "$log_file" 2>&1 || true
  while true; do
    podman logs --timestamps --follow --since 1s "$container" >> "$log_file" 2>&1 || true
    sleep 1
  done
}

follow_container ceph-ai_dashboard-web_1 "$log_dir/app-dashboard.log" &
follow_container ceph-ai_watcher_1 "$log_dir/app-watcher.log" &
follow_container ceph-ai_worker_1 "$log_dir/app-worker.log" &
wait
