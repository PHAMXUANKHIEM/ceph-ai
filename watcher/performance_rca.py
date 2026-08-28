"""Read-only, explainable cross-layer performance correlation.

This module deliberately reports *correlation candidates*, not root-cause
claims.  The current data model has volume history, latest OSD distribution
and live ``ceph osd perf`` data, but it does not persist a volume -> PG -> OSD
mapping or host disk samples.  Those gaps are part of the report so the AI
never fills them with an invented causal story.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from shared import db
from shared.models import CrushOsdDistribution, HostMetricSample, VolumeMetric, VolumeOsdMapping
from watcher import ceph_client
from watcher.ceph_client import CephQueryError
from shared.cluster_nodes import resolve_ssh_creds

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_HOURS = 1
MAX_WINDOW_HOURS = 24
MAX_VOLUME_REPORTS = 20
MIN_VOLUME_SAMPLES = 3
VOLUME_ELEVATED_RATIO = 1.25
POOL_ELEVATED_RATIO = 1.20
FRESH_VOLUME_SECONDS = 15 * 60
FRESH_DISTRIBUTION_SECONDS = 30 * 60
FRESH_HOST_SECONDS = 5 * 60
HOST_CPU_HIGH_PERCENT = 85.0
HOST_MEM_HIGH_PERCENT = 90.0
HOST_DISK_LATENCY_HIGH_MS = 20.0


def _median(values: list[float]) -> float | None:
    return round(float(statistics.median(values)), 3) if values else None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="seconds") + "Z"


def _age(now: datetime, observed_at: datetime | None) -> int | None:
    if observed_at is None:
        return None
    return max(0, int((now - observed_at).total_seconds()))


def _freshness(now: datetime, observed_at: datetime | None, threshold: int) -> dict:
    age = _age(now, observed_at)
    return {
        "observed_at": _iso(observed_at),
        "age_seconds": age,
        "status": "fresh" if age is not None and age <= threshold else "stale",
    }


def _latency(row: VolumeMetric) -> float:
    return max(float(row.read_latency_ms or 0), float(row.write_latency_ms or 0))


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return round(min(high, max(low, value)), 3)


def _host_key(value: str | None) -> str:
    return (value or "").strip().lower().rstrip(".")


def _latest_by_key(rows: list[VolumeMetric]) -> dict[tuple[str, str], VolumeMetric]:
    latest: dict[tuple[str, str], VolumeMetric] = {}
    for row in rows:
        key = (row.pool, row.image)
        if key not in latest or row.polled_at > latest[key].polled_at:
            latest[key] = row
    return latest


def _osd_summary(live_signals: dict | None) -> dict:
    if not live_signals or live_signals.get("status") != "ready":
        return {
            "status": "not_available",
            "reason": (live_signals or {}).get("reason", "Chưa có live ceph osd perf"),
            "measured_osds": 0,
            "outliers": [],
            "score": 0.0,
        }
    outliers = live_signals.get("outliers") or []
    score = max((float(item.get("ratio", 1.0)) - 1.0) / 3.0 for item in outliers) if outliers else 0.0
    return {
        "status": "observed",
        "reason": "Live commit latency từ ceph osd perf; chưa phải disk SMART/IOPS per OSD.",
        "measured_osds": int(live_signals.get("measured_osds", 0)),
        "median_commit_latency_ms": live_signals.get("median_commit_latency_ms"),
        "outliers": outliers[:5],
        "score": _clamp(score),
        "freshness": live_signals.get("freshness"),
    }


def _host_evidence(
    topology: dict | None,
    host_by_osd: dict[int, str | None],
    host_samples: dict[str, HostMetricSample],
    now: datetime,
) -> list[dict]:
    if not topology:
        return []
    evidence = []
    for osd_id in topology.get("acting_osds", []):
        host = host_by_osd.get(osd_id)
        if not host:
            continue
        sample = host_samples.get(host.strip().lower().rstrip("."))
        if sample is None or (_age(now, sample.collected_at) or 0) > FRESH_HOST_SECONDS:
            continue
        flags = []
        if sample.cpu_percent >= HOST_CPU_HIGH_PERCENT:
            flags.append("cpu_high")
        if sample.mem_percent >= HOST_MEM_HIGH_PERCENT:
            flags.append("memory_high")
        if sample.disk_latency_ms >= HOST_DISK_LATENCY_HIGH_MS:
            flags.append("disk_latency_high")
        evidence.append({
            "host": host,
            "sample_host": sample.host,
            "node_name": sample.node_name,
            "osd_id": osd_id,
            "cpu_percent": round(sample.cpu_percent, 1),
            "mem_percent": round(sample.mem_percent, 1),
            "disk_latency_ms": round(sample.disk_latency_ms, 2),
            "disk_read_iops": round(sample.disk_read_iops, 1),
            "disk_write_iops": round(sample.disk_write_iops, 1),
            "network_rx_bytes_per_sec": round(sample.network_rx_bytes_per_sec, 1),
            "network_tx_bytes_per_sec": round(sample.network_tx_bytes_per_sec, 1),
            "flags": flags,
            "bottleneck": bool(flags),
            "observed_at": _iso(sample.collected_at),
        })
    return evidence


def _volume_analysis(
    rows: list[VolumeMetric],
    latest: VolumeMetric,
    pool_latest: list[VolumeMetric],
    cluster_latest: list[VolumeMetric],
    osd_summary: dict,
    topology: dict | None = None,
    host_evidence: list[dict] | None = None,
) -> dict:
    values = [_latency(row) for row in rows]
    historical = values[:-1] if len(values) > 1 else values
    baseline = _median(historical)
    current = round(values[-1], 3)
    delta_percent = round(((current - baseline) / baseline) * 100, 1) if baseline and baseline > 0 else None
    volume_elevated = bool(
        baseline is not None
        and baseline > 0
        and current >= baseline * VOLUME_ELEVATED_RATIO
    )

    pool_values = [_latency(row) for row in pool_latest]
    cluster_values = [_latency(row) for row in cluster_latest]
    pool_median = _median(pool_values)
    cluster_median = _median(cluster_values)
    pool_ratio = round(pool_median / cluster_median, 3) if pool_median and cluster_median else None
    pool_contention = bool(
        len(pool_latest) >= 2
        and pool_median is not None
        and cluster_median is not None
        and pool_median >= cluster_median * POOL_ELEVATED_RATIO
    )

    volume_score = _clamp((current / baseline) - 1.0) if baseline and baseline > 0 else 0.0
    pool_score = _clamp((pool_ratio or 1.0) - 1.0)
    osd_score = float(osd_summary.get("score", 0.0))
    host_evidence = host_evidence or []
    host_bottlenecks = [item for item in host_evidence if item.get("bottleneck")]
    mapped_osds = topology.get("acting_osds", []) if topology else []
    outlier_ids = {item.get("osd_id") for item in osd_summary.get("outliers", [])}
    mapped_outliers = [osd_id for osd_id in mapped_osds if osd_id in outlier_ids]
    if volume_elevated and host_bottlenecks:
        hypothesis = "host_resource_candidate"
        explanation = "Volume tăng latency và host của acting set có CPU/RAM/disk signal cao."
        confidence = _clamp(0.50 + volume_score * 0.20)
    elif volume_elevated and mapped_outliers:
        hypothesis = "sampled_data_osd_latency_candidate"
        explanation = "Volume đang tăng latency và các data-object PG được sample chứa OSD latency outlier."
        confidence = _clamp(0.55 + volume_score * 0.20 + osd_score * 0.20)
    elif volume_elevated and pool_contention:
        hypothesis = "pool_contention_candidate"
        explanation = "Volume và các peer cùng pool cùng tăng latency so với median toàn cụm."
        confidence = _clamp(0.35 + volume_score * 0.25 + pool_score * 0.25)
    elif volume_elevated:
        hypothesis = "volume_consumer_bottleneck_candidate"
        explanation = "Volume tăng latency nhưng chưa thấy tín hiệu pool-wide tương ứng."
        confidence = _clamp(0.30 + volume_score * 0.25)
    elif osd_summary.get("outliers"):
        hypothesis = "osd_disk_latency_candidate_unscoped"
        explanation = "Có OSD commit-latency outlier, nhưng chưa ánh xạ được tới volume/PG này."
        confidence = _clamp(0.25 + osd_score * 0.20)
    else:
        hypothesis = "no_strong_candidate"
        explanation = "Chưa có tín hiệu đủ mạnh để xếp hạng một bottleneck."
        confidence = 0.1

    return {
        "pool": latest.pool,
        "image": latest.image,
        "observed_at": _iso(latest.polled_at),
        "samples": len(rows),
        "current_latency_ms": current,
        "baseline_latency_ms": baseline,
        "delta_percent": delta_percent,
        "iops": round(float(latest.iops or 0), 3),
        "saturated": bool(latest.saturated),
        "signals": {
            "volume": {
                "status": "elevated" if volume_elevated else "normal",
                "score": volume_score,
                "source": "volume_metrics",
            },
            "pool": {
                "status": "elevated" if pool_contention else "normal",
                "score": pool_score,
                "peer_volumes": len(pool_latest),
                "pool_median_latency_ms": pool_median,
                "cluster_median_latency_ms": cluster_median,
                "source": "volume_metrics",
            },
            "osd": osd_summary,
            "mapped_outlier_osds": mapped_outliers,
            "host": {
                "status": "bottleneck" if host_bottlenecks else ("observed" if host_evidence else "not_available"),
                "evidence": host_evidence,
            },
        },
        "hypothesis": hypothesis,
        "explanation": explanation,
        "confidence": confidence,
        "topology": topology,
        "host_evidence": host_evidence,
    }


def _distribution_chain(session, cluster_id: str, now: datetime) -> tuple[dict, dict | None]:
    rows = session.query(CrushOsdDistribution).filter(
        CrushOsdDistribution.cluster_id == cluster_id,
    ).all()
    latest_at = max((row.updated_at for row in rows), default=None)
    usable = [row for row in rows if row.host]
    pgs = [int(row.pgs) for row in rows if row.pgs is not None]
    pgs_median = _median([float(value) for value in pgs])
    pgs_max = max(pgs) if pgs else None
    chain = {
        "layer": "pg",
        "status": "observed" if pgs else "not_available",
        "detail": (
            f"Có {len(pgs)} OSD có PG count; max={pgs_max}, median={pgs_median}. "
            "Đây là phân bố PG theo OSD, chưa phải acting set của volume."
            if pgs else "Chưa có PG distribution trong DB."
        ),
        "source": "crush_osd_distribution",
        "freshness": _freshness(now, latest_at, FRESH_DISTRIBUTION_SECONDS),
    }
    host_chain = {
        "layer": "host",
        "status": "metadata_only" if usable else "not_available",
        "detail": (
            f"Đã biết host của {len(usable)}/{len(rows)} OSD; chưa có host CPU/RAM/disk sample "
            "được lưu cùng timestamp để chứng minh causal link."
            if rows else "Chưa có OSD→host mapping trong DB."
        ),
        "source": "crush_osd_distribution",
        "freshness": _freshness(now, latest_at, FRESH_DISTRIBUTION_SECONDS),
    }
    return {"pg": chain, "host": host_chain}, {
        "source_id": "crush_osd_distribution",
        "observed_at": _iso(latest_at),
        "row_count": len(rows),
    } if rows else None


def build_report(
    session,
    cluster_id: str,
    *,
    now: datetime | None = None,
    pool: str | None = None,
    image: str | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    live_signals: dict | None = None,
) -> dict:
    """Build a deterministic report from already-collected evidence."""
    now = now or datetime.utcnow()
    window_hours = max(1, min(int(window_hours or DEFAULT_WINDOW_HOURS), MAX_WINDOW_HOURS))
    window_start = now - timedelta(hours=window_hours)
    query = session.query(VolumeMetric).filter(
        VolumeMetric.cluster_id == cluster_id,
        VolumeMetric.polled_at >= window_start,
        VolumeMetric.polled_at <= now,
    )
    if pool:
        query = query.filter(VolumeMetric.pool == pool)
    if image:
        query = query.filter(VolumeMetric.image == image)
    rows = query.order_by(VolumeMetric.polled_at.asc()).all()

    grouped: dict[tuple[str, str], list[VolumeMetric]] = defaultdict(list)
    for row in rows:
        grouped[(row.pool, row.image)].append(row)
    latest_map = _latest_by_key(rows)
    cluster_latest = list(latest_map.values())
    osd_summary = _osd_summary(live_signals)
    mapping_rows = session.query(VolumeOsdMapping).filter(
        VolumeOsdMapping.cluster_id == cluster_id,
    ).all()
    fresh_mapping_latest_at = None
    topology_by_key = {}
    stale_or_legacy_mappings = 0
    for mapping in mapping_rows:
        mapping_age = _age(now, mapping.captured_at)
        if (
            getattr(mapping, "mapping_scope", "header_legacy") != "data_sample"
            or mapping_age is None
            or mapping_age > FRESH_DISTRIBUTION_SECONDS
        ):
            stale_or_legacy_mappings += 1
            continue
        fresh_mapping_latest_at = max(fresh_mapping_latest_at or mapping.captured_at, mapping.captured_at)
        try:
            acting_osds = json.loads(mapping.acting_osds_json)
        except (TypeError, ValueError):
            acting_osds = []
        if not isinstance(acting_osds, list):
            acting_osds = []
        try:
            pgids = json.loads(mapping.pgids_json)
        except (TypeError, ValueError, AttributeError):
            pgids = [mapping.pgid]
        try:
            sampled_objects = json.loads(mapping.sampled_objects_json)
        except (TypeError, ValueError, AttributeError):
            sampled_objects = [mapping.object_name]
        if not isinstance(pgids, list):
            pgids = [mapping.pgid]
        if not isinstance(sampled_objects, list):
            sampled_objects = [mapping.object_name]
        topology_by_key[(mapping.pool, mapping.image)] = {
            "pgid": mapping.pgid,
            "pgids": [item for item in pgids if isinstance(item, str)],
            "acting_osds": [item for item in acting_osds if isinstance(item, int)],
            "primary_osd": mapping.primary_osd,
            "object_name": mapping.object_name,
            "sampled_objects": [item for item in sampled_objects if isinstance(item, str)],
            "data_object_count": mapping.data_object_count,
            "mapping_scope": mapping.mapping_scope,
            "captured_at": _iso(mapping.captured_at),
            "freshness": _freshness(now, mapping.captured_at, FRESH_DISTRIBUTION_SECONDS),
            "source": "volume_osd_mappings",
        }
    distribution_rows = session.query(CrushOsdDistribution).filter(
        CrushOsdDistribution.cluster_id == cluster_id,
    ).all()
    fresh_distribution_rows = []
    stale_distribution_rows = 0
    for row in distribution_rows:
        distribution_age = _age(now, row.updated_at)
        if (
            row.host
            and distribution_age is not None
            and distribution_age <= FRESH_DISTRIBUTION_SECONDS
        ):
            fresh_distribution_rows.append(row)
        else:
            stale_distribution_rows += 1
    # A failed CRUSH scan intentionally leaves the latest-known distribution
    # row in place. Do not use that row to attach host telemetry to an OSD:
    # an OSD may have moved hosts since the last successful scan.
    host_by_osd = {row.osd_id: row.host for row in fresh_distribution_rows}
    host_rows = session.query(HostMetricSample).filter(
        HostMetricSample.cluster_id == cluster_id,
        HostMetricSample.collected_at >= window_start,
        HostMetricSample.collected_at <= now,
    ).order_by(HostMetricSample.collected_at.asc()).all()
    fresh_host_rows = [
        row for row in host_rows
        if (_age(now, row.collected_at) or 0) <= FRESH_HOST_SECONDS
    ]
    host_samples = {}
    for row in fresh_host_rows:
        for identity in (row.node_name, row.host):
            key = _host_key(identity)
            if key:
                host_samples[key] = row
    analyses = []
    for key, history in grouped.items():
        latest = latest_map[key]
        pool_latest = [row for (row_pool, _), row in latest_map.items() if row_pool == latest.pool]
        if len(history) >= MIN_VOLUME_SAMPLES:
            analyses.append(_volume_analysis(
                history, latest, pool_latest, cluster_latest, osd_summary,
                topology_by_key.get(key),
                _host_evidence(topology_by_key.get(key), host_by_osd, host_samples, now),
            ))
    analyses.sort(key=lambda item: (item["confidence"], item["current_latency_ms"]), reverse=True)
    analyses = analyses[:MAX_VOLUME_REPORTS]
    host_join_count = sum(1 for item in analyses if item.get("host_evidence"))

    chain, distribution_citation = _distribution_chain(session, cluster_id, now)
    if topology_by_key:
        chain["pg"] = {
            **chain["pg"],
            "status": "mapped",
            "detail": f"Đã map {len(topology_by_key)} volume tới PG/acting OSD; PG count distribution vẫn lấy từ CRUSH OSD data.",
            "source": "volume_osd_mappings",
            "freshness": _freshness(now, fresh_mapping_latest_at, FRESH_DISTRIBUTION_SECONDS),
        }
    chain["volume"] = {
        "layer": "volume",
        "status": "observed" if analyses else "not_available",
        "detail": f"{len(analyses)} volume có tối thiểu {MIN_VOLUME_SAMPLES} mẫu trong cửa sổ.",
        "source": "volume_metrics",
        "freshness": _freshness(
            now,
            max((item.polled_at for item in rows), default=None),
            FRESH_VOLUME_SECONDS,
        ),
    }
    chain["pool"] = {
        "layer": "pool",
        "status": "observed" if analyses else "not_available",
        "detail": "Pool signal được suy ra từ các volume peer trong cùng pool.",
        "source": "volume_metrics",
        "freshness": chain["volume"]["freshness"],
    }
    chain["osd"] = {
        "layer": "osd",
        "status": "observed" if osd_summary["status"] == "observed" else "not_available",
        "detail": osd_summary["reason"],
        "source": "ceph_osd_perf_live",
        "freshness": osd_summary.get("freshness"),
    }
    chain["disk"] = {
        "layer": "disk",
        "status": (
            "observed" if fresh_host_rows
            else "stale" if host_rows
            else "proxy_only" if osd_summary.get("outliers") else "not_available"
        ),
        "detail": (
            "Có host disk IOPS/latency sample cùng time window."
            if fresh_host_rows
            else "Host disk sample đã stale quá 5 phút; không dùng làm evidence hiện tại."
            if host_rows
            else "OSD commit latency chỉ là disk/OSD proxy; chưa có host disk sample."
        ),
        "source": "host_metric_samples" if host_rows else "ceph_osd_perf_live",
        "freshness": _freshness(now, max((row.collected_at for row in host_rows), default=None), FRESH_HOST_SECONDS)
        if host_rows else osd_summary.get("freshness"),
    }
    if host_rows:
        chain["host"] = {
            "layer": "host",
            "status": (
                "observed" if host_join_count
                else "unscoped" if fresh_host_rows
                else "stale"
            ),
            "detail": (
                f"Có {len(host_rows)} host metric samples; {host_join_count} volume analysis "
                "join được với OSD→hostname."
                if fresh_host_rows
                else f"Có {len(host_rows)} host metric samples nhưng tất cả đã stale quá 5 phút."
            ),
            "source": "host_metric_samples",
            "freshness": _freshness(now, max(row.collected_at for row in host_rows), FRESH_HOST_SECONDS),
        }

    citations = [{
        "source_id": "volume_metrics",
        "observed_at": chain["volume"]["freshness"]["observed_at"],
        "window_start": _iso(window_start),
        "window_end": _iso(now),
        "row_count": len(rows),
    }]
    if distribution_citation:
        citations.append(distribution_citation)
    if live_signals and live_signals.get("status") == "ready":
        citations.append({
            "source_id": "ceph_osd_perf_live",
            "observed_at": live_signals.get("freshness", {}).get("observed_at"),
            "measured_osds": live_signals.get("measured_osds", 0),
        })
    if host_rows:
        citations.append({
            "source_id": "host_metric_samples",
            "observed_at": _iso(max(row.collected_at for row in host_rows)),
            "row_count": len(host_rows),
            "fresh_row_count": len(fresh_host_rows),
        })

    gaps = []
    if not analyses:
        gaps.append("Chưa đủ lịch sử VolumeMetric cho volume nào trong cửa sổ.")
    if topology_by_key:
        gaps.append("Mapping PG/OSD là latest snapshot; chưa có lịch sử acting-set để chứng minh thay đổi theo thời gian.")
    elif stale_or_legacy_mappings:
        gaps.append(f"Có {stale_or_legacy_mappings} mapping stale/legacy; không dùng để suy luận causal.")
    else:
        gaps.append("Chưa có volume→PG→OSD acting-set mapping trong dữ liệu hiện tại.")
    if not host_rows:
        gaps.append("Chưa có host disk/SMART/network sample được lưu theo cùng time window.")
    elif not fresh_host_rows:
        gaps.append("Có host metrics nhưng tất cả sample đã stale quá 5 phút; không dùng để suy luận.")
    elif not host_join_count:
        gaps.append("Có host metrics nhưng chưa join được với acting OSD của volume nào trong cửa sổ.")
    if stale_distribution_rows:
        gaps.append(
            f"Có {stale_distribution_rows} OSD→host mapping stale/thiếu host; không dùng cho host correlation."
        )
    return {
        "status": "ready" if analyses else "insufficient_evidence",
        "conclusion": "correlation_only",
        "cluster_id": cluster_id,
        "captured_at": _iso(now),
        "window": {"hours": window_hours, "start": _iso(window_start), "end": _iso(now)},
        "scope": {"pool": pool, "image": image},
        "analyses": analyses,
        "chain": [chain[layer] for layer in ("volume", "pool", "pg", "osd", "disk", "host")],
        "evidence_gaps": gaps,
        "_citations": citations,
    }


def collect_live_osd_signals(cluster) -> dict:
    """Collect only cheap, read-only OSD latency evidence for one cluster."""
    captured_at = datetime.utcnow()
    try:
        nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
        ssh_user, ssh_key_path, exec_mode, container_name = resolve_ssh_creds(cluster)
        connection = (nodes, container_name, ssh_user, ssh_key_path, exec_mode)
        _stdout, perf_payload = ceph_client.run_ceph_json_command_with(
            *connection, "ceph osd perf",
        )
        _stdout, tree_payload = ceph_client.run_ceph_json_command_with(
            *connection, "ceph osd tree",
        )
        if not isinstance(perf_payload, dict) or not isinstance(tree_payload, (dict, list)):
            raise ValueError("ceph response không đúng dạng JSON")
        infos = (perf_payload.get("osdstats") or {}).get("osd_perf_infos", [])
        if not isinstance(infos, list):
            raise ValueError("ceph osd perf thiếu osd_perf_infos")
        host_by_id = {
            item["osd_id"]: item.get("crush_host")
            for item in ceph_client._normalize_osd_tree(tree_payload)
        }
        values = {
            int(item["id"]): float((item.get("perf_stats") or {}).get("commit_latency_ms"))
            for item in infos
            if isinstance(item, dict)
            and isinstance(item.get("id"), int)
            and isinstance((item.get("perf_stats") or {}).get("commit_latency_ms"), (int, float))
        }
        if not values:
            raise ValueError("ceph osd perf không trả latency hợp lệ")
        median = float(statistics.median(values.values()))
        outliers = []
        for osd_id, value in values.items():
            ratio = value / max(median, 0.1)
            if ratio >= 3.0 and value >= 5.0:
                outliers.append({
                    "osd_id": osd_id,
                    "host": host_by_id.get(osd_id),
                    "commit_latency_ms": round(value, 3),
                    "ratio": round(ratio, 3),
                })
        outliers.sort(key=lambda item: item["ratio"], reverse=True)
        return {
            "status": "ready",
            "measured_osds": len(values),
            "median_commit_latency_ms": round(median, 3),
            "outliers": outliers,
            "freshness": _freshness(captured_at, captured_at, 300),
        }
    except (CephQueryError, ValueError, KeyError, TypeError, AttributeError) as exc:
        logger.info("performance RCA live OSD evidence unavailable: %s", exc)
        return {
            "status": "unavailable",
            "reason": "Không lấy được ceph osd perf ở thời điểm report.",
            "freshness": _freshness(captured_at, captured_at, 300),
        }


def report(cluster, **kwargs) -> dict:
    live_signals = kwargs.pop("live_signals", None)
    if live_signals is None:
        live_signals = collect_live_osd_signals(cluster)
    with db.SessionLocal() as session:
        return build_report(session, cluster.id, live_signals=live_signals, **kwargs)
