#!/usr/bin/env bash
# Install, upgrade and run Ceph AI on a customer host (plan readiness-gap-review R8.1, 15/10/2026).
#
#   ceph-ai-customer.sh preflight --image REF [--dashboard-bind ADDR:PORT]
#   ceph-ai-customer.sh install   --image REF --ssh-key FILE [--old-env FILE] [--dashboard-bind ADDR:PORT]
#   ceph-ai-customer.sh upgrade   --image REF
#   ceph-ai-customer.sh rollback          (previous image; the database is NOT rolled back)
#   ceph-ai-customer.sh trust-hosts       (copy the host's verified SSH host keys of the Ceph nodes)
#   ceph-ai-customer.sh backup | status | start | stop
#
# REF is an approved digest, ghcr.io/<owner>/ceph-ai@sha256:<64 hex>. The stack is
# deploy/customer/compose.yaml: PostgreSQL and RabbitMQ live inside it and publish no
# port, so services the host already runs are never touched. Secrets are generated
# here, kept in 0600/0640 files and never printed; the Dashboard admin password is
# written to /root/.config/ceph-ai/dashboard-admin.pass.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$REPO_DIR/deploy/customer/compose.yaml"
CONF=/etc/ceph-ai-customer
DATA=/var/lib/ceph-ai-customer      # mounted at /var/lib/ceph-ai in the containers
RUN_DIR=/run/ceph-ai-customer       # mounted at /run/ceph-ai
LEGACY_ENV=/var/lib/ceph-ai/config/.env   # where releases before 16/10/2026 wrote ENV_FILE
ENV_FILE=$DATA/config/.env
BACKUP_DIR=/var/backups/ceph-ai
ADMIN_PASS_FILE=/root/.config/ceph-ai/dashboard-admin.pass
UNIT=/etc/systemd/system/ceph-ai-customer.service
APP_UID=10001
NETWORK=ceph-ai-customer_default
APP_SERVICES=(dashboard-web watcher remediation-watcher worker)
MIN_FREE_GIB=5
KEEP_BACKUPS=7
if [ -z "${PODMAN_COMPOSE:-}" ]; then
  if [ -x /opt/ceph-ai-customer/venv/bin/podman-compose ]; then
    PODMAN_COMPOSE=/opt/ceph-ai-customer/venv/bin/podman-compose
  else
    PODMAN_COMPOSE=podman-compose
  fi
fi

IMAGE=""
SSH_KEY=""
OLD_ENV=""
DASHBOARD_BIND="0.0.0.0:8000"

say() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf 'LỖI: %s\n' "$*" >&2; exit 1; }
failures=0
check() {  # check <name> <command...>: PASS/FAIL line, counts failures
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then printf 'PREFLIGHT PASS %s\n' "$name"
  else printf 'PREFLIGHT FAIL %s\n' "$name" >&2; failures=$((failures + 1)); fi
}

valid_image() { [[ "$1" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]]; }
load_compose_env() {
  [ -r "$CONF/compose.env" ] || die "chưa cài ($CONF/compose.env không có); dùng lệnh install"
  IMAGE="$(sed -n 's/^CEPH_AI_IMAGE=//p' "$CONF/compose.env")"
  DASHBOARD_BIND="$(sed -n 's/^CEPH_AI_DASHBOARD_BIND=//p' "$CONF/compose.env")"
}
save_compose_env() {
  umask 077
  printf 'CEPH_AI_IMAGE=%s\nCEPH_AI_DASHBOARD_BIND=%s\n' "$IMAGE" "$DASHBOARD_BIND" > "$CONF/compose.env.tmp"
  mv "$CONF/compose.env.tmp" "$CONF/compose.env"
}
compose() {
  CEPH_AI_IMAGE="$IMAGE" CEPH_AI_DASHBOARD_BIND="$DASHBOARD_BIND" \
    "$PODMAN_COMPOSE" -p ceph-ai-customer -f "$COMPOSE_FILE" "$@"
}
container() { printf 'ceph-ai-customer_%s_1' "$1"; }
local_url() {
  local host="${DASHBOARD_BIND%:*}" port="${DASHBOARD_BIND##*:}"
  [ "$host" = "0.0.0.0" ] && host=127.0.0.1
  printf 'http://%s:%s/login' "$host" "$port"
}
secret() { openssl rand -hex "$1"; }

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --image) IMAGE="${2:-}"; shift 2 ;;
      --ssh-key) SSH_KEY="${2:-}"; shift 2 ;;
      --old-env) OLD_ENV="${2:-}"; shift 2 ;;
      --dashboard-bind) DASHBOARD_BIND="${2:-}"; shift 2 ;;
      *) die "tham số lạ: $1" ;;
    esac
  done
}

