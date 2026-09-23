#!/usr/bin/env bash
# Deploy an exact Git revision to the canonical Podman Compose stack.
#
# This intentionally differs from restart_services.sh, which is retained for
# the legacy bare-metal services. Running both owners creates duplicate
# workers against the same database and incident queue.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEPLOY_REF="${DEPLOY_REF:-origin/main}"
SERVICES=(dashboard-web full-executor watcher worker telegram-ai)
DEPLOY_IMAGE="${CEPH_AI_IMAGE:-}"
IMAGE_REF_FILE=/var/lib/ceph-ai/release-artifacts/current-image-ref
PREVIOUS_IMAGE_REF_FILE=/var/lib/ceph-ai/release-artifacts/previous-image-ref

cd "$REPO_DIR"
# A self-hosted runner may share the canonical checkout with an operator who
# is editing a bounded documentation file. Preserve only the explicitly
# allowlisted paths across the checkout/reset; any dirty source/config file
# still blocks deployment. The trap also restores the file when deployment
# fails, so a failed release cannot discard the operator's work.
ALLOWED_DIRTY_PATHS="${CEPH_AI_DEPLOY_ALLOWED_DIRTY_PATHS:-}"
PRESERVED_DIRTY_ROOT=""
restore_allowed_dirty_files() {
  local exit_status=$?
  if [ -n "$PRESERVED_DIRTY_ROOT" ]; then
    while IFS= read -r path; do
      [ -n "$path" ] || continue
      if [ -f "$PRESERVED_DIRTY_ROOT/$path.present" ]; then
        mkdir -p "$(dirname "$REPO_DIR/$path")"
        cp -a "$PRESERVED_DIRTY_ROOT/$path" "$REPO_DIR/$path"
      else
        rm -f "$REPO_DIR/$path"
      fi
    done < "$PRESERVED_DIRTY_ROOT/paths"
    rm -rf "$PRESERVED_DIRTY_ROOT"
  fi
  return "$exit_status"
}
trap restore_allowed_dirty_files EXIT

dirty_status="$(git status --porcelain --untracked-files=all)"
if [ -n "$dirty_status" ]; then
  PRESERVED_DIRTY_ROOT="$(mktemp -d /tmp/ceph-ai-deploy-preserved.XXXXXX)"
  : > "$PRESERVED_DIRTY_ROOT/paths"
  while IFS= read -r dirty_line; do
    [ -n "$dirty_line" ] || continue
    dirty_path="${dirty_line:3}"
    case ",$ALLOWED_DIRTY_PATHS," in
      *,"$dirty_path",*)
        printf '%s\n' "$dirty_path" >> "$PRESERVED_DIRTY_ROOT/paths"
        if [ -e "$REPO_DIR/$dirty_path" ] || [ -L "$REPO_DIR/$dirty_path" ]; then
          mkdir -p "$PRESERVED_DIRTY_ROOT/$(dirname "$dirty_path")"
          cp -a "$REPO_DIR/$dirty_path" "$PRESERVED_DIRTY_ROOT/$dirty_path"
          : > "$PRESERVED_DIRTY_ROOT/$dirty_path.present"
          rm -f "$REPO_DIR/$dirty_path"
        fi
        ;;
      *)
        echo "ERROR: refusing to replace a dirty checkout: $dirty_path" >&2
        exit 3
        ;;
    esac
  done <<< "$dirty_status"
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
install -m 0644 "$REPO_DIR/scripts/deploy/systemd/ceph-ai-code-repair-supervisor.service" /etc/systemd/system/
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

# Pull the registry manifest that passed CI. The immutable reference is the
# release identity; a local image ID is used only for running-container checks.
export CEPH_AI_IMAGE="$DEPLOY_IMAGE"
if [ -n "$DEPLOY_IMAGE" ]; then
  if [[ ! "$DEPLOY_IMAGE" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]]; then
    echo "ERROR: CEPH_AI_IMAGE must be an immutable ghcr.io manifest digest" >&2
    exit 4
  fi
  podman pull "$DEPLOY_IMAGE"
  approved_image_id="$(podman image inspect "$DEPLOY_IMAGE" --format '{{.Id}}')"
  artifact_commit="$(podman image inspect "$DEPLOY_IMAGE" --format '{{ index .Labels "org.opencontainers.image.revision" }}')"
  if [ "$artifact_commit" != "$(git rev-parse HEAD)" ]; then
    echo "ERROR: approved image revision does not match deployment checkout" >&2
    exit 4
  fi
else
  echo "ERROR: deployment requires CEPH_AI_IMAGE=ghcr.io/...@sha256:<digest>" >&2
  exit 4
fi

# Only migrate after the exact artifact has been verified or built. The
# canonical migration entrypoint creates a backup first and records revision,
# checksum, artifact and commit metadata. A failed backup must stop rollout.
"$REPO_DIR/scripts/deploy/run_migrations.sh"

# Persist the approved reference before systemd restarts the stack. A reboot
# must reuse it. Preserve the previous digest for explicit, schema-compatible
# container rollback rather than rebuilding the old checkout.
install -d -m 0750 "$(dirname "$IMAGE_REF_FILE")"
if [ -s "$IMAGE_REF_FILE" ]; then
  cp -a "$IMAGE_REF_FILE" "$PREVIOUS_IMAGE_REF_FILE"
fi
image_ref_tmp="$(mktemp "${IMAGE_REF_FILE}.XXXXXX")"
printf '%s\n' "$DEPLOY_IMAGE" > "$image_ref_tmp"
chmod 0640 "$image_ref_tmp"
mv -f "$image_ref_tmp" "$IMAGE_REF_FILE"

# The container service's launcher disables conflicting legacy service units
# and force-recreates the Python processes from the immutable image.
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

for service in "${SERVICES[@]}"; do
  running_image_id="$(podman inspect "ceph-ai_${service}_1" --format '{{.Image}}')"
  if [ "$running_image_id" != "$approved_image_id" ]; then
    echo "ERROR: running container $service is not using the approved registry artifact" >&2
    echo "expected=$approved_image_id actual=$running_image_id" >&2
    exit 5
  fi
done

curl -fsS --max-time 10 http://127.0.0.1:8000/login >/dev/null
# Code Repair continues as a separate host-owned process, without container
# source mounts. Its unit explicitly disables auto-push/deploy/promotion until
# candidates have their own scanned-artifact pipeline.
systemctl daemon-reload
systemctl enable --now ceph-ai-code-repair-supervisor.service
echo "Deploy complete: $(git rev-parse HEAD); incident consumers=${consumer_count}"
