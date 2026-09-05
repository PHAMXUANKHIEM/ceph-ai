"""Background Vitastor health polling and transition-scoped Telegram alerts."""

from __future__ import annotations

import json
import logging
import time
import statistics
from datetime import datetime, timedelta

from config.settings import settings
from shared import db
from shared.models import VitastorCluster, VitastorMetricSample, VitastorNetworkMetricSample, VitastorNodeMetricSample, VitastorOsdMetricSample
from shared.telegram_alerts import send_vitastor_alert
from vitastor.client import VitastorConnectionError, normalize_etcd, normalize_status, query_dashboard
from vitastor.anomaly import detect_and_record, extract_entities
from vitastor.node_metrics import query_node_hardware, query_node_network
from vitastor.remediation import reconcile_monitor_proposals

logger = logging.getLogger(__name__)
PROBLEM_STATES = {"WARNING", "CRITICAL", "UNREACHABLE"}


def _cached(cluster: VitastorCluster) -> dict:
    try:
        value = json.loads(cluster.last_status_json or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _connection_args(cluster: VitastorCluster) -> tuple:
    return (
        cluster.management_host, cluster.ssh_user, cluster.ssh_key_path,
        cluster.etcd_address, cluster.etcd_prefix, cluster.config_path,
        cluster.exec_mode, cluster.container_name,
    )


def _health_detail(summary: dict) -> str:
    health = summary.get("health", "UNKNOWN")
    etcd, osds, pools = summary.get("etcd", {}), summary.get("osds", {}), summary.get("pools", {})
    parts = [
        f"Health {health}",
        f"Etcd {etcd.get('up', 0)}/{etcd.get('total', 0)} up",
        f"OSD {osds.get('up', 0)}/{osds.get('total', 0)} up",
        f"Pool {pools.get('active', 0)}/{pools.get('total', 0)} active",
    ]
    bad_pgs = [f"{state}={count}" for state, count in summary.get("pg_states", {}).items() if state != "active"]
    if bad_pgs:
        parts.append("PG " + ", ".join(bad_pgs[:6]))
    if osds.get("full") or osds.get("nearfull"):
        parts.append(f"OSD full={osds.get('full', 0)}, nearfull={osds.get('nearfull', 0)}")
    if summary.get("flags"):
        parts.append("Flags " + ", ".join(summary["flags"]))
    return " · ".join(parts)


def _persist(cluster_id: str, cache: dict, alert_state: str) -> None:
    cache["_telegram_health"] = alert_state
    with db.SessionLocal() as session:
        row = session.get(VitastorCluster, cluster_id)
        if row is not None:
            row.last_status_json = json.dumps(cache)
            row.last_checked_at = datetime.utcnow()
            session.commit()


def _number(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _latency_ms(stats: dict) -> float | None:
    if isinstance(stats.get("latency_ms"), (int, float)):
        return float(stats["latency_ms"])
    for key in ("latency_us", "usec", "lat"):
        if isinstance(stats.get(key), (int, float)):
            return float(stats[key]) / 1000
    return None


def _record_metrics(cluster_id: str, datasets: dict, summary: dict, etcd_detail: dict) -> None:
    """Persist one bounded cluster sample and one row for every OSD in this scan."""
    io = summary.get("io") or {}
    read = io.get("read") if isinstance(io.get("read"), dict) else {}
    write = io.get("write") if isinstance(io.get("write"), dict) else {}
    recovery = summary.get("recovery") or {}
    recovery_bps = sum(
        _number(value.get("bps")) for value in recovery.values() if isinstance(value, dict)
    )
    capacity, etcd, osds = summary.get("capacity", {}), summary.get("etcd", {}), summary.get("osds", {})
    now = datetime.utcnow()
    with db.SessionLocal() as session:
        session.add(VitastorMetricSample(
            cluster_id=cluster_id, health=str(summary.get("health") or "UNKNOWN"),
            osd_up=int(osds.get("up") or 0), osd_total=int(osds.get("total") or 0),
            used_bytes=int(capacity.get("used") or 0), free_bytes=int(capacity.get("free") or 0),
            used_percent=_number(capacity.get("used_percent")), etcd_up=int(etcd.get("up") or 0),
            etcd_total=int(etcd.get("total") or 0), etcd_latency_ms=etcd_detail.get("latency_ms"),
            etcd_quorum=etcd_detail.get("quorum") if etcd_detail.get("total") else None,
            etcd_leader_count=int(etcd_detail.get("leader_count") or 0), read_iops=_number(read.get("iops")),
            write_iops=_number(write.get("iops")), read_bps=_number(read.get("bps")),
            write_bps=_number(write.get("bps")), read_latency_ms=_latency_ms(read),
            write_latency_ms=_latency_ms(write), recovery_bps=recovery_bps,
            degraded_bytes=int((summary.get("data_states") or {}).get("degraded") or 0),
            raw_json=json.dumps(datasets.get("status") or {}), collected_at=now,
        ))
        for item in datasets.get("osds") or []:
            if not isinstance(item, dict) or item.get("type") != "osd":
                continue
            size, free = int(item.get("size") or 0), int(item.get("free") or 0)
            item_io = item.get("op_stats") if isinstance(item.get("op_stats"), dict) else {}
            item_read = item_io.get("read") if isinstance(item_io.get("read"), dict) else {}
            item_write = item_io.get("write") if isinstance(item_io.get("write"), dict) else {}
            used = max(0, size - free)
            session.add(VitastorOsdMetricSample(
                cluster_id=cluster_id, osd_id=str(item.get("name") or item.get("id") or "unknown"),
                host=str(item.get("parent") or ""), is_up=bool(item.get("up")), size_bytes=size,
                used_bytes=used, used_percent=(used / size * 100 if size else 0),
                read_iops=_number(item_read.get("iops")), write_iops=_number(item_write.get("iops")),
                read_bps=_number(item_read.get("bps")), write_bps=_number(item_write.get("bps")),
                read_latency_ms=_latency_ms(item_read), write_latency_ms=_latency_ms(item_write),
                raw_json=json.dumps(item), collected_at=now,
            ))
        cutoff = now - timedelta(days=max(1, settings.vitastor_metric_retention_days))
        session.query(VitastorMetricSample).filter(VitastorMetricSample.collected_at < cutoff).delete()
        session.query(VitastorOsdMetricSample).filter(VitastorOsdMetricSample.collected_at < cutoff).delete()
        session.commit()


def _capacity_level(percent: float) -> str:
    if percent >= settings.vitastor_capacity_critical_percent:
        return "CRITICAL"
    if percent >= settings.vitastor_capacity_warning_percent:
        return "WARNING"
    return "HEALTHY"


def _capacity_alerts(cluster: VitastorCluster, datasets: dict, summary: dict, cache: dict) -> None:
    previous = cache.get("_telegram_capacity")
    previous = previous if isinstance(previous, dict) else {}
    current: dict[str, str] = {}
    entities = [("cluster", "Toàn cụm", _number((summary.get("capacity") or {}).get("used_percent")))]
    for item in datasets.get("osds") or []:
        if not isinstance(item, dict) or item.get("type") != "osd":
            continue
        size, free = _number(item.get("size")), _number(item.get("free"))
        percent = max(0.0, (size - free) / size * 100) if size else 0.0
        osd_id = str(item.get("name") or item.get("id") or "unknown")
        entities.append((f"osd:{osd_id}", f"OSD {osd_id} ({item.get('parent') or 'không rõ host'})", percent))
    for key, label, percent in entities:
        level = _capacity_level(percent)
        current[key] = level
        old = str(previous.get(key) or "")
        if level in PROBLEM_STATES and level != old:
            send_vitastor_alert(
                cluster.name, level,
                f"Dung lượng {label}: {percent:.2f}% đã dùng · ngưỡng WARNING "
                f"{settings.vitastor_capacity_warning_percent:g}% / CRITICAL "
                f"{settings.vitastor_capacity_critical_percent:g}%",
            )
        elif level == "HEALTHY" and old in PROBLEM_STATES:
            send_vitastor_alert(cluster.name, "HEALTHY", f"Dung lượng {label} đã phục hồi: {percent:.2f}% đã dùng")
    cache["_telegram_capacity"] = current


def _etcd_alert(cluster: VitastorCluster, detail: dict, cache: dict, error: str | None = None) -> None:
    if error:
        previous = str(cache.get("_telegram_etcd") or "")
        if previous != "UNREACHABLE":
            send_vitastor_alert(cluster.name, "UNREACHABLE", f"Không lấy được Etcd detail: {error}")
        cache["_telegram_etcd"] = "UNREACHABLE"
        return
    total, healthy = int(detail.get("total") or 0), int(detail.get("healthy") or 0)
    if not total:
        return
    latency = detail.get("latency_ms")
    if total and (not detail.get("quorum") or int(detail.get("leader_count") or 0) != 1):
        level = "CRITICAL"
    elif isinstance(latency, (int, float)) and latency >= settings.vitastor_etcd_latency_critical_ms:
        level = "CRITICAL"
    elif isinstance(latency, (int, float)) and latency >= settings.vitastor_etcd_latency_warning_ms:
        level = "WARNING"
    else:
        level = "HEALTHY"
    previous = str(cache.get("_telegram_etcd") or "")
    latency_text = f"{latency:.2f} ms" if isinstance(latency, (int, float)) else "không có dữ liệu"
    if level in PROBLEM_STATES and level != previous:
        send_vitastor_alert(cluster.name, level, f"Etcd {healthy}/{total} healthy · leader {detail.get('leader_count', 0)} · latency {latency_text}")
    elif level == "HEALTHY" and previous in PROBLEM_STATES:
        send_vitastor_alert(cluster.name, "HEALTHY", f"Etcd đã phục hồi · {healthy}/{total} healthy · latency {latency_text}")
    cache["_telegram_etcd"] = level


def _recovery_and_data_alerts(cluster: VitastorCluster, summary: dict, cache: dict) -> None:
    states = summary.get("data_states") or {}
    affected = int(states.get("degraded") or 0) + int(states.get("incomplete") or 0)
    data_level = "CRITICAL" if int(states.get("incomplete") or 0) else "WARNING" if affected else "HEALTHY"
    old_data = str(cache.get("_telegram_data_integrity") or "")
    if data_level in PROBLEM_STATES and data_level != old_data:
        send_vitastor_alert(cluster.name, data_level, f"Data integrity: degraded={states.get('degraded', 0)} bytes · incomplete={states.get('incomplete', 0)} bytes")
    elif data_level == "HEALTHY" and old_data in PROBLEM_STATES:
        send_vitastor_alert(cluster.name, "HEALTHY", "Dữ liệu đã trở lại CLEAN, không còn degraded/incomplete")
    cache["_telegram_data_integrity"] = data_level

    recovery = summary.get("recovery") or {}
    bps = sum(_number(value.get("bps")) for value in recovery.values() if isinstance(value, dict))
    mbps = bps / 1_000_000
    level = "CRITICAL" if mbps >= settings.vitastor_recovery_critical_mbps else "WARNING" if mbps >= settings.vitastor_recovery_warning_mbps else "HEALTHY"
    old = str(cache.get("_telegram_recovery") or "")
    if level in PROBLEM_STATES and level != old:
        send_vitastor_alert(cluster.name, level, f"Recovery/Rebalance đang dùng {mbps:.1f} MB/s; có thể làm tăng latency ứng dụng")
    elif level == "HEALTHY" and old in PROBLEM_STATES:
        send_vitastor_alert(cluster.name, "HEALTHY", f"Recovery/Rebalance đã giảm còn {mbps:.1f} MB/s")
    cache["_telegram_recovery"] = level


def _slow_osd_alerts(cluster: VitastorCluster, datasets: dict, cache: dict) -> None:
    latencies: dict[str, tuple[float, str]] = {}
    for item in datasets.get("osds") or []:
        if not isinstance(item, dict) or item.get("type") != "osd" or not item.get("up"):
            continue
        stats = item.get("op_stats") if isinstance(item.get("op_stats"), dict) else {}
        read = stats.get("read") if isinstance(stats.get("read"), dict) else {}
        write = stats.get("write") if isinstance(stats.get("write"), dict) else {}
        values = [value for value in (_latency_ms(read), _latency_ms(write)) if value is not None]
        if values:
            latencies[str(item.get("name") or item.get("id") or "unknown")] = (max(values), str(item.get("parent") or ""))
    if not latencies:
        return
    median = statistics.median(value[0] for value in latencies.values())
    streaks = cache.get("_slow_osd_streaks")
    streaks = streaks if isinstance(streaks, dict) else {}
    previous = cache.get("_telegram_slow_osds")
    previous_set = set(previous) if isinstance(previous, list) else set()
    slow = set()
    for osd_id, (latency, _host) in latencies.items():
        candidate = latency >= settings.vitastor_slow_osd_latency_ms and latency >= median * settings.vitastor_slow_osd_median_multiplier
        streaks[osd_id] = int(streaks.get(osd_id, 0)) + 1 if candidate else 0
        if streaks[osd_id] >= max(1, settings.vitastor_slow_osd_consecutive_scans):
            slow.add(osd_id)
    for osd_id in sorted(slow - previous_set):
        latency, host = latencies[osd_id]
        send_vitastor_alert(cluster.name, "WARNING", f"Slow OSD {osd_id} ({host}): {latency:.2f} ms · median cụm {median:.2f} ms")
    for osd_id in sorted(previous_set - slow):
        send_vitastor_alert(cluster.name, "HEALTHY", f"OSD {osd_id} không còn là latency outlier")
    cache["_slow_osd_streaks"] = streaks
    cache["_telegram_slow_osds"] = sorted(slow)


def _collect_hardware(cluster: VitastorCluster, datasets: dict, cache: dict) -> list[dict]:
    hosts = sorted({str(item.get("parent") or "").split("/", 1)[0] for item in datasets.get("osds") or [] if isinstance(item, dict) and item.get("type") == "osd" and item.get("parent")})
    results = []
    for host in hosts:
        try:
            results.append(query_node_hardware(host, cluster.ssh_user, cluster.ssh_key_path))
        except Exception as exc:
            logger.warning("Vitastor hardware scan failed for %s: %s", host, exc)
            results.append({"host": host, "error": str(exc), "devices": []})
    now = datetime.utcnow()
    with db.SessionLocal() as session:
        for result in results:
            devices = result.get("devices") or []
            temperatures = [_number(d.get("temperature_c")) for d in devices if d.get("temperature_c") is not None]
            wear = [_number(d.get("wear_percent")) for d in devices if d.get("wear_percent") is not None]
            session.add(VitastorNodeMetricSample(cluster_id=cluster.id, host=result["host"], osd_processes=int(result.get("osd_processes") or 0), cpu_percent=_number(result.get("cpu_percent")), ram_bytes=int(result.get("ram_bytes") or 0), max_temperature_c=max(temperatures) if temperatures else None, max_wear_percent=max(wear) if wear else None, media_errors=sum(int(d.get("media_errors") or 0) for d in devices), smart_failing=any(d.get("smart_passed") is False for d in devices), raw_json=json.dumps(result), collected_at=now))
        cutoff = now - timedelta(days=max(1, settings.vitastor_metric_retention_days))
        session.query(VitastorNodeMetricSample).filter(VitastorNodeMetricSample.collected_at < cutoff).delete()
        session.commit()
    previous = cache.get("_telegram_hardware")
    previous = previous if isinstance(previous, dict) else {}
    current = {}
    for result in results:
        for device in result.get("devices") or []:
            key = f"{result['host']}:{device.get('device')}"
            temp, wear, errors = device.get("temperature_c"), device.get("wear_percent"), int(device.get("media_errors") or 0)
            critical = device.get("smart_passed") is False or errors > 0 or (isinstance(temp, (int, float)) and temp >= settings.vitastor_disk_temperature_critical_c) or (isinstance(wear, (int, float)) and wear >= settings.vitastor_disk_wear_critical_percent)
            warning = (isinstance(temp, (int, float)) and temp >= settings.vitastor_disk_temperature_warning_c) or (isinstance(wear, (int, float)) and wear >= settings.vitastor_disk_wear_warning_percent)
            level = "CRITICAL" if critical else "WARNING" if warning else "HEALTHY"; current[key] = level
            old = str(previous.get(key) or "")
            detail = f"{key} · temperature={temp}°C · wear={wear}% · media_errors={errors}"
            if level in PROBLEM_STATES and level != old: send_vitastor_alert(cluster.name, level, "Hardware " + detail)
            elif level == "HEALTHY" and old in PROBLEM_STATES: send_vitastor_alert(cluster.name, "HEALTHY", "Hardware đã phục hồi " + detail)
    cache["_telegram_hardware"] = current
    return results


def _collect_network(cluster: VitastorCluster, datasets: dict, cache: dict) -> list[dict]:
    osd_hosts = sorted({str(item.get("parent") or "").split("/", 1)[0] for item in datasets.get("osds") or [] if isinstance(item, dict) and item.get("type") == "osd" and item.get("parent")})[:max(1, settings.vitastor_network_max_nodes)]
    sources = list(osd_hosts)
    if cluster.management_host not in sources: sources.append(cluster.management_host)
    results = []
    for source in sources:
        targets = [host for host in osd_hosts if host != source]
        if not targets: continue
        try: results.append(query_node_network(source, targets, cluster.ssh_user, cluster.ssh_key_path))
        except Exception as exc:
            logger.warning("Vitastor network scan failed for %s: %s", source, exc)
            results.append({"source": source, "interfaces": [], "probes": [], "error": str(exc)})
    now = datetime.utcnow()
    with db.SessionLocal() as session:
        for result in results:
            for probe in result.get("probes") or []:
                session.add(VitastorNetworkMetricSample(cluster_id=cluster.id, source=result["source"], target=probe["target"], reachable=bool(probe.get("reachable")), rtt_ms=probe.get("rtt_ms"), jumbo_9000=bool(probe.get("jumbo_9000")), interface_json=json.dumps(result.get("interfaces") or []), collected_at=now))
        cutoff = now - timedelta(days=max(1, settings.vitastor_metric_retention_days))
        session.query(VitastorNetworkMetricSample).filter(VitastorNetworkMetricSample.collected_at < cutoff).delete()
        session.commit()
    previous = cache.get("_telegram_network") if isinstance(cache.get("_telegram_network"), dict) else {}
    old_counters = cache.get("_network_counters") if isinstance(cache.get("_network_counters"), dict) else {}
    current, counters = {}, {}
    for result in results:
        source = result["source"]
        nic_delta = 0
        for nic in result.get("interfaces") or []:
            key = f"{source}:{nic.get('name')}"
            total = sum(int(nic.get(field) or 0) for field in ("rx_errors", "rx_dropped", "tx_errors", "tx_dropped"))
            counters[key] = total; nic_delta += max(0, total - int(old_counters.get(key, total)))
        for probe in result.get("probes") or []:
            key = f"{source}->{probe['target']}"; rtt = probe.get("rtt_ms")
            critical = not probe.get("reachable") or (isinstance(rtt, (int, float)) and rtt >= settings.vitastor_network_rtt_critical_ms)
            warning = nic_delta > 0 or (isinstance(rtt, (int, float)) and rtt >= settings.vitastor_network_rtt_warning_ms) or (settings.vitastor_expect_jumbo_frames and not probe.get("jumbo_9000"))
            level = "CRITICAL" if critical else "WARNING" if warning else "HEALTHY"; current[key] = level
            old = str(previous.get(key) or "")
            detail = f"Network {key} · RTT={rtt} ms · jumbo9000={probe.get('jumbo_9000')} · NIC errors/drops tăng={nic_delta}"
            if level in PROBLEM_STATES and level != old: send_vitastor_alert(cluster.name, level, detail)
            elif level == "HEALTHY" and old in PROBLEM_STATES: send_vitastor_alert(cluster.name, "HEALTHY", detail + " · đã phục hồi")
    cache["_telegram_network"], cache["_network_counters"] = current, counters
    return results


def poll_cluster_once(cluster: VitastorCluster) -> str:
    """Poll one cluster, alert only on state transitions, and return its state."""
    cache = _cached(cluster)
    checked_at = datetime.utcnow().isoformat() + "Z"
    previous = str(cache.get("_telegram_health") or "")
    try:
        datasets = query_dashboard(*_connection_args(cluster))
    except VitastorConnectionError as exc:
        current = "UNREACHABLE"
        if previous != current:
            send_vitastor_alert(cluster.name, current, f"Không kết nối được management host {cluster.management_host}: {exc}")
        cache["checked_at"] = checked_at
        cache["monitor_error"] = str(exc)
        _persist(cluster.id, cache, current)
        cluster.last_status_json = json.dumps(cache)
        return current

    summary = normalize_status(datasets["status"])
    etcd_detail = normalize_etcd(datasets.get("etcd_status"), datasets.get("etcd_health"))
    errors = datasets.get("errors") if isinstance(datasets.get("errors"), dict) else {}
    etcd_error = "; ".join(
        f"{key}: {value}" for key, value in errors.items()
        if key in {"etcd_status", "etcd_health"} and value
    ) or None
    anomalies = detect_and_record(cluster.id, extract_entities(datasets, summary))
    for explanation in anomalies["opened"]:
        send_vitastor_alert(cluster.name, "WARNING", "Dynamic anomaly: " + explanation)
    for explanation in anomalies["resolved"]:
        send_vitastor_alert(cluster.name, "HEALTHY", "Dynamic anomaly đã phục hồi: " + explanation)
    _record_metrics(cluster.id, datasets, summary, etcd_detail)
    _capacity_alerts(cluster, datasets, summary, cache)
    _etcd_alert(cluster, etcd_detail, cache, etcd_error)
    _recovery_and_data_alerts(cluster, summary, cache)
    _slow_osd_alerts(cluster, datasets, cache)
    hardware = _collect_hardware(cluster, datasets, cache)
    network = _collect_network(cluster, datasets, cache)
    # Closed-loop remediation: turn observed faults (a down OSD) into
    # approval-gated proposals, auto-run any SAFE ones, and alert on new
    # RISKY pending. Isolated in its own try — a remediation hiccup must
    # never abort the health poll or block cache persistence below.
    try:
        for proposal in reconcile_monitor_proposals(cluster, datasets, summary):
            send_vitastor_alert(
                cluster.name, "WARNING",
                "Đề xuất khắc phục (chờ duyệt): " + (proposal.get("rationale") or proposal.get("action_id", "")),
            )
    except Exception:
        logger.exception("Vitastor remediation reconcile failed for %r", cluster.name)
    current = summary["health"]
    if current in PROBLEM_STATES and current != previous:
        send_vitastor_alert(cluster.name, current, _health_detail(summary))
    elif current == "HEALTHY" and previous in PROBLEM_STATES:
        send_vitastor_alert(cluster.name, current, "Cluster đã phục hồi · " + _health_detail(summary))

    cache.update({
        "summary": summary,
        "checked_at": checked_at,
        "etcd_detail": etcd_detail,
        "hardware": hardware,
        "network": network,
        "anomalies": anomalies["open"],
        "pools": datasets.get("pools") if isinstance(datasets.get("pools"), list) else [],
        "osds": datasets.get("osds") if isinstance(datasets.get("osds"), list) else [],
        "images": datasets.get("images") if isinstance(datasets.get("images"), list) else [],
        "section_errors": datasets.get("errors") or {},
        "monitor_error": None,
    })
    _persist(cluster.id, cache, current)
    cluster.last_status_json = json.dumps(cache)
    return current


def run_cluster_loop(cluster: VitastorCluster, max_iterations: int | None = None) -> None:
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        try:
            poll_cluster_once(cluster)
        except Exception:
            logger.exception("Unexpected Vitastor monitor failure for %r", cluster.name)
        iterations += 1
        if max_iterations is None or iterations < max_iterations:
            time.sleep(max(5, settings.vitastor_poll_interval_seconds))


def run_all_clusters_loop(max_iterations: int | None = None) -> None:
    """Discover active clusters every cycle so newly-added rows need no restart."""
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        with db.SessionLocal() as session:
            clusters = session.query(VitastorCluster).filter(VitastorCluster.is_active.is_(True)).all()
            session.expunge_all()
        for cluster in clusters:
            try:
                poll_cluster_once(cluster)
            except Exception:
                logger.exception("Unexpected Vitastor monitor failure for %r", cluster.name)
        iterations += 1
        if max_iterations is None or iterations < max_iterations:
            time.sleep(max(5, settings.vitastor_poll_interval_seconds))
