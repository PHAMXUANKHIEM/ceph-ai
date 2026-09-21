#!/usr/bin/env bash
# Read-only production-readiness preflight.
#
# This script never changes systemd, firewall, database or cluster state.  It
# fails closed when a release-critical identity, ownership or egress control
# cannot be proven from the target host.
set -u -o pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${CEPH_AI_ENV_FILE:-/var/lib/ceph-ai/config/.env}"
failures=0

pass() { printf 'PASS %-28s %s\n' "$1" "$2"; }
fail() { printf 'FAIL %-28s %s\n' "$1" "$2" >&2; failures=$((failures + 1)); }
pending() { printf 'PENDING %-25s %s\n' "$1" "$2"; failures=$((failures + 1)); }

env_value() {
  [ -r "$ENV_FILE" ] || return 0
  awk -F= -v key="$1" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE"
}

cd "$REPO_DIR"

if [ -x .venv/bin/alembic ]; then
  heads="$(.venv/bin/alembic heads 2>/dev/null | awk '/\(head\)/ {print $1}')"
  head_count="$(printf '%s\n' "$heads" | awk 'NF {count++} END {print count + 0}')"
  if [ "$head_count" -eq 1 ]; then
    pass migration-head "$heads"
  else
    fail migration-head "expected one Alembic head, found $head_count"
  fi
else
  fail migration-head "repository virtualenv/Alembic is unavailable"
fi

if command -v podman >/dev/null 2>&1; then
  container_names="$(podman ps --format '{{.Names}}' 2>/dev/null || true)"
  worker_count="$(printf '%s\n' "$container_names" | awk '/ceph-ai_worker_1$/ {count++} END {print count + 0}')"
  watcher_count="$(printf '%s\n' "$container_names" | awk '/ceph-ai_watcher_1$/ {count++} END {print count + 0}')"
  telegram_count="$(printf '%s\n' "$container_names" | awk '/ceph-ai_telegram-ai_1$/ {count++} END {print count + 0}')"
  if [ "$worker_count" -le 1 ] && [ "$watcher_count" -le 1 ] && [ "$telegram_count" -le 1 ]; then
    pass runtime-owner "one Podman worker/watcher/Telegram consumer"
  else
    fail runtime-owner "duplicate worker/watcher/Telegram container detected"
  fi
else
  fail runtime-owner "Podman is unavailable"
fi

legacy_active=0
for unit in ceph-ai-dashboard.service ceph-ai-worker.service ceph-ai-watcher.service ceph-ai-telegram.service; do
  if systemctl is-active --quiet "$unit" 2>/dev/null; then
    printf 'ACTIVE legacy-unit=%s\n' "$unit" >&2
    legacy_active=$((legacy_active + 1))
  fi
done
if [ "$legacy_active" -eq 0 ]; then
  pass legacy-runtime "no competing legacy service unit is active"
else
  fail legacy-runtime "$legacy_active competing legacy service unit(s) active"
fi

environment="$(env_value CEPH_AI_ENVIRONMENT)"
if [ "$environment" = production ] || [ "$environment" = staging ]; then
  pass environment "$environment"
else
  fail environment "CEPH_AI_ENVIRONMENT must explicitly be production or staging (got ${environment:-unset})"
fi

cluster_name="$(env_value CLUSTER_NAME)"
if [ -n "$cluster_name" ]; then
  pass identity "target cluster=$cluster_name"
else
  fail identity "CLUSTER_NAME is not explicitly mapped"
fi

autopilot="$(env_value AUTOPILOT_ENABLED)"
if [ "$autopilot" = false ] || [ "$autopilot" = 0 ]; then
  pass autopilot "disabled in runtime environment"
else
  fail autopilot "autopilot is ${autopilot:-unset}; explicit operator decision is required"
fi

egress_policy="$(env_value CEPH_AI_EGRESS_POLICY)"
output_policy="$(iptables -S OUTPUT 2>/dev/null | awk '$1 == "-P" {print $3; exit}' || true)"
if [ "$egress_policy" = deny-by-default ] && [ "$output_policy" = DROP ]; then
  pass egress "deny-by-default policy and OUTPUT DROP are present"
else
  fail egress "requires CEPH_AI_EGRESS_POLICY=deny-by-default and host OUTPUT DROP (policy=${egress_policy:-unset}, OUTPUT=${output_policy:-unknown})"
fi

if [ -x scripts/deploy/check_resource_budget.sh ]; then
  if scripts/deploy/check_resource_budget.sh >/dev/null 2>&1; then
    pass resources "CPU/memory/swap budget is within policy"
  else
    fail resources "resource budget preflight failed"
  fi
else
  fail resources "resource budget checker is unavailable"
fi

if [ "$failures" -eq 0 ]; then
  echo "PRODUCTION READINESS PREFLIGHT PASSED"
  exit 0
fi
echo "PRODUCTION READINESS PREFLIGHT FAILED: $failures gate(s)" >&2
exit 1
