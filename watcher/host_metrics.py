"""Persisted, read-only host telemetry for cross-layer performance RCA."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from shared.time import utc_now
from threading import Lock

from config.settings import settings
from shared import db
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.models import HostMetricSample
from watcher import ceph_client
from watcher.node_metrics import NodeMetricsError, collect_node_metrics, collect_node_metrics_with

logger = logging.getLogger(__name__)

HOSTNAME_TIMEOUT_SECONDS = 5
HOST_METRICS_RETENTION_DAYS = 30
HOST_METRICS_PRUNE_INTERVAL = timedelta(hours=1)
_identity_cache: dict[tuple[str, str], str] = {}
_prune_lock = Lock()
_last_prune_at: dict[str, datetime] = {}


def _osd_hosts(cluster) -> list[str]:
    nodes = configured_nodes(cluster)
    return [node["host"] for node in nodes if "OSD" in node.get("roles", [])]


def _telemetry_hosts(cluster) -> list[str]:
    """Return every configured node that can be shown in Node Monitoring."""
    return [node["host"] for node in configured_nodes(cluster) if node.get("host")]


def _identity(cluster_id: str, host: str, cluster) -> str | None:
    key = (cluster_id, host)
    if key in _identity_cache:
        return _identity_cache[key]
    if cluster is None:
        user, key_path = settings.ssh_user, settings.ssh_key_path
        try:
            output = ceph_client.run_command_on_node(host, "hostname -s", timeout=HOSTNAME_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.info("host_metrics: hostname lookup failed for %s: %s", host, exc)
            return None
    else:
        user, key_path, _mode, _container = resolve_ssh_creds(cluster)
        try:
            output = ceph_client.run_command_on_node_with(
                host, "hostname -s", user, key_path, timeout=HOSTNAME_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.info("host_metrics: hostname lookup failed for %s: %s", host, exc)
            return None
    node_name = next((line.strip() for line in output.splitlines() if line.strip()), None)
    if node_name:
        _identity_cache[key] = node_name
    return node_name


def _collect_sample(
    cluster_id: str,
    host: str,
    cluster,
    user: str,
    key_path: str,
    now: datetime,
) -> HostMetricSample | None:
    try:
        metrics = (
            collect_node_metrics(host)
            if cluster is None
            else collect_node_metrics_with(host, user, key_path)
        )
        return HostMetricSample(
            cluster_id=cluster_id,
            host=host,
            node_name=_identity(cluster_id, host, cluster),
            cpu_percent=float(metrics.get("cpu_percent", 0)),
            mem_percent=float(metrics.get("mem_percent", 0)),
            disk_read_iops=float(metrics.get("disk_read_iops", 0)),
            disk_write_iops=float(metrics.get("disk_write_iops", 0)),
            disk_latency_ms=float(metrics.get("disk_latency_ms", 0)),
            network_rx_bytes_per_sec=float(metrics.get("network_rx_bytes_per_sec", 0)),
            network_tx_bytes_per_sec=float(metrics.get("network_tx_bytes_per_sec", 0)),
            collected_at=now,
        )
    except NodeMetricsError as exc:
        logger.info("host_metrics: metrics unavailable for %s: %s", host, exc)
    except Exception:
        # One malformed response must not discard samples collected from the
        # other nodes in the same telemetry tick.
        logger.exception("host_metrics: unexpected collection failure for %s", host)
    return None


def _prune_due(cluster_id: str, now: datetime) -> bool:
    with _prune_lock:
        previous = _last_prune_at.get(cluster_id)
        if previous is not None and now - previous < HOST_METRICS_PRUNE_INTERVAL:
            return False
        _last_prune_at[cluster_id] = now
        return True


def collect_and_store(cluster_id: str, cluster=None, *, now: datetime | None = None) -> int:
    """Collect one sample per configured node; failed hosts are skipped."""
    now = now or utc_now()
    hosts = _telemetry_hosts(cluster)
    if not hosts:
        return 0
    if cluster is None:
        user, key_path = settings.ssh_user, settings.ssh_key_path
    else:
        user, key_path, _mode, _container = resolve_ssh_creds(cluster)

    with ThreadPoolExecutor(max_workers=min(8, len(hosts)), thread_name_prefix="host-metrics") as executor:
        futures = [
            executor.submit(_collect_sample, cluster_id, host, cluster, user, key_path, now)
            for host in hosts
        ]
        samples = [sample for future in futures if (sample := future.result()) is not None]
    if not samples:
        return 0
    with db.SessionLocal() as session:
        session.add_all(samples)
        if _prune_due(cluster_id, now):
            retention_cutoff = now - timedelta(days=HOST_METRICS_RETENTION_DAYS)
            session.query(HostMetricSample).filter(
                HostMetricSample.cluster_id == cluster_id,
                HostMetricSample.collected_at < retention_cutoff,
            ).delete(synchronize_session=False)
        session.commit()
    return len(samples)
