from datetime import datetime, timedelta
from contextlib import contextmanager

from shared.learning_safety import CircuitBreaker, RateLimiter
from shared.models import Cluster, HostMetricSample
from watcher import learning_retention


def test_rate_limiter_blocks_only_within_interval():
    limiter = RateLimiter()
    assert limiter.allow("job", interval_seconds=10, now=100.0)
    assert not limiter.allow("job", interval_seconds=10, now=109.9)
    assert limiter.allow("job", interval_seconds=10, now=110.0)


def test_circuit_breaker_opens_and_allows_one_probe_after_cooldown():
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=30)
    breaker.record_failure(now=100.0)
    assert breaker.allow(now=101.0)
    breaker.record_failure(now=102.0)
    assert breaker.is_open
    assert not breaker.allow(now=110.0)
    assert breaker.allow(now=133.0)
    assert not breaker.allow(now=133.1)
    breaker.record_success()
    assert not breaker.is_open
    assert breaker.allow(now=134.0)


def test_learning_retention_removes_old_raw_samples_only(db_session, monkeypatch):
    cluster = Cluster(
        name="retention-test", ceph_mon_nodes="10.0.0.1", ssh_user="root",
        ssh_key_path="/key", is_active=True,
    )
    db_session.add(cluster)
    db_session.flush()
    now = datetime(2026, 9, 18, 10, 0, 0)
    db_session.add_all([
        HostMetricSample(
            cluster_id=cluster.id, host="10.0.0.1", cpu_percent=1, mem_percent=2,
            disk_read_iops=0, disk_write_iops=0, disk_latency_ms=0,
            network_rx_bytes_per_sec=0, network_tx_bytes_per_sec=0,
            collected_at=now - timedelta(days=31),
        ),
        HostMetricSample(
            cluster_id=cluster.id, host="10.0.0.1", cpu_percent=3, mem_percent=4,
            disk_read_iops=0, disk_write_iops=0, disk_latency_ms=0,
            network_rx_bytes_per_sec=0, network_tx_bytes_per_sec=0,
            collected_at=now - timedelta(days=1),
        ),
    ])
    db_session.commit()
    @contextmanager
    def session_override():
        yield db_session

    monkeypatch.setattr(learning_retention.db, "SessionLocal", session_override)
    monkeypatch.setattr(learning_retention, "_last_prune_at", None)
    monkeypatch.setattr(learning_retention.settings, "learning_retention_interval_seconds", 60)
    result = learning_retention.prune_old_rows(now)
    assert result["host_metric_samples"] == 1
    assert db_session.query(HostMetricSample).count() == 1
