#!/usr/bin/env bash
# Rehearse the customer installer on one operating system (plan R8.7, 16/10/2026).
#
#   ci_rehearsal.sh <os-image> <old-image-ref> <new-image-ref>
#
# Starts a privileged container of <os-image>, installs that OS's own Podman and podman-compose
# 1.0.6 the way deploy/customer/README.md tells a customer to, copies this checkout's tracked
# files into it and drives scripts/customer/ceph-ai-customer.sh through the sequence that broke
# on 10.2.0.94 (15-16/10/2026): install the old image from an SSH-like session
# (XDG_RUNTIME_DIR set) → upgrade → rollback → upgrade → stop from the SSH-like session and
# start from a systemd-like one → backup. After each step: six containers running, the
# application on the expected image without restarts, Dashboard /login 200, at most one dnsmasq.
#
# The container has no systemd: systemctl and systemd-tmpfiles are stubs, and Podman uses the
# cgroupfs manager. REHEARSAL_ENGINE picks docker (CI) or podman; REHEARSAL_SKIP_CGROUP_CHECK=1
# drops the installer's cgroup-v2 check in the copy, for a cgroup-v1 machine (never in CI).
set -euo pipefail

OS_IMAGE="${1:?os image}"
OLD="${2:?old image reference}"
NEW="${3:?new image reference}"
[ "$OLD" != "$NEW" ] || { echo "old and new image must differ" >&2; exit 2; }
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENGINE="${REHEARSAL_ENGINE:-docker}"
NAME="ceph-ai-rehearsal-$$"
STORAGE="$(mktemp -d "${RUNNER_TEMP:-/tmp}/ceph-ai-rehearsal.XXXXXX")"
S=/root/ceph-ai/scripts/customer/ceph-ai-customer.sh

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "::group::diagnostics"
    "$ENGINE" exec "$NAME" bash -c 'podman ps -a --format "{{.Names}} {{.Status}}";
      for c in $(podman ps -a --format "{{.Names}}"); do echo "== $c"; podman logs --tail 30 "$c" 2>&1; done' || true
    echo "::endgroup::"
  fi
  "$ENGINE" rm -f "$NAME" >/dev/null 2>&1 || true
  # Files under the storage dir belong to the container's root.
  "$ENGINE" run --rm -v "$STORAGE:/s" "$OS_IMAGE" rm -rf /s/storage /s/overlay /s/volumes >/dev/null 2>&1 || true
  rm -rf "$STORAGE" 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT

inside() { "$ENGINE" exec "$NAME" bash -c "$1"; }
inside_ssh() { "$ENGINE" exec -e XDG_RUNTIME_DIR=/run/user/0 "$NAME" bash -c "$1"; }
step() { printf '\n=== %s (%s)\n' "$1" "$(date -u +%H:%M:%S)"; }