# --- preflight ------------------------------------------------------------------------

cni_dns_ready() {
  # Podman 4 with netavark resolves service names itself; Podman 3 (CNI) needs the dnsname plugin.
  [ "$(podman info --format '{{.Host.NetworkBackend}}' 2>/dev/null)" = "netavark" ] && return 0
  [ -x /usr/lib/cni/dnsname ] || [ -x /opt/cni/bin/dnsname ]
}
free_gib() { df -BG --output=avail /var/lib | tail -1 | tr -dc '0-9'; }
port_free() {
  local port="${DASHBOARD_BIND##*:}"
  ! ss -ltnH "( sport = :$port )" | grep -q .
}

preflight() {
  failures=0
  check root [ "$(id -u)" = 0 ]
  check image-reference valid_image "$IMAGE"
  local command
  for command in podman curl openssl ss df sha256sum systemctl ssh-keygen python3; do
    check "command.$command" command -v "$command"
  done
  check podman-compose "$PODMAN_COMPOSE" --version
  check catatonit-for-init bash -c 'command -v catatonit || [ -x /usr/libexec/podman/catatonit ] || [ -x /usr/bin/catatonit ]'
  check service-name-dns cni_dns_ready
  check "dashboard-port-free(${DASHBOARD_BIND})" port_free
  check cgroup-v2 bash -c '[ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ]'
  check "disk-free>=${MIN_FREE_GIB}GiB" bash -c "[ \"\$(df -BG --output=avail /var/lib | tail -1 | tr -dc '0-9')\" -ge $MIN_FREE_GIB ]"
  check registry-network curl -sS -o /dev/null --max-time 10 https://ghcr.io/v2/
  printf 'PREFLIGHT INFO podman=%s free=%sGiB dashboard=%s\n' "$(podman --version 2>/dev/null | awk '{print $3}')" "$(free_gib)" "$DASHBOARD_BIND"
  [ "$failures" -eq 0 ] || die "preflight có $failures mục FAIL"
}

# --- shared steps ---------------------------------------------------------------------

