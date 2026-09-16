import asyncio
import json

import paramiko
import pytest

from config.settings import Settings
from shared.retry import RetryPolicy, get_metrics, is_retryable_error, retry_async, retry_sync


def test_ceph_collection_defaults_are_bounded():
    settings = Settings(_env_file=None)

    assert settings.ceph_ssh_connect_timeout == 5
    assert settings.ceph_command_timeout == 15
    assert settings.ceph_health_timeout == 8
    assert settings.ceph_max_concurrency == 8
    assert settings.ceph_max_retries == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("ceph_command_timeout", 0),
        ("ceph_max_concurrency", 0),
        ("ceph_max_retries", 11),
        ("ceph_snapshot_max_age", 86401),
    ],
)
def test_ceph_collection_settings_reject_unbounded_or_invalid_values(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


def test_retry_classification_does_not_retry_deterministic_errors():
    assert not is_retryable_error(paramiko.AuthenticationException("bad key"))
    assert not is_retryable_error(paramiko.BadHostKeyException("host", None, None))
    assert not is_retryable_error(json.JSONDecodeError("bad", "{}", 0))
    assert is_retryable_error(TimeoutError("slow"))
    assert is_retryable_error(OSError("connection reset"))


def test_retry_policy_is_finite_and_jittered_within_bounds():
    policy = RetryPolicy(max_retries=2, base_delay_seconds=1, max_delay_seconds=3)

    assert policy.max_attempts == 3
    assert policy.delay(1, random_fn=lambda: 0.0) == 0.75
    assert policy.delay(2, random_fn=lambda: 1.0) == 2.5
    with pytest.raises(ValueError):
        policy.delay(3)


def test_retry_async_stops_at_configured_retry_limit():
    attempts = 0
    sleeps = []

    async def operation():
        nonlocal attempts
        attempts += 1
        raise TimeoutError("node stalled")

    async def fake_sleep(delay):
        sleeps.append(delay)

    with pytest.raises(TimeoutError):
        asyncio.run(
            retry_async(
                operation,
                RetryPolicy(max_retries=2, base_delay_seconds=1),
                sleep=fake_sleep,
                random_fn=lambda: 0.5,
            )
        )

    assert attempts == 3
    assert len(sleeps) == 2


def test_retry_sync_retries_transient_failure_and_records_metrics():
    attempts = 0
    sleeps = []
    before = get_metrics()

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("connection reset")
        return "ok"

    result = retry_sync(
        operation,
        RetryPolicy(max_retries=2, base_delay_seconds=1),
        sleep=sleeps.append,
        random_fn=lambda: 0.5,
    )

    after = get_metrics()
    assert result == "ok"
    assert attempts == 2
    assert sleeps == [1]
    assert after["retries_total"] == before["retries_total"] + 1
    assert after["retry_success_total"] == before["retry_success_total"] + 1


def test_retry_sync_does_not_retry_when_backoff_would_miss_deadline():
    before = get_metrics()

    with pytest.raises(TimeoutError):
        retry_sync(
            lambda: (_ for _ in ()).throw(TimeoutError("slow")),
            RetryPolicy(max_retries=2, base_delay_seconds=1),
            sleep=lambda _delay: pytest.fail("deadline must prevent sleeping"),
            random_fn=lambda: 0.5,
            deadline=0,
        )

    after = get_metrics()
    assert after["deadline_exhausted_total"] == before["deadline_exhausted_total"] + 1
