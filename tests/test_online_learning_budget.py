from shared.online_learning import LearningCircuitBreaker, run_bounded_updates


def test_bounded_updates_stop_at_sample_budget():
    values = []
    breaker = LearningCircuitBreaker()
    result = run_bounded_updates(
        range(10), values.append, max_samples=3, timeout_seconds=10,
        circuit_breaker=breaker,
    )
    assert result.reason == "sample_budget_exhausted"
    assert result.processed == result.applied == 3
    assert values == [0, 1, 2]


def test_bounded_updates_stop_on_timeout_without_consuming_more_items():
    ticks = iter([0.0, 0.1, 2.0, 2.0])
    values = []
    result = run_bounded_updates(
        range(10), values.append, max_samples=10, timeout_seconds=1,
        circuit_breaker=LearningCircuitBreaker(), clock=lambda: next(ticks),
    )
    assert result.reason == "timeout"
    assert result.processed == result.applied == 1
    assert values == [0]


def test_circuit_breaker_opens_after_repeated_failures_and_resets_on_success():
    breaker = LearningCircuitBreaker(failure_threshold=2, cooldown_seconds=10)
    breaker.record_failure(now=1.0)
    assert breaker.allow(now=2.0)
    breaker.record_failure(now=2.0)
    assert not breaker.allow(now=3.0)
    assert breaker.allow(now=12.0)
    breaker.record_success()
    assert breaker.consecutive_failures == 0
    assert breaker.allow(now=3.0)


def test_open_circuit_skips_cycle_without_calling_update():
    values = []
    breaker = LearningCircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    breaker.record_failure(now=10.0)
    result = run_bounded_updates(
        range(3), values.append, max_samples=3, timeout_seconds=10,
        circuit_breaker=breaker, clock=lambda: 11.0,
    )
    assert result.reason == "circuit_open"
    assert result.processed == result.applied == 0
    assert values == []