wait_for() {  # wait_for <seconds> <description> <command...>
  local seconds="$1" what="$2"; shift 2
  local deadline=$((SECONDS + seconds))
  until "$@" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || die "$what không sẵn sàng sau ${seconds}s"
    sleep 3
  done
}
ensure_network() {
  # Podman 3.4 on Ubuntu 22.04 writes CNI configs with cniVersion 1.0.0, which its own
  # containernetworking-plugins 0.9.1 reject ("plugin firewall does not support config
  # version"). Create the stack's network here and lower the version when needed;
  # podman-compose reuses an existing network.
  [ "$(podman info --format '{{.Host.NetworkBackend}}' 2>/dev/null)" = "netavark" ] && return 0
  podman network exists "$NETWORK" \
    || podman network create --label io.podman.compose.project=ceph-ai-customer "$NETWORK" >/dev/null
  local conflist="/etc/cni/net.d/$NETWORK.conflist"
  [ ! -f "$conflist" ] || python3 "$REPO_DIR/scripts/customer/cni_compat.py" "$conflist" \
    || die "cấu hình mạng CNI $conflist không dùng được với plugin CNI của máy"
}
release_stale_dns() {
  # Podman 3.4's dnsname plugin runs one dnsmasq per network. On 10.2.0.94 (16/10/2026) one
  # outlived `stop` while Podman lost track of it, and the next start failed with "dnsmasq:
  # failed to create listening socket for 10.89.0.1: Address already in use". With no
  # container left on the network, stop that network's dnsmasq and drop its state.
  [ "$(podman info --format '{{.Host.NetworkBackend}}' 2>/dev/null)" = "netavark" ] && return 0
  if podman ps -q --filter "network=$NETWORK" 2>/dev/null | grep -q .; then return 0; fi
  local state="/run/containers/cni/dnsname/$NETWORK" pids
  pids="$(ps -eo pid=,args= | awk -v conf="$state/dnsmasq.conf" '$2 ~ /dnsmasq$/ && index($0, conf) {print $1}')"
  if [ -n "$pids" ]; then
    say "dừng dnsmasq cũ của mạng $NETWORK (pid $pids)"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
  fi
  rm -rf "$state"
}
dns_port_free() {
  local gateway
  gateway="$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c["plugins"][0]["ipam"]["ranges"][0][0]["gateway"])' \
             "/etc/cni/net.d/$NETWORK.conflist" 2>/dev/null)" || return 0
  [ -n "$gateway" ] || return 0
  if ss -lunpH "( sport = :53 )" | grep -F "$gateway:53" >&2; then
    die "cổng DNS $gateway:53 của mạng $NETWORK đang bị tiến trình trên giữ; không khởi động"
  fi
}
start_backends() {
  ensure_network
  release_stale_dns
  dns_port_free
  compose up -d postgres rabbitmq
  wait_for 120 PostgreSQL podman exec "$(container postgres)" pg_isready -U ceph_ai -d ceph_ai
  wait_for 180 RabbitMQ podman exec "$(container rabbitmq)" rabbitmq-diagnostics -q ping
}
check_env_against_image() {
  # extra="forbid": a key the image does not know crash-loops every container.
  # Loaded as the settings file (not as environment variables, which Settings would
  # silently ignore when unknown).
  podman run --rm --network none --user 0 -v "$ENV_FILE:/in/.env:ro" -e CEPH_AI_ENV_FILE=/in/.env "$IMAGE" \
    python -c 'from config.settings import Settings; Settings()' \
    || die "$ENV_FILE có khoá mà image này không nhận; không khởi động"
}
migrate() {
  say "migration"
  compose run --rm dashboard-web python -m alembic upgrade head
}
smoke() {
  say "kiểm tra sau khởi động"
  wait_for 240 "Dashboard ($(local_url))" curl -fsS -o /dev/null "$(local_url)"
  sleep 45  # let a crash-looping service show itself
  local service bad=0 state restarts
  for service in postgres rabbitmq "${APP_SERVICES[@]}"; do
    state="$(podman inspect "$(container "$service")" --format '{{.State.Status}}' 2>/dev/null || echo missing)"
    restarts="$(podman inspect "$(container "$service")" --format '{{.RestartCount}}' 2>/dev/null || echo ?)"
    printf 'SMOKE %-20s %s restarts=%s\n' "$service" "$state" "$restarts"
    if [ "$state" != running ] || [ "$restarts" != 0 ]; then bad=$((bad + 1)); fi
  done
  [ "$bad" -eq 0 ] || die "$bad dịch vụ không ổn định; xem: podman logs <container>"
  say "OK: $(local_url)"
}
runtime_dir() {
  # Heartbeats and the watchers' singleton locks live here, written by uid 10001. Create it
  # before any container: otherwise Podman creates the bind-mounted host dir as root 0700 and
  # the watchers crash-loop on "Permission denied: '/run/ceph-ai/watcher.lock'" (Podman 3.4
  # test, 15/10/2026). The tmpfiles entry recreates it after a reboot.
  printf 'd %s 0750 %s %s -\n' "$RUN_DIR" "$APP_UID" "$APP_UID" > /etc/tmpfiles.d/ceph-ai-customer.conf
  install -d -m 0750 -o "$APP_UID" -g "$APP_UID" "$RUN_DIR"
  chown -R "$APP_UID:$APP_UID" "$RUN_DIR"; chmod 0750 "$RUN_DIR"
}
data_ownership() {
  # Everything the application writes belongs to uid 10001; only the settings file stays
  # root-owned and group-readable. Re-applied on every start, so a file a root process left
  # behind cannot crash-loop a service.
  chown -R "$APP_UID:$APP_UID" "$DATA"
  if [ -e "$ENV_FILE" ]; then chown "root:$APP_UID" "$ENV_FILE"; chmod 0640 "$ENV_FILE"; fi
}
adopt_legacy_env() {
  # An unfinished install of an earlier release left its generated settings (and the
  # PostgreSQL password they hold) at $LEGACY_ENV. Move them, never a file we did not write.
  [ ! -e "$ENV_FILE" ] && [ -f "$LEGACY_ENV" ] || return 0
  head -1 "$LEGACY_ENV" | grep -q '^# Generated by scripts/customer/build_env.py' || return 0
  install -d -m 0750 -o "$APP_UID" -g "$APP_UID" "$DATA" "$DATA/config"
  mv "$LEGACY_ENV" "$ENV_FILE"
  say "chuyển $LEGACY_ENV (do bản cài trước sinh) sang $ENV_FILE"
}
install_unit() {
  cat > "$UNIT" <<EOF
[Unit]
Description=Ceph AI (customer stack, deploy/customer/compose.yaml)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$REPO_DIR/scripts/customer/ceph-ai-customer.sh start
ExecStop=$REPO_DIR/scripts/customer/ceph-ai-customer.sh stop
TimeoutStartSec=900
TimeoutStopSec=300

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable ceph-ai-customer.service
}

