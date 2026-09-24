"""Shared resource-budget constants for the production runtime.

These values intentionally describe hard container/job budgets, not a promise
that a host can safely run arbitrary additional workloads.  The host reserve
is kept explicit so a future service cannot silently consume the CPU budget
needed by Ceph and SSH.
"""

from __future__ import annotations

from dataclasses import dataclass

HOST_RESERVED_CPU = 2.0
MIN_HOST_CPU = 4
REQUIRED_HOST_CPU = 8

WORKER_CPU_LIMIT = 2.0
WORKER_MEMORY_LIMIT_MIB = 2048
WATCHER_CPU_LIMIT = 1.5
WATCHER_MEMORY_LIMIT_MIB = 1024
JOB_CPU_LIMIT = 1.0
JOB_MEMORY_LIMIT_MIB = 2048
RECOMMENDED_SWAP_GIB = (4, 8)


@dataclass(frozen=True)
class ResourceBudget:
    """A bounded CPU/RAM budget for one long-running or batch component."""

    cpu: float
    memory_mib: int


SERVICE_CPU_BUDGETS = {
    "dashboard-web": ResourceBudget(0.50, 512),
    "telegram-ai": ResourceBudget(0.50, 1024),
    "full-executor": ResourceBudget(1.00, 2048),
    "watcher": ResourceBudget(WATCHER_CPU_LIMIT, WATCHER_MEMORY_LIMIT_MIB),
    "vault-monitor": ResourceBudget(0.25, 256),
    "worker": ResourceBudget(WORKER_CPU_LIMIT, WORKER_MEMORY_LIMIT_MIB),
}


def total_service_cpu_budget() -> float:
    return sum(budget.cpu for budget in SERVICE_CPU_BUDGETS.values())


def host_cpu_reserve_is_possible(host_cpu_count: int, *, extra_cpu: float = 0.0) -> bool:
    """Return whether service caps leave the required host reserve."""

    return host_cpu_count >= MIN_HOST_CPU and (
        total_service_cpu_budget() + max(0.0, extra_cpu) + HOST_RESERVED_CPU
        <= float(host_cpu_count)
    )


def fio_one_cpu_options() -> str:
    """Return fio options that keep one benchmark job on one logical CPU."""

    return "--cpus_allowed=0 --cpus_allowed_policy=split"
