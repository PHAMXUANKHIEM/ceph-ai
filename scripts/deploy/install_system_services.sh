#!/usr/bin/env bash
# Retired 2026-10-06 (plan SM5). This installer predates the container stack:
# it enabled and restarted the bare-metal ceph-ai-watcher/worker/dashboard
# units, which would run a second Watcher/Worker beside the containers
# against the same database and queues.
#
# Production units are installed by the deploy itself:
#   scripts/deploy/restart_container_stack.sh (runtime_setup phase)
# See docs/immutable-production-release.md.
echo "install_system_services.sh is retired: production units come from scripts/deploy/restart_container_stack.sh (docs/immutable-production-release.md)." >&2
exit 2