# --- commands -------------------------------------------------------------------------

cmd_install() {
  [ ! -e "$DATA/release-artifacts/current-image-ref" ] || die "máy này đã cài xong; dùng upgrade"
  adopt_legacy_env
  local resume=0
  if [ -e "$ENV_FILE" ]; then
    # A previous install stopped before the stack ran (e.g. a network error): keep the
    # passwords and .env it generated, since PostgreSQL may already hold that password.
    [ -r "$CONF/postgres.env" ] && [ -r "$CONF/rabbitmq.env" ] && [ -r "$ADMIN_PASS_FILE" ] \
      || die "$ENV_FILE có nhưng thiếu file mật khẩu trong $CONF; không tự sinh lại"
    resume=1
    # Containers of the unfinished run (they may still hold the Dashboard port); data stays on disk.
    compose down >/dev/null 2>&1 || true
  fi
  [ -r "$SSH_KEY" ] || die "--ssh-key phải là file khoá SSH đọc được"
  [ -z "$OLD_ENV" ] || [ -r "$OLD_ENV" ] || die "--old-env không đọc được"
  preflight

  say "thư mục và quyền"
  install -d -m 0700 "$CONF" "$CONF/ssh" /root/.config/ceph-ai "$BACKUP_DIR"
  # The images' entrypoints chown their own data subdirectory to the database user, who
  # must be able to traverse these: 0755, not 0700 (compose.yaml, postgres service).
  install -d -m 0755 /var/lib/ceph-ai-postgres /var/lib/ceph-ai-rabbitmq
  install -d -m 0750 -o "$APP_UID" -g "$APP_UID" "$DATA" "$DATA/config" "$DATA/release-artifacts"
  install -o "$APP_UID" -g "$APP_UID" -m 0400 "$SSH_KEY" "$CONF/ssh/id_ed25519"
  runtime_dir
  data_ownership

  say "tải image"
  podman pull "$IMAGE"
  podman pull docker.io/library/postgres:18
  podman pull docker.io/library/rabbitmq:3.13-management

  if [ "$resume" = 1 ]; then
    say "tiếp tục lần cài trước: dùng lại mật khẩu và $ENV_FILE đã sinh"
  else
    generate_config
  fi
  check_env_against_image
  cmd_install_start
}
generate_config() {
  say "sinh mật khẩu (không in ra)"
  local db_password rabbitmq_password admin_password password_hash
  db_password="$(secret 24)"; rabbitmq_password="$(secret 24)"; admin_password="$(secret 12)"
  (umask 077
   printf 'POSTGRES_PASSWORD=%s\n' "$db_password" > "$CONF/postgres.env"
   printf 'RABBITMQ_DEFAULT_USER=ceph_ai\nRABBITMQ_DEFAULT_PASS=%s\n' "$rabbitmq_password" > "$CONF/rabbitmq.env"
   printf '%s\n' "$admin_password" > "$ADMIN_PASS_FILE")
  password_hash="$(printf '%s' "$admin_password" | podman run --rm -i --network none "$IMAGE" \
          python -c 'import bcrypt, sys; print(bcrypt.hashpw(sys.stdin.read().encode(), bcrypt.gensalt()).decode())')"

  say "dựng $ENV_FILE"
  local old_mount=()
  [ -z "$OLD_ENV" ] || old_mount=(-v "$(readlink -f "$OLD_ENV"):/in/old.env:ro")
  printf '{"db_password": "%s", "rabbitmq_password": "%s", "session_secret": "%s", "dashboard_password_hash": "%s"}' \
      "$db_password" "$rabbitmq_password" "$(secret 32)" "$password_hash" \
    | podman run --rm -i --network none --user 0 -v "$DATA/config:/out:rw" "${old_mount[@]}" "$IMAGE" \
        python -m scripts.customer.build_env --out /out/.env ${OLD_ENV:+--old-env /in/old.env}
  chown "root:$APP_UID" "$ENV_FILE"; chmod 0640 "$ENV_FILE"
}
cmd_install_start() {
  save_compose_env
  data_ownership
  cmd_trust_hosts
  start_backends
  migrate
  say "khởi động ứng dụng"
  compose up -d "${APP_SERVICES[@]}"
  smoke
  # Only now is the install finished: a failed smoke test leaves `install` re-runnable.
  printf '%s\n' "$IMAGE" > "$DATA/release-artifacts/current-image-ref"
  install_unit
  say "Mật khẩu admin Dashboard ở $ADMIN_PASS_FILE (đọc rồi đổi trong Settings nếu cần). Autopilot đang TẮT."
}

