from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from shared.models import Cluster, HostMetricSample
from watcher import node_resource_forecast
from watcher.multivariate_shadow import align_host_vectors


def _inputs(count=16):
    start = datetime(2026, 9, 23, tzinfo=timezone.utc)
    loki = [(start + timedelta(minutes=10 * i), 30 + i / 10, 40 + i / 10)
            for i in range(count)]
    hosts = [SimpleNamespace(
        collected_at=timestamp - timedelta(seconds=30), disk_read_iops=100 + index,
        disk_write_iops=20 + index, disk_latency_ms=2 + index / 10,
    ) for index, (timestamp, _cpu, _ram) in enumerate(loki)]
    return loki, hosts


def test_align_host_vectors_requires_timestamp_match_and_all_finite_components():
    loki, hosts = _inputs()
    result = align_host_vectors(loki, hosts)
    assert result.quality_status == "OK"
    assert result.aligned_samples == 16
    assert result.max_skew_seconds == 30
    assert result.rows[-1]["disk_write_iops"] == 35


def test_align_host_vectors_does_not_use_future_or_stale_host_sample():
    loki, hosts = _inputs()
    hosts[-1].collected_at = loki[-1][0] + timedelta(seconds=1)
    hosts[-2].collected_at = loki[-2][0] - timedelta(minutes=8)
    result = align_host_vectors(loki, hosts)
    assert result.rejected_samples == 2
    assert result.quality_status == "PARTIAL"
    assert result.aligned_samples == 14


def test_align_host_vectors_never_zero_fills_missing_disk_evidence():
    loki, _hosts = _inputs()
    result = align_host_vectors(loki, [])
    assert result.rows == ()
    assert result.quality_status == "INSUFFICIENT_EVIDENCE"
    assert "disk_latency_ms" in result.missing_features


def test_align_host_vectors_rejects_invalid_values():
    loki, hosts = _inputs()
    hosts[-1].disk_latency_ms = float("nan")
    result = align_host_vectors(loki, hosts)
    assert result.quality_status == "PARTIAL"
    assert result.rejected_samples == 1
    assert all(row["disk_latency_ms"] == row["disk_latency_ms"] for row in result.rows)


def test_shadow_evidence_is_cluster_scoped_and_read_only(db_session):
    loki, hosts = _inputs()
    selected = Cluster(name="vector-selected", ceph_mon_nodes="10.0.0.1",
                       ssh_user="root", ssh_key_path="/tmp/key")
    other = Cluster(name="vector-other", ceph_mon_nodes="10.0.0.2",
                    ssh_user="root", ssh_key_path="/tmp/key")
    db_session.add_all([selected, other])
    db_session.flush()
    for host in hosts:
        db_session.add(HostMetricSample(
            cluster_id=other.id, host="node-1", cpu_percent=30, mem_percent=40,
            disk_read_iops=host.disk_read_iops, disk_write_iops=host.disk_write_iops,
            disk_latency_ms=host.disk_latency_ms, network_rx_bytes_per_sec=0,
            network_tx_bytes_per_sec=0, collected_at=host.collected_at.replace(tzinfo=None),
        ))
    db_session.commit()

    missing = node_resource_forecast._multivariate_shadow_evidence(
        db_session, selected.name, "node-1", loki,
    )
    assert missing["quality_status"] == "INSUFFICIENT_EVIDENCE"
    assert missing["score"] is None
    assert missing["alert_candidate"] is False

    for host in hosts:
        db_session.add(HostMetricSample(
            cluster_id=selected.id, host="node-1", cpu_percent=30, mem_percent=40,
            disk_read_iops=host.disk_read_iops, disk_write_iops=host.disk_write_iops,
            disk_latency_ms=host.disk_latency_ms, network_rx_bytes_per_sec=0,
            network_tx_bytes_per_sec=0, collected_at=host.collected_at.replace(tzinfo=None),
        ))
    db_session.commit()
    ready = node_resource_forecast._multivariate_shadow_evidence(
        db_session, selected.name, "node-1", loki,
    )
    assert ready["quality_status"] == "OK"
    assert ready["aligned_samples"] == 16
    assert ready["execution_mode"] == "SHADOW_ONLY"


def test_shadow_evidence_rejects_ambiguous_cluster_names(db_session):
    for address in ("10.0.1.1", "10.0.1.2"):
        db_session.add(Cluster(name="ambiguous-vector", ceph_mon_nodes=address,
                               ssh_user="root", ssh_key_path="/tmp/key"))
    db_session.commit()
    result = node_resource_forecast._multivariate_shadow_evidence(
        db_session, "ambiguous-vector", "node-1", _inputs()[0],
    )
    assert result["quality_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["alert_candidate"] is False
