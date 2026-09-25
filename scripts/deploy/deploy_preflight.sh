#!/usr/bin/env bash
# Read-only deployment prerequisite check. Run before checkout, migration, or restart.
set -u -o pipefail

DEPLOY_DIR="${CEPH_AI_DEPLOY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ENV_FILE="${CEPH_AI_ENV_FILE:-/var/lib/ceph-ai/config/.env}"
BACKUP_DIR="${CEPH_AI_DATABASE_BACKUP_DIR:-/var/lib/ceph-ai/database-backups}"
REGISTRY_URL="${CEPH_AI_REGISTRY_PROBE_URL:-https://ghcr.io/v2/}"
REQUIRED_DIRS="${CEPH_AI_PREFLIGHT_REQUIRED_DIRS:-/etc/systemd/system /var/lib/ceph-ai /var/lib/containers /run/ceph-ai}"
REQUIRED_COMMANDS="${CEPH_AI_PREFLIGHT_REQUIRED_COMMANDS:-bash git podman systemctl install curl awk mktemp}"
REPORT_FILE="${CEPH_AI_PREFLIGHT_REPORT:-}"
failures=0

emit() {
  local status="$1" check="$2" detail="$3"
  printf 'PREFLIGHT status=%s check=%s detail=%s\n' "$status" "$check" "$detail"
}
pass() { emit PASS "$1" "$2"; }
fail() { emit FAIL "$1" "$2" >&2; failures=$((failures + 1)); }

env_value() {
  [ -r "$ENV_FILE" ] || return 0
  awk -F= -v key="$1" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE"
}

run_checks() {
  emit INFO phase preflight

  local command_name
  for command_name in $REQUIRED_COMMANDS; do
    if command -v "$command_name" >/dev/null 2>&1; then
      pass "command.$command_name" "available"
    else
      fail "command.$command_name" "missing"
    fi
  done

  if command -v podman-compose >/dev/null 2>&1 || podman compose version >/dev/null 2>&1; then
    pass compose-provider "available"
  else
    fail compose-provider "podman-compose/podman-compose-plugin-unavailable"
  fi

  if [ -d "$DEPLOY_DIR/.git" ] && [ -r "$DEPLOY_DIR/.git" ]; then
    pass deploy-checkout "$DEPLOY_DIR"
  else
    fail deploy-checkout "not-a-readable-git-checkout"
  fi

  local required_dir
  for required_dir in $REQUIRED_DIRS; do
    if [ -d "$required_dir" ] && [ -r "$required_dir" ] && [ -w "$required_dir" ] && [ -x "$required_dir" ]; then
      pass "permission.$required_dir" "rwx"
    else
      fail "permission.$required_dir" "missing-or-not-rwx"
    fi
  done

  if [ -x "$DEPLOY_DIR/.venv/bin/alembic" ]; then
    pass migration-tool "available"
  else
    fail migration-tool "missing:$DEPLOY_DIR/.venv/bin/alembic"
  fi

  local database_url
  database_url="${DATABASE_URL:-$(env_value DATABASE_URL)}"
  case "$database_url" in
    postgresql://*|postgresql+psycopg://*|postgresql+psycopg2://*) pass database-profile "postgresql" ;;
    "") fail database-profile "DATABASE_URL-unset" ;;
    *) fail database-profile "non-postgresql-profile" ;;
  esac

  if [ -d "$BACKUP_DIR" ] && [ -w "$BACKUP_DIR" ] && [ -x "$BACKUP_DIR" ]; then
    pass backup-destination "available"
  else
    fail backup-destination "missing-or-not-writable"
  fi

  if [ "${CEPH_AI_PREFLIGHT_SKIP_HOST_RUNTIME:-0}" = 1 ]; then
    pass host-runtime "skipped-by-test-harness"
  else
    if systemctl cat ceph-ai-containers.service >/dev/null 2>&1; then
      pass systemd-unit "ceph-ai-containers.service"
    else
      fail systemd-unit "ceph-ai-containers.service-unavailable"
    fi
    if podman container exists rabbitmq >/dev/null 2>&1 && \
       podman exec rabbitmq rabbitmqctl status >/dev/null 2>&1; then
      pass rabbitmq "container-and-rabbitmqctl-available"
    else
      fail rabbitmq "container-or-rabbitmqctl-unavailable"
    fi
  fi

  if [ "${CEPH_AI_PREFLIGHT_SKIP_NETWORK:-0}" = 1 ]; then
    pass registry-network "skipped-by-test-harness"
  elif curl --silent --show-error --fail --head --connect-timeout 3 --max-time 5 \
      "$REGISTRY_URL" >/dev/null 2>&1; then
    pass registry-network "$REGISTRY_URL"
  else
    fail registry-network "unreachable:$REGISTRY_URL"
  fi

  if [ -n "${GH_TOKEN:-}" ] || podman login --get-login ghcr.io >/dev/null 2>&1; then
    pass registry-auth "credential-source-available"
  else
    fail registry-auth "GH_TOKEN-unset-and-no-existing-login"
  fi

  if [ "$failures" -eq 0 ]; then
    emit PASS summary "all-checks-passed"
    return 0
  fi
  emit FAIL summary "${failures}-checks-failed" >&2
  return 1
}

if [ -n "$REPORT_FILE" ]; then
  report_dir="$(dirname "$REPORT_FILE")"
  if [ ! -d "$report_dir" ] || [ ! -w "$report_dir" ]; then
    printf 'PREFLIGHT status=FAIL check=report detail=directory-not-writable\n' >&2
    exit 1
  fi
  report_tmp="$(mktemp "$report_dir/.deploy-preflight.XXXXXX")"
  trap 'rm -f "$report_tmp"' EXIT
  if run_checks >"$report_tmp" 2>&1; then
    result=0
  else
    result=$?
  fi
  chmod 0640 "$report_tmp"
  mv -f "$report_tmp" "$REPORT_FILE"
  trap - EXIT
  cat "$REPORT_FILE"
  exit "$result"
fi

run_checks