cmd_backup() {
  load_compose_env
  install -d -m 0700 "$BACKUP_DIR"
  local file="$BACKUP_DIR/ceph_ai-$(date -u +%Y%m%dT%H%M%SZ).dump"
  say "sao lưu DB → $file"
  # Dump and verify inside the container, then copy the file out. Podman 3.4 truncates binary
  # output streamed through `podman exec` (a 343 kB dump arrived as 106496 bytes, 15/10/2026).
  local pg inside=/tmp/ceph-ai-backup.dump
  pg="$(container postgres)"
  podman exec "$pg" pg_dump -U ceph_ai -Fc ceph_ai -f "$inside"
  podman exec "$pg" pg_restore --list "$inside" > /dev/null || die "bản dump không đọc lại được"
  podman cp "$pg:$inside" "$file.tmp"
  podman exec "$pg" rm -f "$inside"
  [ "$(stat -c %s "$file.tmp")" -gt 0 ] || die "bản dump rỗng"
  mv "$file.tmp" "$file"; chmod 0600 "$file"
  sha256sum "$file" > "$file.sha256"
  ls -1t "$BACKUP_DIR"/ceph_ai-*.dump | tail -n +$((KEEP_BACKUPS + 1)) | while read -r old; do rm -f "$old" "$old.sha256"; done
  say "OK $(du -h "$file" | cut -f1)"
}

cmd_upgrade() {
  local target="$IMAGE"
  load_compose_env
  valid_image "$target" || die "--image phải là ghcr.io/...@sha256:<digest>"
  [ "$target" != "$IMAGE" ] || die "đang chạy đúng image này rồi"
  local previous="$IMAGE"
  IMAGE="$target"
  preflight_upgrade
  cmd_backup
  podman pull "$IMAGE"
  check_env_against_image
  data_ownership
  migrate
  printf '%s\n' "$previous" > "$DATA/release-artifacts/previous-image-ref"
  save_compose_env
  say "tạo lại container ứng dụng"
  compose up -d --force-recreate "${APP_SERVICES[@]}"
  printf '%s\n' "$IMAGE" > "$DATA/release-artifacts/current-image-ref"
  smoke
}
preflight_upgrade() {
  failures=0
  check root [ "$(id -u)" = 0 ]
  check image-reference valid_image "$IMAGE"
  check podman-compose "$PODMAN_COMPOSE" --version
  check "disk-free>=${MIN_FREE_GIB}GiB" bash -c "[ \"\$(df -BG --output=avail /var/lib | tail -1 | tr -dc '0-9')\" -ge $MIN_FREE_GIB ]"
  check postgres-running podman exec "$(container postgres)" pg_isready -U ceph_ai -d ceph_ai
  [ "$failures" -eq 0 ] || die "preflight có $failures mục FAIL"
}

