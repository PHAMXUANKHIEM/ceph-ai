"""OSD latency outlier detection (2026-08-07) — flags an OSD whose disk
commit latency is abnormally high compared to its PEERS in the same
cluster, right now, so an operator finds out about a "quietly slow" disk
BEFORE it shows up as degraded p99/p999 tail latency cluster-wide. Neither
`ceph health detail` (MON/OSD down, PG degraded...) nor
watcher/node_health_monitor.py (node-level CPU/RAM) catches this: an OSD
can be fully `up`/`in`, contributing to no HEALTH_WARN/HEALTH_ERR check at
all, and still be the one slow disk every write to its PGs has to wait on.

Deliberately STATELESS/peer-relative, not self-relative-to-own-history like
watcher/volume_monitor.py's rolling window, and no new metrics-history DB
table — every scan computes the cluster's OWN current median
`commit_latency_ms` (via `ceph osd perf`, a single cheap JSON-RPC through a
MON, no SSH) and compares each up OSD against THAT. This is simpler and
avoids the "which baseline is even correct for this specific disk's
hardware" problem an absolute ms threshold would have on a cluster mixing
HDD/SSD/NVMe — a peer-median comparison is hardware-mix-agnostic by
construction. `apply_latency_ms` (Filestore-era, typically 0 on BlueStore)
is reported in the rationale if nonzero but never used for the threshold
math.

Same "own in-memory rolling streak, no DB persistence of raw samples"
posture as watcher/node_health_monitor.py/watcher/volume_monitor.py — a
consecutive-scans-over-threshold streak per osd_id is all this needs.

2026-08-07: JSON shape VERIFIED against a real cluster (`ceph osd perf
--format json` -> `{"pg_ready": bool, "osdstats": {"osd_perf_infos":
[{"id": int, "perf_stats": {"commit_latency_ms": float,
"apply_latency_ms": float, ..._ns variants}}]}}` — `osd_perf_infos` is
nested one level under `osdstats`, not top-level). NOT yet verified under
a genuine slow-disk condition (the lab cluster used for verification only
has 3 OSDs, below MIN_UP_OSDS_FOR_COMPARISON, and all were idle/0ms) — the
outlier math itself is still unexercised against real non-zero, divergent
latencies in production.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime

from shared import audit, db
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from shared.incident_actions import cancel_pending_actions
from shared.telegram_alerts import send_osd_latency_alert
from watcher import ceph_client
from watcher.ceph_client import CephQueryError
from worker.policy import gate

OSD_LATENCY_HIGH_PREFIX = "OSD_LATENCY_HIGH:"
# No automated remediation exists for "this OSD's disk is slow" (could be
# a failing drive, a noisy neighbor on shared storage, a backfill/scrub in
# progress...) — same "no Command, operator investigates" posture as
# watcher/node_health_monitor.py's own NODE_RESOURCE_HIGH_ACTION_ID.
OSD_LATENCY_ACTION_ID = "investigate_manually"

# Internal tuning constants, not config/settings.py fields — same
# "operational tuning, not per-deployment config" convention as
# watcher/volume_monitor.py's ROLLING_WINDOW_SIZE/NEAR_PEAK_RATIO/etc.
OUTLIER_LATENCY_RATIO = 3.0
# Absolute floor in ms — without this, a cluster where EVERY OSD is
# extremely fast (e.g. all-NVMe, median ~0.1ms) would flag an OSD at
# 0.5ms as "5x the median", which is real but operationally meaningless.
MIN_ABSOLUTE_LATENCY_MS = 5.0
# Below this many `up` OSDs, a median is too noisy/unstable to compare
# against (e.g. a 2-OSD lab cluster: one OSD IS "50% of the median" by
# construction whenever they differ at all) — skip detection entirely.
MIN_UP_OSDS_FOR_COMPARISON = 4
# Must look like an outlier this many scans IN A ROW before an Incident
# fires — latency is far noisier/burstier than CPU/RAM (a single scrub-
# triggered spike must never trigger an alert on its own), same reasoning
# as node_health_monitor.py's own CONSECUTIVE_SCANS_REQUIRED.
CONSECUTIVE_SCANS_REQUIRED = 2

# Mirrors watcher/main.py::_RECOVERABLE_STATUSES / the identical copies in
# watcher/device_health_monitor.py, watcher/node_health_monitor.py,
# watcher/volume_monitor.py — kept as its own copy rather than a
# cross-import, same "independent modules" reasoning all three already
# document.
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

# Module-level, process-lifetime state — one entry per osd_id ever scanned
# since this Watcher process started. Never cleared/evicted, same judgment
# call as node_health_monitor.py's/volume_monitor.py's own _state (a
# lab/small deployment's OSD count is small and stable enough not to be a
# real unbounded-growth risk).
_consecutive_high_scans: dict[int, int] = {}


def ceph_code_for(osd_id: int) -> str:
    return f"{OSD_LATENCY_HIGH_PREFIX}{osd_id}"


def check_osd_latency_outliers(
    still_over_threshold: set[str] | None = None,
) -> dict[str, dict]:
    """Scans every currently-`up` OSD's commit latency via `ceph osd perf`
    and returns {ceph_code: detail} for every OSD whose latency has stayed
    at/above OUTLIER_LATENCY_RATIO times the cluster's own current median
    (AND at/above MIN_ABSOLUTE_LATENCY_MS) for CONSECUTIVE_SCANS_REQUIRED
    scans in a row. No-op (returns {}) if fewer than
    MIN_UP_OSDS_FOR_COMPARISON OSDs are up, or if the query itself fails —
    never raises, same best-effort posture as
    watcher/device_health_monitor.py::check_predicted_failing_osds."""
    try:
        _, payload = ceph_client.run_ceph_json_command("ceph osd perf")
    except CephQueryError:
        return {}

    # Verified 2026-08-07 against a real cluster: `osd_perf_infos` is
    # nested under `osdstats`, NOT at the top level of `ceph osd perf`'s
    # own JSON output (`{"pg_ready": bool, "osdstats": {"osd_perf_infos":
    # [...]}}`) — this module's docstring originally assumed the flatter
    # shape before that verification.
    osdstats = payload.get("osdstats") if isinstance(payload, dict) else None
    perf_infos = osdstats.get("osd_perf_infos") if isinstance(osdstats, dict) else None
    if not isinstance(perf_infos, list):
        return {}

    osds = ceph_client.list_osds()
    up_osd_ids = {o["osd_id"] for o in osds if o.get("status") == "up"}
    host_by_osd_id = {o["osd_id"]: o.get("crush_host") for o in osds}

    commit_latency_by_id: dict[int, float] = {}
    apply_latency_by_id: dict[int, float] = {}
    for info in perf_infos:
        if not isinstance(info, dict):
            continue
        osd_id = info.get("id")
        if osd_id not in up_osd_ids:
            continue  # down/out OSDs already covered by `ceph health detail`
        stats = info.get("perf_stats") or {}
        commit_ms = stats.get("commit_latency_ms")
        if not isinstance(commit_ms, (int, float)):
            continue
        commit_latency_by_id[osd_id] = float(commit_ms)
        apply_ms = stats.get("apply_latency_ms")
        if isinstance(apply_ms, (int, float)):
            apply_latency_by_id[osd_id] = float(apply_ms)

    if len(commit_latency_by_id) < MIN_UP_OSDS_FOR_COMPARISON:
        return {}

    median_ms = statistics.median(commit_latency_by_id.values())
    outlier_floor_ms = max(median_ms, 0.1) * OUTLIER_LATENCY_RATIO

    currently_high: set[int] = set()
    for osd_id, commit_ms in commit_latency_by_id.items():
        if commit_ms >= outlier_floor_ms and commit_ms >= MIN_ABSOLUTE_LATENCY_MS:
            currently_high.add(osd_id)

    # Reset the streak for every up-and-measured OSD not currently high —
    # a normal-looking scan in between must reset progress, not just leave
    # it untouched, same "no partial credit across separate spikes"
    # reasoning as node_health_monitor.py's own streak handling.
    flagged: dict[str, dict] = {}
    for osd_id in commit_latency_by_id:
        if osd_id in currently_high:
            _consecutive_high_scans[osd_id] = _consecutive_high_scans.get(osd_id, 0) + 1
            # 2026-08-20: độc lập với streak — xem
            # create_or_resolve_osd_latency_incidents để biết vì sao
            # "đang cao ngay bây giờ" phải tách khỏi "đã cao đủ lâu".
            if still_over_threshold is not None:
                still_over_threshold.add(ceph_code_for(osd_id))
        else:
            _consecutive_high_scans[osd_id] = 0

        if _consecutive_high_scans[osd_id] >= CONSECUTIVE_SCANS_REQUIRED:
            flagged[ceph_code_for(osd_id)] = {
                "osd_id": osd_id,
                "host": host_by_osd_id.get(osd_id),
                "commit_latency_ms": commit_latency_by_id[osd_id],
                "apply_latency_ms": apply_latency_by_id.get(osd_id, 0.0),
                "cluster_median_ms": median_ms,
                "consecutive_scans": _consecutive_high_scans[osd_id],
            }
    return flagged


def _rationale_for(detail: dict) -> str:
    ratio = detail["commit_latency_ms"] / max(detail["cluster_median_ms"], 0.1)
    host_suffix = f" ({detail['host']})" if detail["host"] else ""
    text = (
        f"osd.{detail['osd_id']}{host_suffix} có commit_latency "
        f"{detail['commit_latency_ms']:.1f}ms, cao gấp {ratio:.1f}x so với trung vị cụm "
        f"({detail['cluster_median_ms']:.1f}ms), lặp lại {detail['consecutive_scans']} lần quét "
        f"liên tiếp — có thể ổ đĩa/OSD này đang chậm bất thường, ảnh hưởng tail latency toàn cụm, "
        f"không phải lỗi cấu hình có thể tự sửa."
    )
    if detail["apply_latency_ms"]:
        text += f" (apply_latency {detail['apply_latency_ms']:.1f}ms)"
    return text


def create_or_resolve_osd_latency_incidents(
    current: dict[str, dict],
    still_over_threshold: set[str] | None = None,
) -> None:
    """Same shape/reasoning as watcher/device_health_monitor.py::
    create_or_resolve_device_health_incidents — creates a PENDING_APPROVAL
    Incident+Action(investigate_manually) for every newly-flagged osd_id
    not already open, and resolves any open OSD_LATENCY_HIGH: Incident
    whose osd_id dropped out of `current` (latency back to normal, or the
    OSD is no longer up — either way it's out of scope for this check).
    Sends a Telegram alert (shared/telegram_alerts.py::send_osd_latency_alert,
    the Phần cứng channel) for each NEWLY created Incident only — not
    resent every scan an osd_id stays flagged, same "one notification per
    genuinely new problem" posture as every other alert path here.

    `still_over_threshold` (2026-08-20): `_consecutive_high_scans` chỉ sống
    trong RAM, nên sau mỗi lần Watcher restart nó rỗng và OSD vẫn chậm y
    như cũ lại vắng mặt khỏi `current` trong `CONSECUTIVE_SCANS_REQUIRED`
    lần quét đầu — vòng resolve bên dưới sẽ đọc đó là "độ trễ đã về bình
    thường" và đóng nhầm Incident, để rồi tạo lại Incident + Action +
    Telegram mới ngay sau đó. Đây là đúng lỗi đã đo được trên
    watcher/crush_skew_monitor.py (xem docstring hàm tương ứng ở đó); cùng
    một khuôn streak-trong-RAM nên cùng một cách vá. Mặc định None giữ
    nguyên hành vi cũ."""
    with db.SessionLocal() as session:
        open_incidents = (
            session.query(Incident)
            .filter(Incident.ceph_code.like(f"{OSD_LATENCY_HIGH_PREFIX}%"))
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

            action = Action(
                incident_id=incident.id,
                action_id=OSD_LATENCY_ACTION_ID,
                classification=gate.classify_action(OSD_LATENCY_ACTION_ID).value,
                status=ActionStatus.PENDING_APPROVAL.value,
                rationale=rationale,
                # No specific automated target — investigate_manually has
                # no automated Command regardless (has_command() is False
                # for it, same as watcher/node_health_monitor.py's
                # identical comment), target_nodes/action_params are
                # informational only here.
                target_nodes=json.dumps([]),
                action_params=json.dumps({"osd_id": detail["osd_id"], "host": detail["host"]}),
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

            send_osd_latency_alert(detail["osd_id"], detail["host"], rationale)
        session.commit()
