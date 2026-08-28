"""Persisted, read-only host telemetry for cross-layer performance RCA."""

from __future__ import annotations

import logging
from datetime import datetime

from config.settings import settings
from shared import db
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.models import HostMetricSample
from watcher import ceph_client
from watcher.node_metrics import NodeMetricsError, collect_node_metrics, collect_node_metrics_with

logger = logging.getLogger(__name__)

HOSTNAME_TIMEOUT_SECONDS = 5
_identity_cache: dict[tuple[str, str], str] = {}


def _osd_hosts(cluster) -> list[str]:
    nodes = configured_nodes(cluster)
    return [node["host"] for node in nodes if "OSD" in node.get("roles", [])]


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


def collect_and_store(cluster_id: str, cluster=None, *, now: datetime | None = None) -> int:
    """Collect one sample per configured OSD host; failed hosts are skipped."""
    now = now or datetime.utcnow()
    hosts = _osd_hosts(cluster)
    if not hosts:
        return 0
    if cluster is None:
        user, key_path = settings.ssh_user, settings.ssh_key_path
    else:
        user, key_path, _mode, _container = resolve_ssh_creds(cluster)

    samples = []
    for host in hosts:
        try:
            metrics = collect_node_metrics(host) if cluster is None else collect_node_metrics_with(host, user, key_path)
        except NodeMetricsError as exc:
            logger.info("host_metrics: metrics unavailable for %s: %s", host, exc)
            continue
        samples.append(HostMetricSample(
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
        ))
    if not samples:
        return 0
    with db.SessionLocal() as session:
        session.add_all(samples)
        session.commit()
    return len(samples)
