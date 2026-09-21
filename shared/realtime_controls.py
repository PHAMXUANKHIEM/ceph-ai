"""Bounded controls shared by the realtime Ceph snapshot collector.

The normal SSH runner already limits individual commands.  This module adds
the missing collector-level guard: a slow cluster cannot consume every
collector slot, and repeated failures stop creating more Ceph work until a
single half-open probe is allowed.
"""

from __future__ import annotations

import random
from contextlib import contextmanager
from threading import BoundedSemaphore, Lock
from time import monotonic

from config.settings import settings


class CollectionBusyError(TimeoutError):
    """The collector could not obtain a bounded concurrency slot."""


class CircuitOpenError(RuntimeError):
    """A cluster/tier circuit is open and no probe is currently allowed."""


_METRICS_LOCK = Lock()
_METRICS = {
    "slot_acquired_total": 0,
    "slot_rejected_total": 0,
    "slot_released_total": 0,
    "inflight": 0,
    "circuit_open_total": 0,
    "circuit_probe_total": 0,
    "circuit_failure_total": 0,
    "circuit_success_total": 0,
}


def _metric(name: str, delta: int = 1) -> None:
    with _METRICS_LOCK:
        _METRICS[name] += delta


def get_metrics() -> dict[str, int]:
    with _METRICS_LOCK:
        return dict(_METRICS)


class ClusterConcurrency:
    """Limit collector work globally and per cluster within one process."""

    def __init__(self, max_concurrency: int | None = None, per_cluster: int | None = None):
        maximum = max(1, int(max_concurrency or settings.ceph_max_concurrency))
        self.maximum = maximum
        self.per_cluster_limit = max(1, min(int(per_cluster or maximum), 4))
        self._global = BoundedSemaphore(maximum)
        self._cluster: dict[str, BoundedSemaphore] = {}
        self._lock = Lock()

    def _cluster_gate(self, cluster_id: str) -> BoundedSemaphore:
        with self._lock:
            gate = self._cluster.get(cluster_id)
            if gate is None:
                # Keep the map bounded. Cluster IDs are application-owned and
                # normally very small, but diagnostics must not grow forever.
                if len(self._cluster) >= 256:
                    self._cluster.pop(next(iter(self._cluster)))
                gate = self._cluster[cluster_id] = BoundedSemaphore(self.per_cluster_limit)
            return gate

    @contextmanager
    def slot(self, cluster_id: str | None, *, timeout_seconds: float):
        key = str(cluster_id or "__unscoped__")[:128]
        timeout = max(0.01, float(timeout_seconds))
        started = monotonic()
        if not self._global.acquire(timeout=timeout):
            _metric("slot_rejected_total")
            raise CollectionBusyError(f"realtime collector concurrency limit reached for {key}")
        cluster_gate = self._cluster_gate(key)
        remaining = max(0.01, timeout - (monotonic() - started))
        if not cluster_gate.acquire(timeout=remaining):
            self._global.release()
            _metric("slot_rejected_total")
            raise CollectionBusyError(f"realtime collector cluster limit reached for {key}")
        _metric("slot_acquired_total")
        _metric("inflight")
        try:
            yield
        finally:
            cluster_gate.release()
            self._global.release()
            _metric("inflight", -1)
            _metric("slot_released_total")


class BackoffCircuit:
    """Circuit breaker with exponential cooldown and bounded jitter."""

    def __init__(
        self,
        *,
        failure_threshold: int,
        base_cooldown_seconds: float,
        max_cooldown_seconds: float,
        random_fn=random.random,
    ):
        self.failure_threshold = max(1, int(failure_threshold))
        self.base_cooldown_seconds = max(0.1, float(base_cooldown_seconds))
        self.max_cooldown_seconds = max(self.base_cooldown_seconds, float(max_cooldown_seconds))
        self.random_fn = random_fn
        self.failures = 0
        self.open_until = 0.0
        self.probe_in_flight = False
        self.last_reason = ""
        self._lock = Lock()

    def allow(self, *, now: float | None = None) -> bool:
        current = monotonic() if now is None else float(now)
        with self._lock:
            if self.open_until <= current:
                if self.open_until > 0 and self.probe_in_flight:
                    return False
                if self.open_until > 0:
                    self.probe_in_flight = True
                    _metric("circuit_probe_total")
                return True
            _metric("circuit_open_total")
            return False

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.open_until = 0.0
            self.probe_in_flight = False
            self.last_reason = ""
        _metric("circuit_success_total")

    def record_failure(self, reason: object = "") -> None:
        current = monotonic()
        with self._lock:
            self.failures += 1
            self.probe_in_flight = False
            self.last_reason = str(reason)[:240]
            if self.failures >= self.failure_threshold:
                exponent = self.failures - self.failure_threshold
                cooldown = min(self.max_cooldown_seconds, self.base_cooldown_seconds * (2 ** exponent))
                # +/- 25% jitter prevents all clusters retrying at once.
                cooldown *= 0.75 + (self.random_fn() * 0.5)
                self.open_until = current + min(self.max_cooldown_seconds, cooldown)
        _metric("circuit_failure_total")

    def state(self) -> dict[str, object]:
        with self._lock:
            return {
                "failures": self.failures,
                "open": self.open_until > monotonic(),
                "open_until_monotonic": self.open_until or None,
                "last_reason": self.last_reason,
            }


_CIRCUITS_LOCK = Lock()
_CIRCUITS: dict[tuple[str, str], BackoffCircuit] = {}


def circuit_for(cluster_id: str, tier: str) -> BackoffCircuit:
    key = (str(cluster_id or "")[:128], str(tier or "unknown")[:64])
    with _CIRCUITS_LOCK:
        circuit = _CIRCUITS.get(key)
        if circuit is None:
            if len(_CIRCUITS) >= 512:
                _CIRCUITS.pop(next(iter(_CIRCUITS)))
            circuit = _CIRCUITS[key] = BackoffCircuit(
                failure_threshold=settings.ceph_realtime_circuit_failure_threshold,
                base_cooldown_seconds=settings.ceph_realtime_circuit_base_cooldown_seconds,
                max_cooldown_seconds=settings.ceph_realtime_circuit_max_cooldown_seconds,
            )
        return circuit


def circuit_metrics() -> dict[str, object]:
    with _CIRCUITS_LOCK:
        states = {
            f"{cluster}:{tier}": circuit.state()
            for (cluster, tier), circuit in list(_CIRCUITS.items())[:512]
        }
    return {"tracked": len(states), "states": states}


COLLECTOR_CONCURRENCY = ClusterConcurrency()
