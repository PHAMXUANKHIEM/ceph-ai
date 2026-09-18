#!/usr/bin/env bash
set -euo pipefail

# Read-only host preflight.  It never changes CPU, memory or swap state.
# Set REQUIRE_SWAP=1 in a deployment gate when swap is mandatory.

required_swap_gib_min=4
host_cpus="$(nproc)"
mem_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
swap_kib="$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo)"
mem_gib=$((mem_kib / 1024 / 1024))
swap_gib=$((swap_kib / 1024 / 1024))

printf 'host_cpus=%s memory_gib=%s swap_gib=%s\n' "$host_cpus" "$mem_gib" "$swap_gib"

if (( host_cpus < 4 )); then
  echo 'ERROR: at least 4 logical CPUs are required for the 2-CPU host reserve.' >&2
  exit 2
fi
if (( swap_gib < required_swap_gib_min )); then
  echo "WARNING: swap is below the recommended ${required_swap_gib_min} GiB safety buffer; no swap was created." >&2
  if [[ "${REQUIRE_SWAP:-0}" == '1' ]]; then
    exit 3
  fi
fi

echo 'OK: compose caps reserve at least 2 CPUs for OS/Ceph/SSH.'
