#!/usr/bin/env bash
# Explicit container-only rollback to the previously approved registry digest.
# This does not downgrade PostgreSQL: the operator must confirm that the
# current schema remains compatible with the previous application artifact.
set -euo pipefail

if [ "${CEPH_AI_ROLLBACK_ACK_COMPATIBLE_SCHEMA:-}" != "yes" ]; then
  echo "ERROR: set CEPH_AI_ROLLBACK_ACK_COMPATIBLE_SCHEMA=yes after a PostgreSQL compatibility rehearsal" >&2
  exit 2
fi

artifact_dir=/var/lib/ceph-ai/release-artifacts
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
current_file="$artifact_dir/current-image-ref"
previous_file="$artifact_dir/previous-image-ref"
if [ ! -s "$current_file" ] || [ ! -s "$previous_file" ]; then
  echo "ERROR: current and previous approved image references are required" >&2
  exit 2
fi
IFS= read -r current_ref < "$current_file"
IFS= read -r previous_ref < "$previous_file"
for reference in "$current_ref" "$previous_ref"; do
  if [[ ! "$reference" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]]; then
    echo "ERROR: rollback reference is not an immutable GHCR digest" >&2
    exit 2
  fi
done
if [ "$current_ref" = "$previous_ref" ]; then
  echo "ERROR: previous artifact is the current artifact" >&2
  exit 2
fi

podman pull "$previous_ref"
expected_id="$(podman image inspect "$previous_ref" --format '{{.Id}}')"
restore_current_release() {
  echo "ERROR: rollback post-check failed; restoring the pre-rollback image reference" >&2
  tmp_ref="$(mktemp "${current_file}.restore.XXXXXX")"
  printf '%s\n' "$current_ref" > "$tmp_ref"
  chmod 0640 "$tmp_ref"
  mv -f "$tmp_ref" "$current_file"
  systemctl restart ceph-ai-containers.service || true
}
tmp_ref="$(mktemp "${current_file}.rollback.XXXXXX")"
printf '%s\n' "$previous_ref" > "$tmp_ref"
chmod 0640 "$tmp_ref"
mv -f "$tmp_ref" "$current_file"
systemctl restart ceph-ai-containers.service

services=(dashboard-web full-executor watcher worker telegram-ai)
for _attempt in $(seq 1 24); do
  healthy=true
  for service in "${services[@]}"; do
    name="ceph-ai_${service}_1"
    state="$(podman inspect "$name" --format '{{.State.Healthcheck.Status}}' 2>/dev/null || true)"
    image_id="$(podman inspect "$name" --format '{{.Image}}' 2>/dev/null || true)"
    if [ "$state" != "healthy" ] || [ "$image_id" != "$expected_id" ]; then
      healthy=false
      break
    fi
  done
  if [ "$healthy" = true ]; then
    break
  fi
  sleep 5
done
if [ "$healthy" != true ]; then
  restore_current_release
  exit 1
fi

consumer_count="$(podman exec rabbitmq rabbitmqctl list_queues -q name consumers | awk '$1 == "incidents" {print $2}')"
if [ "${consumer_count:-0}" -lt 1 ]; then
  restore_current_release
  echo "ERROR: rollback completed without an incidents queue consumer" >&2
  exit 1
fi

if ! curl -fsS --max-time 10 http://127.0.0.1:8000/api/system/health >/dev/null || \
   ! curl -fsS --max-time 10 http://127.0.0.1:8000/login >/dev/null; then
  restore_current_release
  exit 1
fi

printf '%s\n' "$current_ref" > "$previous_file"
echo "Container rollback complete: $previous_ref (database schema unchanged)"
