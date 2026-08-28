#!/usr/bin/env bash
# Redeploys this exact checkout: pulls latest main, installs any new
# dependencies, applies pending DB migrations, and restarts the four
# long-running services (watcher, AI remediation watcher, worker, dashboard)
# using the systemd units for the canonical checkout, with the legacy
# `nohup ... & disown` path retained for older hosts and sibling checkouts.
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

# Refresh once during deployment so a newly installed checkout does not wait
# for the first timer tick. A network/catalog failure is non-fatal because
# the cost dashboard can use the last validated snapshot or built-in prices.
if ! "$VENV_PYTHON" -m scripts.update_ai_pricing; then
  echo "WARNING: AI pricing refresh failed; retaining the previous snapshot/fallback prices"
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

# Optional server-local override (gitignored, never committed — this repo
# may end up on a public remote and must not hardcode this box's real bind
# address) — e.g. `echo 'DASHBOARD_HOST=103.69.193.220' >
# scripts/deploy/deploy.local.env` once, on this server only. When running
# a 2nd cluster's checkout on the SAME server, its own deploy.local.env
# MUST set a different DASHBOARD_PORT — otherwise the two instances fight
# over the same port.
if [ -f "$REPO_DIR/scripts/deploy/deploy.local.env" ]; then
  # shellcheck disable=SC1091
  source "$REPO_DIR/scripts/deploy/deploy.local.env"
fi
DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"
if ! [[ "$DASHBOARD_PORT" =~ ^[0-9]+$ ]] || [ "$DASHBOARD_PORT" -lt 1 ] || [ "$DASHBOARD_PORT" -gt 65535 ]; then
  echo "ERROR: DASHBOARD_PORT must be an integer between 1 and 65535."
  exit 2
fi
if ! [[ "$DASHBOARD_HOST" =~ ^[A-Za-z0-9_.:-]+$ ]]; then
  echo "ERROR: DASHBOARD_HOST contains unsupported characters."
  exit 2
fi

# Keep the RabbitMQ network restriction persistent across reboots. This is a
# host-wide rule, so install it independently of which checkout is deployed.
SYSTEMD_AVAILABLE=false
if command -v systemctl >/dev/null 2>&1 && \
   [ -d /run/systemd/system ] && \
   systemctl show --property=Version --value >/dev/null 2>&1; then
  SYSTEMD_AVAILABLE=true
fi
if [ "$SYSTEMD_AVAILABLE" = "true" ] && \
   [ -f "$REPO_DIR/scripts/deploy/ceph-ai-firewall.sh" ] && \
   [ -f "$REPO_DIR/scripts/deploy/systemd/ceph-ai-firewall.service" ]; then
  install -m 0755 "$REPO_DIR/scripts/deploy/ceph-ai-firewall.sh" \
    /usr/local/sbin/ceph-ai-firewall
  install -m 0644 "$REPO_DIR/scripts/deploy/systemd/ceph-ai-firewall.service" \
    /etc/systemd/system/ceph-ai-firewall.service
  if [ -f "$REPO_DIR/scripts/deploy/systemd/ceph-ai-firewall-reconcile.service" ] && \
     [ -f "$REPO_DIR/scripts/deploy/systemd/ceph-ai-firewall.timer" ]; then
    install -m 0644 "$REPO_DIR/scripts/deploy/systemd/ceph-ai-firewall-reconcile.service" \
      /etc/systemd/system/ceph-ai-firewall-reconcile.service
    install -m 0644 "$REPO_DIR/scripts/deploy/systemd/ceph-ai-firewall.timer" \
      /etc/systemd/system/ceph-ai-firewall.timer
  fi
  systemctl daemon-reload
  systemctl enable --now ceph-ai-firewall.service
  if [ -f "$REPO_DIR/scripts/deploy/systemd/ceph-ai-firewall-reconcile.service" ] && \
     [ -f "$REPO_DIR/scripts/deploy/systemd/ceph-ai-firewall.timer" ]; then
    systemctl enable --now ceph-ai-firewall.timer
  fi
fi

