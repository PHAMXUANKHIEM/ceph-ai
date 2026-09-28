"""Read-only view of Ceph's own operational features (autonomy plan WP4).

Ceph already ships the pieces a self-operating system needs for disks and
data placement: the ``devicehealth`` and ``diskprediction_local`` mgr
modules (SMART scraping and life-expectancy prediction), ``pg_autoscaler``
and ``balancer``.  This module reports whether they are on, whether they
can actually work on this cluster (virtual disks expose no SMART data, so
a prediction module has nothing to learn from) and what an operator could
change.  It never changes anything: recommendations are text for a human,
and every command it runs comes from a closed, read-only set.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from shared.time import utc_now

# Closed set of read-only commands (same "no free-form command" posture as
# dashboard/ceph_tools.FIXED_TOOL_COMMANDS).
FEATURE_COMMANDS: dict[str, str] = {
    "mgr_modules": "ceph mgr module ls",
    "devices": "ceph device ls",
    "balancer": "ceph balancer status",
    "autoscale": "ceph osd pool autoscale-status",
}
HEALTH_METRICS_COMMAND = "ceph device get-health-metrics {devid}"
HEALTH_METRICS_SAMPLE = 3
_DEVID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

OK, INFO, WARN, UNAVAILABLE = "ok", "info", "warn", "unavailable"
Runner = Callable[[str], Any]


def parse_mgr_modules(payload: Any) -> dict[str, str]:
    """Module name -> always_on | enabled | disabled | force_disabled.

    ``always_on_modules`` is a list on recent releases and a dict keyed by
    release name on older ones; disabled modules are dicts with a ``name``.
    """
    if not isinstance(payload, dict):
        return {}
    states: dict[str, str] = {}

    def names(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [name for items in value.values() for name in names(items)]
        if isinstance(value, list):
            return [item.get("name", "") if isinstance(item, dict) else str(item) for item in value]
        return []

    for key, state in (("disabled_modules", "disabled"), ("force_disabled_modules", "force_disabled"),
                       ("enabled_modules", "enabled"), ("always_on_modules", "always_on")):
        for name in names(payload.get(key)):
            if name:
                states[name] = state
    return states


def _is_on(modules: dict[str, str], name: str) -> bool:
    return modules.get(name) in {"always_on", "enabled"}


def parse_devices(payload: Any) -> list[dict]:
    rows = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict) or not item.get("devid"):
            continue
        location = (item.get("location") or [{}])[0] or {}
        rows.append({
            "devid": str(item["devid"]),
            "host": location.get("host"),
            "dev": location.get("dev"),
            "daemons": list(item.get("daemons") or []),
            "life_expectancy_min": item.get("life_expectancy_min") or None,
            "life_expectancy_max": item.get("life_expectancy_max") or None,
        })
    return rows


# Fields smartctl's JSON output carries when it actually read the device.
_SMART_KEYS = ("smart_status", "ata_smart_attributes", "nvme_smart_health_information_log",
               "scsi_grown_defect_list", "power_on_time")


def smart_state(payload: Any) -> str:
    """``ok`` when a scrape read real SMART data, ``failed`` when every scrape
    is a smartctl error (devicehealth still stores those entries), ``none``
    when nothing was scraped."""
    entries = [entry for entry in (payload.values() if isinstance(payload, dict) else []) if isinstance(entry, dict)]
    if any(not entry.get("error") and any(key in entry for key in _SMART_KEYS) for entry in entries):
        return "ok"
    return "failed" if entries else "none"


def _metrics_sample(devices: list[dict], size: int = HEALTH_METRICS_SAMPLE) -> list[str]:
    """OSD devices first: those are the disks a prediction is useful for."""
    ordered = sorted(devices, key=lambda row: not any(str(d).startswith("osd.") for d in row["daemons"]))
    return [row["devid"] for row in ordered if _DEVID_RE.match(row["devid"])][:size]


def _feature(feature_id: str, title: str, state: str, summary: str, recommendation: str = "") -> dict:
    return {"id": feature_id, "title": title, "state": state, "summary": summary,
            "recommendation": recommendation}


def _devicehealth(modules: dict[str, str]) -> dict:
    if _is_on(modules, "devicehealth"):
        return _feature("devicehealth", "Device health (SMART)", OK, "Module devicehealth đang bật.")
    return _feature("devicehealth", "Device health (SMART)", WARN, "Module devicehealth đang tắt.",
                    "Bật lại (cần duyệt): ceph mgr module enable devicehealth")


def _prediction(modules: dict[str, str], devices: list[dict], sampled: int, with_metrics: int,
                smart_failed: int) -> dict:
    predicted = sum(1 for row in devices if row["life_expectancy_min"] or row["life_expectancy_max"])
    enabled = _is_on(modules, "diskprediction_local")
    no_smart = sampled > 0 and with_metrics == 0
    failed_note = f" smartctl lỗi trên {smart_failed} device." if smart_failed else ""
    if enabled:
        state = OK if predicted else INFO
        summary = f"diskprediction_local đang bật; {predicted}/{len(devices)} device có dự đoán tuổi thọ."
        if no_smart:
            summary += " Không device nào có dữ liệu SMART nên chưa thể dự đoán." + failed_note
        return _feature("diskprediction_local", "Dự đoán tuổi thọ đĩa", state, summary)
    if no_smart:
        return _feature(
            "diskprediction_local", "Dự đoán tuổi thọ đĩa", INFO,
            f"diskprediction_local đang tắt và 0/{sampled} device lấy mẫu có dữ liệu SMART "
            f"(thường gặp với đĩa ảo virtio).{failed_note}",
            "Không cần bật: module không có dữ liệu để dự đoán trên cluster này.",
        )
    if sampled == 0:
        return _feature("diskprediction_local", "Dự đoán tuổi thọ đĩa", UNAVAILABLE,
                        "Không đọc được dữ liệu SMART để đánh giá.")
    return _feature(
        "diskprediction_local", "Dự đoán tuổi thọ đĩa", WARN,
        f"diskprediction_local đang tắt dù {with_metrics}/{sampled} device lấy mẫu có dữ liệu SMART.",
        "Nên bật (cần duyệt): ceph mgr module enable diskprediction_local",
    )


def _autoscaler(modules: dict[str, str], pools: Any) -> dict:
    if not _is_on(modules, "pg_autoscaler"):
        return _feature("pg_autoscaler", "PG autoscaler", WARN, "Module pg_autoscaler đang tắt.",
                        "Cân nhắc bật (cần duyệt): ceph mgr module enable pg_autoscaler")
    if not isinstance(pools, list):
        return _feature("pg_autoscaler", "PG autoscaler", UNAVAILABLE, "Không đọc được autoscale-status.")
    manual = [p.get("pool_name") for p in pools if p.get("pg_autoscale_mode") != "on" and p.get("would_adjust")]
    pending = [p.get("pool_name") for p in pools if p.get("pg_autoscale_mode") == "on" and p.get("would_adjust")]
    if manual:
        return _feature(
            "pg_autoscaler", "PG autoscaler", WARN,
            f"{len(manual)} pool cần đổi pg_num nhưng autoscale không ở chế độ on: {', '.join(manual[:5])}.",
            "Xem lại pg_num hoặc đặt pg_autoscale_mode=on cho các pool này (cần duyệt).",
        )
    summary = f"{len(pools)} pool; autoscaler không đề xuất thay đổi."
    if pending:
        summary = f"{len(pools)} pool; autoscaler sẽ tự điều chỉnh {len(pending)} pool: {', '.join(pending[:5])}."
    return _feature("pg_autoscaler", "PG autoscaler", INFO if pending else OK, summary)


def _balancer(modules: dict[str, str], status: Any) -> dict:
    if not _is_on(modules, "balancer") or not isinstance(status, dict):
        return _feature("balancer", "Balancer", UNAVAILABLE, "Không đọc được trạng thái balancer.")
    if not status.get("active"):
        return _feature("balancer", "Balancer", WARN, f"Balancer đang tắt (mode {status.get('mode') or '?'}).",
                        "Cân nhắc bật (cần duyệt): ceph balancer on")
    if status.get("no_optimization_needed"):
        return _feature("balancer", "Balancer", OK,
                        f"Balancer đang chạy (mode {status.get('mode')}); phân bố đã tối ưu.")
    return _feature("balancer", "Balancer", INFO,
                    f"Balancer đang chạy (mode {status.get('mode')}): {status.get('optimize_result') or 'đang tối ưu'}.")


def build_report(payloads: dict[str, Any], metrics: dict[str, Any], errors: dict[str, str]) -> dict:
    modules = parse_mgr_modules(payloads.get("mgr_modules"))
    devices = parse_devices(payloads.get("devices"))
    states = [smart_state(value) for value in metrics.values()]
    with_metrics, smart_failed = states.count("ok"), states.count("failed")
    features = [
        _devicehealth(modules),
        _prediction(modules, devices, len(metrics), with_metrics, smart_failed),
        _autoscaler(modules, payloads.get("autoscale")),
        _balancer(modules, payloads.get("balancer")),
    ] if modules else []
    return {
        "schema": "ceph-ai.ceph-features.v1",
        "collected_at": utc_now().isoformat() + "Z",
        "modules": modules,
        "features": features,
        "devices": {
            "total": len(devices),
            "with_prediction": sum(1 for row in devices if row["life_expectancy_min"] or row["life_expectancy_max"]),
            "sampled_for_smart": len(metrics),
            "with_smart": with_metrics,
            "smartctl_failed": smart_failed,
            "rows": devices,
        },
        "errors": errors,
        "read_only": True,
    }


def collect(runner: Runner) -> dict:
    """Run the fixed read-only commands through ``runner`` and build the report.

    ``runner(command)`` returns the parsed JSON or raises; one failing
    command is reported in ``errors`` and does not hide the others.
    """
    payloads: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for command_id, command in FEATURE_COMMANDS.items():
        try:
            payloads[command_id] = runner(command)
        except Exception as exc:  # noqa: BLE001 - reported per command, never fatal
            errors[command_id] = str(exc)[:300]
    metrics: dict[str, Any] = {}
    for devid in _metrics_sample(parse_devices(payloads.get("devices"))):
        try:
            metrics[devid] = runner(HEALTH_METRICS_COMMAND.format(devid=devid))
        except Exception as exc:  # noqa: BLE001
            errors[f"health_metrics:{devid}"] = str(exc)[:300]
    return build_report(payloads, metrics, errors)
