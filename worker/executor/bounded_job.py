"""Run expensive benchmark jobs outside the Worker process budget."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue
import resource
import time
from types import SimpleNamespace

from shared.resource_limits import JOB_CPU_LIMIT, JOB_MEMORY_LIMIT_MIB

logger = logging.getLogger(__name__)

# The longest current volume sweep is bounded by 9 depths x 5 samples x
# (30s runtime + 10s ramp), plus SSH/cleanup overhead.  A 30-minute ceiling
# prevents an abandoned fio job from consuming the Worker forever.
JOB_TIMEOUT_SECONDS = 30 * 60


def _apply_child_limits() -> None:
    """Constrain only the benchmark child, never the parent Worker."""

    if not hasattr(os, "sched_getaffinity"):
        raise RuntimeError("benchmark CPU affinity is unavailable on this host")
    available = sorted(os.sched_getaffinity(0))
    if not available:
        raise RuntimeError("benchmark child has no available CPU")
    if JOB_CPU_LIMIT != 1.0:
        raise RuntimeError("benchmark policy must remain exactly one CPU")
    os.sched_setaffinity(0, {available[0]})

    memory_limit = JOB_MEMORY_LIMIT_MIB * 1024 * 1024
    current = resource.getrlimit(resource.RLIMIT_AS)
    hard_limit = current[1]
    if hard_limit != resource.RLIM_INFINITY and hard_limit < memory_limit:
        memory_limit = hard_limit
    resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))


def _child_entry(
    messages,
    action_pk: str,
    action_params: dict,
    incident_id: str,
    executor_name: str,
    cluster_user: str | None,
    cluster_key_path: str | None,
) -> None:
    try:
        _apply_child_limits()
        from worker.executor import vm_perf, volume_perf

        executor = vm_perf if executor_name == "vm" else volume_perf

        def write_progress(progress_action_pk: str, progress: list[dict]) -> None:
            messages.put(("progress", progress_action_pk, progress))

        if executor_name == "vm":
            cluster = SimpleNamespace(ssh_user=cluster_user, ssh_key_path=cluster_key_path)
            succeeded = executor.run(
                action_pk, action_params, incident_id, write_progress, cluster
            )
        else:
            succeeded = executor.run(action_pk, action_params, incident_id, write_progress)
        messages.put(("result", bool(succeeded), None))
    except BaseException as exc:  # child must report every failure to its parent
        messages.put(("error", type(exc).__name__, str(exc)))


def run_bounded_benchmark(
    action_pk: str,
    action_params: dict,
    incident_id: str,
    write_progress,
    *,
    executor_module,
    executor_name: str,
    cluster=None,
) -> bool:
    """Run a benchmark in a one-CPU/2-GiB child process.

    Pytest keeps the old inline path so existing executor mocks remain useful;
    production always uses ``spawn`` and therefore cannot inherit the
    Worker's database connections or CPU affinity.
    """

    if os.environ.get("PYTEST_CURRENT_TEST"):
        if executor_name == "vm":
            return bool(executor_module.run(
                action_pk, action_params, incident_id, write_progress, cluster
            ))
        return bool(executor_module.run(action_pk, action_params, incident_id, write_progress))

    context = mp.get_context("spawn")
    messages = context.Queue()
    process = context.Process(
        target=_child_entry,
        args=(
            messages,
            action_pk,
            action_params,
            incident_id,
            executor_name,
            getattr(cluster, "ssh_user", None),
            getattr(cluster, "ssh_key_path", None),
        ),
        name=f"ceph-ai-{executor_name}-benchmark",
    )
    process.start()
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    result: bool | None = None
    try:
        while time.monotonic() < deadline:
            try:
                kind, first, second = messages.get(timeout=0.25)
            except queue.Empty:
                if not process.is_alive():
                    break
                continue
            if kind == "progress":
                write_progress(first, second)
            elif kind == "result":
                result = bool(first)
                break
            else:
                logger.error(
                    "bounded benchmark %s failed in child: %s: %s",
                    action_pk, first, second,
                )
                result = False
                break
        else:
            logger.error("bounded benchmark %s exceeded %ss", action_pk, JOB_TIMEOUT_SECONDS)
            process.terminate()
            result = False
        process.join(timeout=10)
        if result is None:
            result = False
        return result
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        messages.close()
        messages.join_thread()