# Keep the price snapshot fresh independently of the Watcher poll loop. The
# oneshot is safe to retry: failed downloads leave the last valid cache intact.
if [ "$SYSTEMD_AVAILABLE" = "true" ] && \
   [ -f "$REPO_DIR/scripts/deploy/systemd/ceph-ai-ai-pricing.service" ] && \
   [ -f "$REPO_DIR/scripts/deploy/systemd/ceph-ai-ai-pricing.timer" ]; then
  install -m 0644 "$REPO_DIR/scripts/deploy/systemd/ceph-ai-ai-pricing.service" \
    /etc/systemd/system/ceph-ai-ai-pricing.service
  install -m 0644 "$REPO_DIR/scripts/deploy/systemd/ceph-ai-ai-pricing.timer" \
    /etc/systemd/system/ceph-ai-ai-pricing.timer
  systemctl daemon-reload
  systemctl enable --now ceph-ai-ai-pricing.timer
fi

echo "==> Stopping existing services (if running)"
USE_SYSTEMD=false
SYSTEMD_CORE_UNITS=(
  ceph-ai-watcher.service
  ceph-ai-remediation-watcher.service
  ceph-ai-worker.service
  ceph-ai-dashboard.service
)
SYSTEMD_REPAIR_UNIT=ceph-ai-code-repair-supervisor.service
INSTALLED_SYSTEMD_UNITS=()
REPAIR_USES_SYSTEMD=false
if [ "$SYSTEMD_AVAILABLE" = "true" ] && [ "$REPO_DIR" = "/root/ceph-ai" ]; then
  for unit in "${SYSTEMD_CORE_UNITS[@]}" "$SYSTEMD_REPAIR_UNIT"; do
    if systemctl cat "$unit" >/dev/null 2>&1; then
      INSTALLED_SYSTEMD_UNITS+=("$unit")
    fi
  done
fi
if [ "${#INSTALLED_SYSTEMD_UNITS[@]}" -ge "${#SYSTEMD_CORE_UNITS[@]}" ] && \
   systemctl cat "${SYSTEMD_CORE_UNITS[@]}" >/dev/null 2>&1; then
  USE_SYSTEMD=true
  # Keep the source templates and installed units synchronized.  This makes
  # hardening changes (and the remediation singleton runtime directory)
  # survive the next deployment instead of being overwritten by an older
  # /etc/systemd/system copy.  The dashboard's host/port override is applied
  # separately through the drop-in below.
  for unit in "${SYSTEMD_CORE_UNITS[@]}"; do
    install -m 0644 "$REPO_DIR/scripts/deploy/systemd/$unit" \
      "/etc/systemd/system/$unit"
  done
  if [ -f "$REPO_DIR/scripts/deploy/systemd/$SYSTEMD_REPAIR_UNIT" ] || \
     systemctl cat "$SYSTEMD_REPAIR_UNIT" >/dev/null 2>&1; then
    if [ -f "$REPO_DIR/scripts/deploy/systemd/$SYSTEMD_REPAIR_UNIT" ]; then
      install -m 0644 "$REPO_DIR/scripts/deploy/systemd/$SYSTEMD_REPAIR_UNIT" \
        "/etc/systemd/system/$SYSTEMD_REPAIR_UNIT"
    fi
    REPAIR_USES_SYSTEMD=true
  fi
  # The checked-in unit keeps the safe default for static verification.  A
  # drop-in applies this checkout's server-local dashboard override without
  # mutating the tracked unit or leaving a stale port behind.
  DASHBOARD_DROPIN_DIR=/etc/systemd/system/ceph-ai-dashboard.service.d
  mkdir -p "$DASHBOARD_DROPIN_DIR"
  DASHBOARD_DROPIN="$DASHBOARD_DROPIN_DIR/10-deploy.conf"
  DASHBOARD_DROPIN_TMP="$(mktemp "$DASHBOARD_DROPIN_DIR/.10-deploy.conf.XXXXXX")"
  {
    printf '%s\n' '[Service]' 'ExecStart='
    printf 'ExecStart=%s -m uvicorn dashboard.app:app --host %s --port %s\n' \
      "$VENV_PYTHON" "$DASHBOARD_HOST" "$DASHBOARD_PORT"
  } > "$DASHBOARD_DROPIN_TMP"
  chmod 0644 "$DASHBOARD_DROPIN_TMP"
  mv -f "$DASHBOARD_DROPIN_TMP" "$DASHBOARD_DROPIN"
  systemctl daemon-reload
  # The repair supervisor owns candidate promotion and must survive a
  # candidate deployment; the other four services are restarted below.
  systemctl stop ceph-ai-watcher ceph-ai-remediation-watcher ceph-ai-worker ceph-ai-dashboard || true
