from __future__ import annotations

import threading
import time

import pytest

from shared.realtime_controls import (
    BackoffCircuit,
    CircuitOpenError,
    ClusterConcurrency,
    CollectionBusyError,
)


def test_concurrency_gate_rejects_when_all_slots_are_busy():
    gate = ClusterConcurrency(max_concurrency=1, per_cluster=1)
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with gate.slot("cluster-a", timeout_seconds=1):
            entered.set()
            release.wait(1)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(1)
    with pytest.raises(CollectionBusyError):
        with gate.slot("cluster-b", timeout_seconds=0.02):
            pass
    release.set()
    thread.join(1)


def test_circuit_breaker_opens_and_allows_one_probe_after_backoff():
    clock = [100.0]
    circuit = BackoffCircuit(
        failure_threshold=2,
        base_cooldown_seconds=5,
        max_cooldown_seconds=20,
        random_fn=lambda: 0.5,
    )
    circuit.record_failure("timeout")
    assert circuit.allow(now=clock[0])
    circuit.record_failure("timeout")
    assert not circuit.allow(now=clock[0])
    circuit.open_until = clock[0] + 5
    assert not circuit.allow(now=clock[0] + 4.9)
    assert circuit.allow(now=clock[0] + 5.1)
    assert not circuit.allow(now=clock[0] + 5.1)
    circuit.record_success()
    assert circuit.allow(now=clock[0] + 5.2)


def test_circuit_failure_does_not_raise_on_record():
    circuit = BackoffCircuit(
        failure_threshold=1,
        base_cooldown_seconds=1,
        max_cooldown_seconds=2,
        random_fn=lambda: 0.5,
    )
    circuit.record_failure(CircuitOpenError("open"))
    assert circuit.state()["open"] is True
