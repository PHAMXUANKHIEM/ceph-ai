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


def test_failing_sample_is_logged_and_skipped_without_blocking_the_batch(caplog):
    learned = []

    def update(sample):
        if sample["sample_id"] == "bad":
            raise ValueError("corrupt learner state")
        learned.append(sample["sample_id"])

    samples = [
        {"sample_id": "bad", "host": "10.3.54.118", "metric": "cpu"},
        {"sample_id": "s1", "host": "10.3.54.118", "metric": "cpu"},
        {"sample_id": "s2", "host": "10.3.54.118", "metric": "ram"},
    ]
    with caplog.at_level("ERROR", logger="shared.online_learning"):
        result = run_bounded_updates(
            samples, update, max_samples=10, timeout_seconds=10,
            circuit_breaker=LearningCircuitBreaker(failure_threshold=3),
        )
    assert learned == ["s1", "s2"]
    assert (result.processed, result.applied, result.failed) == (3, 2, 1)
    assert result.reason == "update_failed"
    record = next(item for item in caplog.records if "online learning update failed" in item.getMessage())
    assert "sample_id=bad host=10.3.54.118 metric=cpu" in record.getMessage()
    assert record.exc_info and "corrupt learner state" in str(record.exc_info[1])


def test_repeated_failures_still_open_the_breaker_and_end_the_cycle():
    calls = []

    def update(sample):
        calls.append(sample)
        raise RuntimeError("database unavailable")

    breaker = LearningCircuitBreaker(failure_threshold=2, cooldown_seconds=60)
    result = run_bounded_updates(
        range(10), update, max_samples=10, timeout_seconds=10,
        circuit_breaker=breaker, clock=lambda: 5.0,
    )
    assert calls == [0, 1]
    assert (result.processed, result.failed, result.reason) == (2, 2, "update_failed")
    assert not breaker.allow(now=6.0)


def test_failure_on_the_last_budgeted_sample_keeps_the_failure_reason():
    def update(sample):
        if sample == 2:
            raise RuntimeError("boom")

    result = run_bounded_updates(
        range(3), update, max_samples=3, timeout_seconds=10,
        circuit_breaker=LearningCircuitBreaker(),
    )
    assert (result.processed, result.applied, result.failed) == (3, 2, 1)
    assert result.reason == "update_failed"