cmd_rollback() {
  load_compose_env
  local previous
  previous="$(cat "$DATA/release-artifacts/previous-image-ref" 2>/dev/null || true)"
  valid_image "$previous" || die "không có previous-image-ref hợp lệ"
  [ "${CEPH_AI_ROLLBACK_ACK_COMPATIBLE_SCHEMA:-}" = yes ] \
    || die "rollback không đảo migration; đặt CEPH_AI_ROLLBACK_ACK_COMPATIBLE_SCHEMA=yes nếu đã chắc schema tương thích"
  IMAGE="$previous"
  save_compose_env
  compose up -d --force-recreate "${APP_SERVICES[@]}"
  printf '%s\n' "$IMAGE" > "$DATA/release-artifacts/current-image-ref"
  smoke
}

cmd_start() {
  load_compose_env
  runtime_dir
  data_ownership
  start_backends
  compose up -d "${APP_SERVICES[@]}"
}
cmd_stop() { load_compose_env; compose down; release_stale_dns; }

node_hosts() {
  # Every node the settings name (CEPH_*_NODES, CEPH_MON_HOSTNAMES): comma or space separated.
  grep -E '^CEPH_[A-Z_]*(NODES|HOSTNAMES)=' "$ENV_FILE" | cut -d= -f2- | tr ', ' '\n\n' | sed '/^$/d' | sort -u
}
cmd_trust_hosts() {
  # The application only connects to hosts whose key is in this file (paramiko RejectPolicy).
  # Copy the keys this host already verified, never trust on first use: a node missing from
  # /root/.ssh/known_hosts is reported, and the operator verifies it first.
  [ -r "$ENV_FILE" ] || die "chưa có $ENV_FILE"
  local source="${KNOWN_HOSTS_SOURCE:-/root/.ssh/known_hosts}" target="$DATA/ssh/ceph_lab_known_hosts"
  local temporary host found=0 missing=()
  install -d -m 0750 -o "$APP_UID" -g "$APP_UID" "$DATA/ssh"
  temporary="$(mktemp "$DATA/ssh/.known_hosts.XXXXXX")"
  [ ! -f "$target" ] || cat "$target" > "$temporary"
  while read -r host; do
    if [ -r "$source" ] && ssh-keygen -F "$host" -f "$source" > "$temporary.match" 2>/dev/null; then
      grep -v '^#' "$temporary.match" >> "$temporary"; found=$((found + 1))
    else
      missing+=("$host")
    fi
  done < <(node_hosts)
  rm -f "$temporary.match"
  sort -u "$temporary" -o "$temporary"
  chown "$APP_UID:$APP_UID" "$temporary"; chmod 0640 "$temporary"
  mv "$temporary" "$target"
  say "host key đã chép cho $found node vào $target"
  if [ "${#missing[@]}" -gt 0 ]; then
    printf 'CHƯA CÓ host key (xác minh rồi `ssh-keyscan` hoặc SSH một lần từ host, sau đó chạy lại trust-hosts): %s\n' "${missing[*]}" >&2
  fi
}
cmd_status() {
  load_compose_env
  printf 'image: %s\ndashboard: %s\n' "$IMAGE" "$(local_url)"
  podman ps -a --filter name=ceph-ai-customer_ --format '{{.Names}}\t{{.Status}}'
}

command="${1:-}"; shift || true
parse_args "$@"
case "$command" in
  preflight) preflight ;;
  install) cmd_install ;;
  upgrade) cmd_upgrade ;;
  rollback) cmd_rollback ;;
  backup) cmd_backup ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  trust-hosts) cmd_trust_hosts ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