elif [ "${#INSTALLED_SYSTEMD_UNITS[@]}" -gt 0 ]; then
  # Transitional host: stop every installed unit before falling back to
  # nohup, otherwise a partial systemd installation would duplicate whichever
  # watcher/worker units already exist.
  echo "==> Partial systemd installation detected; stopping installed units"
  for unit in "${INSTALLED_SYSTEMD_UNITS[@]}"; do
    systemctl stop "${unit%.service}" || true
  done
fi
# Match by checkout cwd as well as module name.  Older deployments launched
# the interpreter as relative `.venv/bin/python`; an absolute-path-only pkill
# missed those processes and left two workers consuming the same queue.
stop_checkout_services() {
  local signal="${1:-TERM}" proc_dir proc_cwd proc_cmd
  for proc_dir in /proc/[0-9]*; do
    proc_cwd="$(readlink "$proc_dir/cwd" 2>/dev/null || true)"
    [ "$proc_cwd" = "$REPO_DIR" ] || continue
    proc_cmd="$(tr '\0' ' ' < "$proc_dir/cmdline" 2>/dev/null || true)"
    if [[ "$proc_cmd" =~ -m[[:space:]]+(watcher\.main|watcher\.remediation_main|worker\.main|uvicorn[[:space:]]+dashboard\.app:app) ]]; then
      kill -"$signal" "${proc_dir##*/}" 2>/dev/null || true
    fi
  done
}
stop_checkout_services TERM
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
stop_checkout_services KILL

echo "==> Starting services"
if [ "$USE_SYSTEMD" = "true" ]; then
  systemctl start ceph-ai-watcher ceph-ai-remediation-watcher ceph-ai-worker ceph-ai-dashboard
else
  nohup "$VENV_PYTHON" -m watcher.main >> "/var/log/${LOG_TAG}-watcher.log" 2>&1 &
  disown
  nohup "$VENV_PYTHON" -m worker.main >> "/var/log/${LOG_TAG}-worker.log" 2>&1 &
  disown
  nohup "$VENV_PYTHON" -m uvicorn dashboard.app:app --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT" \
    >> "/var/log/${LOG_TAG}-dashboard.log" 2>&1 &
  disown
  nohup "$VENV_PYTHON" -m watcher.remediation_main >> "/var/log/${LOG_TAG}-remediation-watcher.log" 2>&1 &
  disown
fi

# The repair supervisor must survive candidate deployments because it owns the
# test/deploy/promote decision. Under systemd it is left running; on a legacy
# host start it only if no matching process is present.
if [ "$USE_SYSTEMD" = "true" ] && [ "$REPAIR_USES_SYSTEMD" = "true" ]; then
  systemctl is-active --quiet ceph-ai-code-repair-supervisor.service || \
    systemctl start ceph-ai-code-repair-supervisor.service
else
  if ! pgrep -f "$VENV_PYTHON -m worker.code_repair_supervisor" >/dev/null 2>&1; then
    nohup "$VENV_PYTHON" -m worker.code_repair_supervisor \
      >> "/var/log/${LOG_TAG}-code-repair-supervisor.log" 2>&1 &
    disown
  fi
fi

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
if [ "$USE_SYSTEMD" = "true" ]; then
  for unit in ceph-ai-watcher ceph-ai-remediation-watcher ceph-ai-worker ceph-ai-dashboard; do
    if ! systemctl is-active --quiet "$unit.service"; then
      echo "ERROR: $unit is not active after deployment."
      exit 1
    fi
  done
fi

echo "==> Deploy complete: $(date -u +%FT%TZ)"
echo "==> Dashboard route check: /pgs HTTP $DASHBOARD_ROUTE_STATUS"
echo "==> Running processes:"
pgrep -fa "$VENV_PYTHON -m watcher\.(main|remediation_main)|$VENV_PYTHON -m worker\.(main|code_repair_supervisor)|$VENV_PYTHON -m uvicorn dashboard.app" || echo "WARNING: no matching processes found after restart"