verify() {  # verify <expected image> <label>
  inside "
    set -e
    image=\$($S status | sed -n 's/^image: //p')
    [ \"\$image\" = '$1' ] || { echo \"FAIL $2: status shows \$image\"; exit 1; }
    running=\$(podman ps -q --filter name=ceph-ai-customer_ | wc -l)
    [ \"\$running\" -eq 6 ] || { echo \"FAIL $2: \$running containers running\"; exit 1; }
    want=\$(podman image inspect '$1' --format '{{.Id}}')
    for c in dashboard-web watcher remediation-watcher worker; do
      [ \"\$(podman inspect ceph-ai-customer_\${c}_1 --format '{{.Image}}')\" = \"\$want\" ] || { echo \"FAIL $2: \$c not on the image\"; exit 1; }
      [ \"\$(podman inspect ceph-ai-customer_\${c}_1 --format '{{.RestartCount}}')\" = 0 ] || { echo \"FAIL $2: \$c restarted\"; exit 1; }
    done
    code=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/login)
    [ \"\$code\" = 200 ] || { echo \"FAIL $2: Dashboard \$code\"; exit 1; }
    dns=\$(ps -eo args | grep -c '[d]nsmasq -u' || true)
    [ \"\$dns\" -le 1 ] || { echo \"FAIL $2: \$dns dnsmasq processes\"; exit 1; }
    echo \"OK $2: 6 running, image ok, no restarts, Dashboard 200, dnsmasq=\$dns\"
  "
}

step "start $OS_IMAGE"
"$ENGINE" run -d --privileged --name "$NAME" -v "$STORAGE:/var/lib/containers" "$OS_IMAGE" sleep infinity >/dev/null
git -C "$REPO" ls-files -z | tar -C "$REPO" --null -T - -cf - \
  | "$ENGINE" exec -i "$NAME" bash -c "mkdir -p /root/ceph-ai && tar -C /root/ceph-ai -xf -"
[ "${REHEARSAL_SKIP_CGROUP_CHECK:-0}" != 1 ] || inside "sed -i '/check cgroup-v2/d' $S"

step "prepare the host as deploy/customer/README.md says"
inside '
set -e
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null
  apt-get install -y -qq podman catatonit python3-venv curl openssl iproute2 ca-certificates openssh-client procps >/dev/null
  apt-get install -y -qq golang-github-containernetworking-plugin-dnsname >/dev/null 2>&1 || true
else
  # RHEL 9 podman ships catatonit itself (/usr/libexec/podman/catatonit).
  dnf install -y -q podman python3 openssl iproute openssh-clients procps-ng >/dev/null   # curl: curl-minimal is preinstalled
fi
python3 -m venv /opt/ceph-ai-customer/venv
/opt/ceph-ai-customer/venv/bin/pip install -q podman-compose==1.0.6
mkdir -p /etc/containers /run/user/0 && chmod 0700 /run/user/0
printf "[engine]\ncgroup_manager = \"cgroupfs\"\nevents_logger = \"file\"\n" > /etc/containers/containers.conf
for tool in systemctl systemd-tmpfiles; do
  printf "#!/bin/sh\necho \"[stub %s] \$*\" >&2\n" "$tool" > /usr/local/bin/$tool; chmod +x /usr/local/bin/$tool
done
ssh-keygen -q -t ed25519 -N "" -f /root/test-key
mkdir -p /root/.ssh && echo "192.0.2.10 $(cut -d" " -f1,2 /root/test-key.pub)" > /root/.ssh/known_hosts
printf "CLUSTER_NAME=REHEARSAL\nCEPH_MON_NODES=192.0.2.10\nCEPH_EXEC_MODE=docker\nSSH_USER=root\n" > /root/old.env
printf "%s\n" "rehearsal-enroll-password-0123456789" > /root/enroll.pass
echo "podman $(podman --version | cut -d" " -f3), network backend $(podman info --format "{{.Host.NetworkBackend}}" 2>/dev/null || echo cni)"
'

step "install the old image from an SSH-like session"
inside_ssh "$S install --image $OLD --ssh-key /root/test-key --old-env /root/old.env --enroll-password-file /root/enroll.pass --dashboard-bind 0.0.0.0:8001"
verify "$OLD" install
inside 'test "$(stat -c %a:%u /var/lib/ceph-ai-customer/config/federation-enroll.pass)" = 600:10001 && echo "OK enroll password stored 0600 for 10001"'

step "upgrade to the new image"
inside "$S upgrade --image $NEW"
verify "$NEW" upgrade

step "rollback to the old image"
inside "CEPH_AI_ROLLBACK_ACK_COMPATIBLE_SCHEMA=yes $S rollback"
verify "$OLD" rollback

step "upgrade to the new image again"
inside "$S upgrade --image $NEW"
verify "$NEW" "second upgrade"

step "stop from an SSH-like session, start from a systemd-like one"
inside_ssh "$S stop"
inside "$S start"
inside "for i in \$(seq 1 60); do curl -fs -o /dev/null http://127.0.0.1:8001/login && break; sleep 3; done"
verify "$NEW" "stop/start"

step "backup"
inside "
set -e
$S backup
file=\$(ls -1t /var/backups/ceph-ai/ceph_ai-*.dump | head -1)
sha256sum -c \"\$file.sha256\"
podman cp \"\$file\" ceph-ai-customer_postgres_1:/tmp/check.dump
entries=\$(podman exec ceph-ai-customer_postgres_1 pg_restore --list /tmp/check.dump | wc -l)
podman exec ceph-ai-customer_postgres_1 rm -f /tmp/check.dump
[ \"\$entries\" -gt 100 ] || { echo \"FAIL backup: only \$entries restore entries\"; exit 1; }
echo \"OK backup: \$entries restore entries, checksum ok\"
"

step "PASS $OS_IMAGE"
