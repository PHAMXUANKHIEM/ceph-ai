"""BlueStore per-pool omap quick-fix, turned into a normal system-proposed
suggestion (2026-08-06) — supersedes the original 2026-08-04 design (see
worker/executor/commands.py::_bluestore_omap_quick_fix_command's own
docstring for the fix itself, unchanged), which required an operator to
manually pick an osd_id from a dedicated Dashboard picker
(dashboard/routes/nodes.py) every time. This module detects
BLUESTORE_NO_PER_POOL_OMAP directly from the SAME `ceph health detail`
result watcher/main.py's main poll loop already fetches every tick, and
proposes bluestore_omap_quick_fix automatically — same shape/architecture
as watcher/device_health_monitor.py's DeviceHealth-driven evacuation
proposals (own synthetic ceph_code prefix, one Incident+Action per real
target, PENDING_APPROVAL, resolved when the underlying problem clears).

Bypasses the Incident-diagnosis AI pipeline entirely, same reasoning as
device_health_monitor.py's own docstring: the osd_id is extracted
DETERMINISTICALLY by regex from Ceph's own health-check detail text, never
guessed by an LLM — action_params={"osd_id": N} is already fully resolved
before the Action row is ever created.

**The one genuinely new problem this module has to solve that
device_health_monitor.py didn't**: `_bluestore_omap_quick_fix_command`
needs to SSH into the EXACT host physically running that osd_id (to stop
its systemd unit and touch its local data path) — unlike
evacuate_predicted_failing_osd's `ceph osd out`, runnable from any MON.
watcher/ceph_client.py::list_osds()'s own docstring already documents that
this app has no reliable `ceph osd tree` crush_host -> configured-node-IP
mapping. `resolve_osd_hosts()` below closes that gap by probing each of
THIS APP's own configured OSD-role hosts for its own locally-running
`ceph-osd@<id>`/`osd.<id>` systemd units (same "verify against the actual
host rather than guess" principle worker/executor/commands.py::
_discover_ceph_units already applies one layer down, at daemon-restart
time) — deterministic, not a guess, and needs no new config surface.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from shared import audit, db
from shared.incident_actions import cancel_pending_actions
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from watcher import ceph_client
# 2026-08-20: bản tra osd_id -> host duy nhất của codebase, tách ra
# watcher/osd_hosts.py để watcher/collector.py dùng chung — trước đó nó
# chỉ nằm ở đây nên collector phải đoán host (xem docstring module ấy).
from watcher.osd_hosts import resolve_osd_hosts
from worker.policy import gate

BLUESTORE_OMAP_PREFIX = "BLUESTORE_NO_PER_POOL_OMAP:"
BLUESTORE_OMAP_ACTION_ID = "bluestore_omap_quick_fix"
_REAL_CEPH_CODE = "BLUESTORE_NO_PER_POOL_OMAP"

# Mirrors watcher/main.py::_RECOVERABLE_STATUSES / device_health_monitor.py's
# own copy — kept as its own copy rather than a cross-import, same
# "independent modules" reasoning both of those already document.
_RECOVERABLE_STATUSES = {
    IncidentStatus.NEW.value,
    IncidentStatus.DIAGNOSING.value,
    IncidentStatus.PENDING_APPROVAL.value,
    IncidentStatus.APPROVED.value,
    IncidentStatus.EXECUTING.value,
    IncidentStatus.FAILED.value,
}

# Ceph's own BLUESTORE_NO_PER_POOL_OMAP detail text names each affected OSD
# directly, e.g. "osd.5 legacy (not per-pool) BlueStore omap detected,
# suggest to run store repair to correct" -- one such message per detail
# list entry in practice, but this regex just pulls every "osd.N" it finds
# across however many detail lines there are, so it's correct either way.
_OSD_DETAIL_ID_RE = re.compile(r"osd\.(\d+)")


def ceph_code_for(osd_id: int) -> str:
    return f"{BLUESTORE_OMAP_PREFIX}{osd_id}"


def check_legacy_omap_osds(health: dict) -> dict[str, dict]:
    """Returns {ceph_code: detail} for every osd_id BLUESTORE_NO_PER_POOL_OMAP
    currently names, extracted from `health` (the SAME dict watcher/main.py's
    main poll loop already has from `query_cluster_health()` this tick --
    no extra `ceph health detail` round trip needed). Empty dict if the
    check isn't currently reported at all (cluster doesn't have the
    problem, or already fixed)."""
    checks = health.get("checks") if isinstance(health, dict) else None
    check = (checks or {}).get(_REAL_CEPH_CODE)
    if not isinstance(check, dict):
        return {}
    detail_entries = check.get("detail") or []
    messages = [d.get("message", "") for d in detail_entries if isinstance(d, dict)]
    osd_ids = {int(m) for msg in messages for m in _OSD_DETAIL_ID_RE.findall(msg)}
    return {
        ceph_code_for(osd_id): {"osd_id": osd_id, "raw_messages": messages} for osd_id in osd_ids
    }


def _rationale_for(detail: dict, host: str) -> str:
    osd_id = detail["osd_id"]
    return (
        f"Ceph báo osd.{osd_id} đang ở chế độ BlueStore omap kiểu cũ (không theo từng pool) — "
        f"cảnh báo BLUESTORE_NO_PER_POOL_OMAP, chỉ ảnh hưởng accounting, không ảnh hưởng IOPS/S3 "
        f"trực tiếp nhưng nên sửa. Đề xuất dừng osd.{osd_id} trên {host}, chạy "
        f"`ceph-bluestore-tool quick-fix`, rồi khởi động lại. Có rủi ro đã biết với phiên bản "
        f"ceph-bluestore-tool cũ (multi-threaded quick-fix/repair race, ceph#41749/#41613) — chỉ "
        f"duyệt khi cluster đang khoẻ mạnh."
    )


def create_or_resolve_bluestore_incidents(current: dict[str, dict]) -> None:
    """Same shape/reasoning as
    watcher/device_health_monitor.py::create_or_resolve_device_health_incidents
    -- creates a PENDING_APPROVAL Incident+Action for every newly-candidate
    osd_id not already open, and resolves any open BLUESTORE_NO_PER_POOL_OMAP:
    Incident whose osd_id dropped out of `current` (fixed, or the OSD no
    longer exists)."""
    with db.SessionLocal() as session:
        open_incidents = (
            session.query(Incident)
            .filter(Incident.ceph_code.like(f"{BLUESTORE_OMAP_PREFIX}%"))
            .filter(Incident.status.in_(_RECOVERABLE_STATUSES))
            .all()
        )
        open_codes = {incident.ceph_code for incident in open_incidents}

        for incident in open_incidents:
            if incident.ceph_code not in current:
                incident.status = IncidentStatus.RESOLVED.value
                cancel_pending_actions(session, incident.id)

        new_codes = {code: detail for code, detail in current.items() if code not in open_codes}
        if new_codes:
            hosts_by_osd_id = resolve_osd_hosts({d["osd_id"] for d in new_codes.values()})
        else:
            hosts_by_osd_id = {}

        for ceph_code, detail in new_codes.items():
            host = hosts_by_osd_id.get(detail["osd_id"])
            if host is None:
                # Can't resolve which configured node runs this osd_id --
                # same "don't guess a target" posture as every other
                # per-host action in this codebase (see this module's own
                # docstring). Will be retried on the next scan.
                continue

            rationale = _rationale_for(detail, host)
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
                action_id=BLUESTORE_OMAP_ACTION_ID,
                classification=gate.classify_action(BLUESTORE_OMAP_ACTION_ID).value,
                status=ActionStatus.PENDING_APPROVAL.value,
                rationale=rationale,
                target_nodes=json.dumps([host]),
                action_params=json.dumps({"osd_id": detail["osd_id"]}),
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
        session.commit()
