#!/usr/bin/env bash
set -euo pipefail

# Explicit opt-in only.  Usage: SWAP_SIZE_GIB=4|5|6|7|8 ./enable_safe_swap.sh
# This script is intentionally not called by deploy/restart scripts.

if [[ "$(id -u)" != '0' ]]; then
  echo 'ERROR: run as root.' >&2
  exit 2
fi

size_gib="${SWAP_SIZE_GIB:-8}"
if [[ ! "$size_gib" =~ ^[4-8]$ ]]; then
  echo 'ERROR: SWAP_SIZE_GIB must be an integer from 4 through 8.' >&2
  exit 2
fi

swapfile='/var/lib/ceph-ai/swapfile'
mkdir -p /var/lib/ceph-ai
if [[ -e "$swapfile" && ! -f "$swapfile" ]]; then
  echo "ERROR: refusing non-regular swapfile path: $swapfile" >&2
  exit 3
fi

if swapon --show=NAME --noheadings | awk '{print $1}' | grep -Fxq "$swapfile"; then
  echo "Already enabled: $swapfile"
  exit 0
fi

if [[ -e "$swapfile" ]]; then
  echo "ERROR: $swapfile exists but is not active swap; inspect it before replacement." >&2
  exit 3
fi

fallocate -l "${size_gib}G" "$swapfile"
chmod 600 "$swapfile"
mkswap "$swapfile" >/dev/null
swapon "$swapfile"

grep -Fqx "$swapfile none swap sw 0 0" /etc/fstab || \
  printf '%s\n' "$swapfile none swap sw 0 0" >> /etc/fstab

sysctl -w vm.swappiness=10 >/dev/null
sysctl_file='/etc/sysctl.d/99-ceph-ai-swap.conf'
if [[ ! -f "$sysctl_file" ]] || ! grep -Fqx 'vm.swappiness=10' "$sysctl_file"; then
  printf '%s\n' 'vm.swappiness=10' > "$sysctl_file"
fi

echo "Enabled ${size_gib} GiB swap at $swapfile with vm.swappiness=10."
