"""Node hardware (CPU/RAM) threshold monitoring — proactively flags a node
whose CPU or RAM usage stays abnormally high for several consecutive scans,
so an operator finds out from an Incident (and, if configured, a Telegram
push — shared/telegram_alerts.py) instead of only discovering a starved
node once something ELSE running on top of it starts failing.

Own SLOW cadence (watcher/main.py::run() calls this on its own
settings.node_health_scan_interval_seconds timer, default 15 minutes — NOT
every watcher_poll_interval_seconds tick), same reasoning as
watcher/device_health_monitor.py's own scan cadence: collecting these
metrics needs a fresh SSH round trip PER NODE (watcher/node_metrics.py
samples /proc twice, ~1s apart, per host) — running that on every 15s
health-check tick would be a meaningfully heavier SSH load than the single
`ceph health detail` query the main loop already does.

Same "own in-memory rolling streak, no DB persistence of raw samples"
posture as watcher/volume_monitor.py — a consecutive-scans-over-threshold
streak is all this needs, not a full metrics history (that's what the
Nodes page's live on-demand query, watcher/node_metrics.py's other caller,
already covers — unrelated to this alerting path).

2026-08-05, NOT verified against a real cluster under genuine CPU/RAM
pressure this session — thresholds/streak-length below are a first-pass
judgment call (same posture as volume_monitor.py's own thresholds when
first written); tune here directly if they turn out wrong once verified
live.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from config.settings import settings
from shared import audit, db
from shared.cluster_nodes import configured_nodes
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from shared.incident_actions import cancel_pending_actions
from shared.telegram_alerts import send_node_alert
from watcher.node_metrics import NodeMetricsError, collect_node_metrics
from watcher import node_resource_forecast
from worker.policy import gate

logger = logging.getLogger(__name__)

NODE_RESOURCE_HIGH_PREFIX = "NODE_RESOURCE_HIGH:"
# CPU-only pressure remains an operator investigation. Sustained RAM >=90%
# proposes a hard reboot, but it is RISKY and remains approval-gated.
NODE_RESOURCE_HIGH_ACTION_ID = "investigate_manually"
NODE_RAM_HIGH_ACTION_ID = "hard_reboot_node"

# Absolute thresholds, not relative-to-own-history like volume_monitor.py's
# NEAR_PEAK_RATIO — CPU%/RAM% are already normalized 0-100 numbers, unlike
# IOPS/latency (workload-dependent, no fixed "normal" number), so they need
# no rolling baseline to compare against.
CPU_ALERT_THRESHOLD_PERCENT = 90.0
MEM_ALERT_THRESHOLD_PERCENT = 90.0
# Must look overloaded this many scans IN A ROW before an Incident fires —
# one noisy sample (a brief CPU spike from a legitimate scrub/backfill/
# backup export) must never trigger one, same reasoning as
# volume_monitor.py's own CONSECUTIVE_POLLS_REQUIRED. Smaller than that
# module's own 3 (this module's own scan interval is already ~60x slower
# than the main poll loop's, so 2 consecutive scans already means the
# condition held for roughly 2 x node_health_scan_interval_seconds).
CONSECUTIVE_SCANS_REQUIRED = 2

# Mirrors watcher/main.py::_RECOVERABLE_STATUSES / watcher/volume_monitor.py's
# own copy — kept as its own copy rather than a cross-import, same
# "independent modules" reasoning both of those already document.
_RECOVERABLE_STATUSES = {
    IncidentStatus.NEW.value,
    IncidentStatus.DIAGNOSING.value,
    IncidentStatus.PENDING_APPROVAL.value,
    IncidentStatus.APPROVED.value,
    IncidentStatus.EXECUTING.value,
    # 2026-08-20: lệnh đã chạy nhưng CHƯA xác minh là hết lỗi —
    # vẫn là một sự cố đang mở. Thiếu dòng này, mọi chỗ dùng tập
    # trạng thái này để chống trùng sẽ tưởng Incident đã đóng và
    # tạo thêm một Incident nữa cho cùng vấn đề.
    IncidentStatus.VERIFYING.value,
    IncidentStatus.FAILED.value,
}

# Module-level, process-lifetime state — one entry per host ever scanned
# since this Watcher process started. Never cleared/evicted, same judgment
# call as volume_monitor.py's own _state (a lab/small deployment's node
# count is small and stable enough not to be a real unbounded-growth risk).
_consecutive_high_scans: dict[str, int] = {}


def ceph_code_for(host: str) -> str:
    return f"{NODE_RESOURCE_HIGH_PREFIX}{host}"


def check_node_resources(
    still_over_threshold: set[str] | None = None,
    *, cluster_name: str | None = None,
) -> dict[str, dict]:
    """Scans every configured cluster node's current CPU%/RAM% and returns
    {ceph_code: detail} for every host whose CPU OR RAM has stayed at/above
    threshold for CONSECUTIVE_SCANS_REQUIRED scans in a row. A single
    host's SSH/parse failure only skips THAT host for this scan (logged,
    not raised, and its streak is left untouched rather than reset to 0 —
    a transient SSH hiccup must not erase real progress toward a genuine
    alert) — one unreachable node must never blank out every other node's
    own result, same per-host isolation posture as watcher/collector.py
    elsewhere in this codebase."""
    flagged: dict[str, dict] = {}
    for node in configured_nodes():
        host = node["host"]
        try:
            metrics = collect_node_metrics(host)
        except NodeMetricsError:
            logger.warning("check_node_resources: failed to collect metrics for %s", host, exc_info=True)
            continue

        cpu = metrics["cpu_percent"]
        mem = metrics["mem_percent"]
        # The forecast path must read its history from Loki.  Persist the
        # fresh sample before any prediction is attempted; a Loki failure
        # degrades forecasting only and never suppresses threshold alerts.
        node_resource_forecast.push_sample(cluster_name or settings.cluster_name, host, metrics)
        if settings.node_resource_forecast_enabled:
            try:
                forecasts = node_resource_forecast.forecast(cluster_name or settings.cluster_name, host)
                for prediction in node_resource_forecast.risky_forecasts(forecasts):
                    logger.warning(
                        "node forecast: %s %s may reach 90%% in %.1fh "
                        "(slope=%.3f%%/h confidence=%.2f samples=%d)",
                        host, prediction.metric.upper(), prediction.hours_to_90,
                        prediction.slope_percent_per_hour, prediction.confidence,
                        prediction.samples,
                    )
            except Exception:
                logger.warning("node forecast: Loki query failed for %s", host, exc_info=True)
        over_threshold = cpu >= CPU_ALERT_THRESHOLD_PERCENT or mem >= MEM_ALERT_THRESHOLD_PERCENT
        _consecutive_high_scans[host] = (_consecutive_high_scans.get(host, 0) + 1) if over_threshold else 0

        # 2026-08-20: độc lập với streak — xem
        # create_or_resolve_node_health_incidents để biết vì sao "đang quá
        # tải ngay bây giờ" phải tách khỏi "đã quá tải đủ lâu".
        if over_threshold and still_over_threshold is not None:
            still_over_threshold.add(ceph_code_for(host))

        if _consecutive_high_scans[host] >= CONSECUTIVE_SCANS_REQUIRED:
            flagged[ceph_code_for(host)] = {
                "host": host,
                "cpu_percent": cpu,
                "mem_percent": mem,
                "consecutive_scans": _consecutive_high_scans[host],
            }
    return flagged


def _rationale_for(detail: dict) -> str:
    remediation = (
        " RAM vượt ngưỡng nên hệ thống đề xuất hard reboot node; thao tác chỉ chạy sau khi operator duyệt."
        if detail["mem_percent"] >= MEM_ALERT_THRESHOLD_PERCENT
        else " Cần operator kiểm tra tiến trình gây tải trước khi xử lý."
    )
    return (
        f"Node {detail['host']} có CPU {detail['cpu_percent']:.1f}% / RAM {detail['mem_percent']:.1f}% "
        f"cao bất thường, lặp lại {detail['consecutive_scans']} lần quét liên tiếp (ngưỡng "
        f"{CPU_ALERT_THRESHOLD_PERCENT:.0f}% CPU hoặc {MEM_ALERT_THRESHOLD_PERCENT:.0f}% RAM) — có thể "
        f"node đang quá tải hoặc có tiến trình bất thường, không phải lỗi cấu hình có thể tự sửa."
        f"{remediation}"
    )


def create_or_resolve_node_health_incidents(
    current: dict[str, dict],
    still_over_threshold: set[str] | None = None,
) -> None:
    """Same shape/reasoning as watcher/volume_monitor.py::
    create_or_resolve_volume_incidents — creates a PENDING_APPROVAL
    Incident plus an approval-gated Action for every newly-flagged host not
    already open, and resolves any open NODE_RESOURCE_HIGH: Incident whose
    host dropped out of `current` (no longer over threshold). Sends a
    Telegram alert (shared/telegram_alerts.py, its own independent
    Phần cứng channel — telegram_node_bot_token/telegram_node_chat_id) for
    each NEWLY created Incident only — not resent on every scan a host
    stays flagged, same "one notification per genuinely new problem"
    posture as every other alert
    path in this codebase.

    `still_over_threshold` (2026-08-20): `_consecutive_high_scans` chỉ sống
    trong RAM, nên sau mỗi lần Watcher restart nó rỗng và node vẫn quá tải
    y như cũ lại vắng mặt khỏi `current` trong `CONSECUTIVE_SCANS_REQUIRED`
    lần quét đầu — vòng resolve bên dưới sẽ đọc đó là "tải đã về bình
    thường" và đóng nhầm Incident, để rồi tạo lại Incident + Action +
    Telegram mới ngay sau đó. Cùng một lỗi, cùng một cách vá như
    watcher/crush_skew_monitor.py (xem docstring hàm tương ứng ở đó, kèm số
    liệu đo được). Mặc định None giữ nguyên hành vi cũ."""
    with db.SessionLocal() as session:
        open_incidents = (
            session.query(Incident)
            .filter(Incident.ceph_code.like(f"{NODE_RESOURCE_HIGH_PREFIX}%"))
            .filter(Incident.status.in_(_RECOVERABLE_STATUSES))
            .all()
        )
        open_codes = {incident.ceph_code for incident in open_incidents}

        still_over = still_over_threshold or set()
        for incident in open_incidents:
            if incident.ceph_code not in current and incident.ceph_code not in still_over:
                incident.status = IncidentStatus.RESOLVED.value
                cancel_pending_actions(session, incident.id)

        for ceph_code, detail in current.items():
            if ceph_code in open_codes:
                continue  # already has an open Incident — don't duplicate

            rationale = _rationale_for(detail)
            incident = Incident(
                ceph_code=ceph_code,
                status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(),
                log_excerpt=rationale,
            )
            session.add(incident)
            session.flush()  # assigns incident.id, needed by the Action FK below

            ram_high = detail["mem_percent"] >= MEM_ALERT_THRESHOLD_PERCENT
            action_id = NODE_RAM_HIGH_ACTION_ID if ram_high else NODE_RESOURCE_HIGH_ACTION_ID
            target_nodes = [detail["host"]] if ram_high else []
            action = Action(
                incident_id=incident.id,
                action_id=action_id,
                classification=gate.classify_action(action_id).value,
                status=ActionStatus.PENDING_APPROVAL.value,
                rationale=rationale,
                # RAM pressure targets the affected host for the approved
                # reboot; CPU-only pressure keeps the manual-action posture.
                target_nodes=json.dumps(target_nodes),
                action_params=json.dumps({"host": detail["host"]}),
                proposed_command=(
                    "nohup systemctl reboot --force --force >/dev/null 2>&1 &"
                    if ram_high else None
                ),
            )
            session.add(action)
            session.flush()

            audit.record(
                session,
                incident_id=incident.id,
                action_id=action.id,
                event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
                actor=audit.ACTOR_SYSTEM,
            )

            send_node_alert(detail["host"], rationale)
        session.commit()
