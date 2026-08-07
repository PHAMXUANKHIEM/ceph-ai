"""DeviceHealth-driven OSD evacuation proposals (Story C, 2026-08-01) —
proactively proposes `mark_osd_out`-equivalent evacuation for an OSD whose
backing device Ceph's own `devicehealth` mgr module already predicts will
fail soon, so the operator finds out (and can approve moving data off it)
BEFORE the drive actually dies, not after.

Same architectural posture as watcher/volume_monitor.py, and for the exact
same reason: this bypasses the Incident-diagnosis AI pipeline entirely
(worker/llm/router_client.py's tool schema has no params field — see
worker/executor/commands.py's own comment on why pg_repair_force was
deliberately never wired up) — the osd_id here is extracted
DETERMINISTICALLY from `ceph device ls`'s own `daemons` field, never
guessed by an LLM, so there's nothing unsafe about creating the Action
directly with `action_params={"osd_id": N}` already resolved.

Deliberately RISKY, always (worker/policy/action_policy.yaml) — evacuating
an OSD moves real data and is never auto-executed just because a scan ran;
an operator must review and approve each proposal on the Dashboard, same
as restore_rbd_image_to_production/rbd_trash_remove.

2026-08-01, NOT verified against a real cluster with an actual predicted-
failing device this session (same posture watcher/volume_monitor.py's own
docstring already documents for RBD iostat) — `ceph device ls`/`ceph osd
dump`'s JSON field names below are taken from Ceph's own source
(src/pybind/mgr/devicehealth/module.py) and long-stable public API
(`osd dump`'s `osds[].osd/in` shape), not a live query, so re-verify with
`pytest -m live` once a lab cluster with a real device-health prediction
exists.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from config.settings import settings
from shared import audit, db
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from shared.telegram_alerts import send_node_alert
from watcher import ceph_client
from watcher.ceph_client import CephQueryError
from worker.policy import gate

DEVICE_HEALTH_EVACUATE_PREFIX = "DEVICE_HEALTH_EVACUATE:"
DEVICE_HEALTH_EVACUATE_ACTION_ID = "evacuate_predicted_failing_osd"

# Mirrors watcher/main.py::_RECOVERABLE_STATUSES / watcher/volume_monitor.py's
# own copy — kept as its own copy rather than a cross-import for the same
# "independent modules" reasoning both of those already document.
_RECOVERABLE_STATUSES = {
    IncidentStatus.NEW.value,
    IncidentStatus.DIAGNOSING.value,
    IncidentStatus.PENDING_APPROVAL.value,
    IncidentStatus.APPROVED.value,
    IncidentStatus.EXECUTING.value,
    IncidentStatus.FAILED.value,
}

_OSD_DAEMON_RE = re.compile(r"^osd\.(\d+)$")
# Ceph's own devicehealth module (src/pybind/mgr/devicehealth/module.py)
# parses this with strptime format '%Y-%m-%dT%H:%M:%S.%f%z', but that
# same module's own docstring example value uses a space separator instead
# ('2019-01-20 21:12:12.000000+00:00') — inconsistent in the one source
# available to verify against here, so both are tried rather than picking
# one and silently failing to parse a real value. Re-verify against a real
# cluster's actual `ceph device ls` output once available (see this
# module's own docstring).
_LIFE_EXPECTANCY_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S.%f%z")
_NO_LIFE_EXPECTANCY_VALUES = (None, "", "0.000000")


def ceph_code_for(osd_id: int) -> str:
    return f"{DEVICE_HEALTH_EVACUATE_PREFIX}{osd_id}"


def _osd_ids_currently_in(mon_host: str) -> set[int]:
    """`ceph osd dump`'s osds[].in is 1/0 (in-cluster vs already marked
    out) — only osd_ids still `in` are real evacuation candidates; one
    already `out` (whether from a prior approved proposal or a manual
    operator action) has nothing left to propose."""
    _, payload = ceph_client.run_ceph_json_command("ceph osd dump")
    osds = payload.get("osds") if isinstance(payload, dict) else None
    if not isinstance(osds, list):
        return set()
    return {int(o["osd"]) for o in osds if isinstance(o, dict) and o.get("in") == 1 and "osd" in o}


def _parse_life_expectancy_min(value) -> datetime | None:
    if not isinstance(value, str) or value in _NO_LIFE_EXPECTANCY_VALUES:
        return None
    for fmt in _LIFE_EXPECTANCY_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def check_predicted_failing_osds() -> dict[str, dict]:
    """Returns {ceph_code: detail} for every OSD that is BOTH (a) currently
    `in` the cluster and (b) backed by a device whose life_expectancy_min
    falls within settings.device_health_evacuate_threshold_days from now.

    life_expectancy_min (not _max) is the deliberate choice — Ceph's own
    prediction is a [min, max] range, and min is the EARLIEST the device
    could fail; waiting for max would mean waiting right up to (or past)
    the worst case before proposing anything, which defeats the "evacuate
    BEFORE it dies" point of this feature.

    Never raises — a transient MON query failure just means this poll
    finds nothing, same best-effort posture as
    watcher/volume_monitor.py::check_volumes."""
    try:
        host, device_payload = ceph_client.run_ceph_json_command("ceph device ls")
        in_osd_ids = _osd_ids_currently_in(host)
    except CephQueryError:
        return {}

    # crush_host (the OSD's actual physical node), NOT mon_host below (just
    # whichever MON `ceph device ls` happened to be run against) — same
    # host_by_osd_id lookup watcher/osd_latency_monitor.py::
    # check_osd_latency_outliers already uses for its own per-OSD Telegram
    # alert. Best-effort: an OSD missing from this map (e.g. `ceph osd
    # tree` query failed) just alerts with host=None.
    host_by_osd_id = {o["osd_id"]: o.get("crush_host") for o in ceph_client.list_osds()}

    devices = device_payload if isinstance(device_payload, list) else []
    horizon = datetime.utcnow() + timedelta(days=settings.device_health_evacuate_threshold_days)

    candidates: dict[str, dict] = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        life_min = _parse_life_expectancy_min(device.get("life_expectancy_min"))
        if life_min is None:
            continue
        # Ceph's own timestamps here are always UTC (+00:00) in practice —
        # tzinfo is dropped after parsing so this compares naive-to-naive
        # against `horizon` (datetime.utcnow(), also naive), rather than
        # risking a raised TypeError from comparing aware-to-naive.
        if life_min.replace(tzinfo=None) > horizon:
            continue
        devid = device.get("devid", "?")
        for daemon in device.get("daemons") or []:
            match = _OSD_DAEMON_RE.match(daemon)
            if not match:
                continue
            osd_id = int(match.group(1))
            if osd_id not in in_osd_ids:
                continue  # already evacuated (out) — nothing left to propose
            candidates[ceph_code_for(osd_id)] = {
                "osd_id": osd_id,
                "devid": devid,
                "life_expectancy_min": device.get("life_expectancy_min"),
                "life_expectancy_max": device.get("life_expectancy_max"),
                "mon_host": host,
                "host": host_by_osd_id.get(osd_id),
            }
    return candidates


def _rationale_for(detail: dict) -> str:
    return (
        f"Thiết bị {detail['devid']} (backing osd.{detail['osd_id']}) được Ceph devicehealth "
        f"dự đoán có khả năng hỏng trong khoảng {detail['life_expectancy_min']} — "
        f"{detail['life_expectancy_max']} (trong ngưỡng {settings.device_health_evacuate_threshold_days} "
        f"ngày đã cấu hình). Đề xuất evacuate osd.{detail['osd_id']} (ceph osd out) chủ động trước khi "
        f"ổ đĩa hỏng thật, để dữ liệu có thời gian di dời an toàn thay vì phục hồi khẩn cấp sau khi mất "
        f"redundancy đột ngột."
    )


def create_or_resolve_device_health_incidents(current: dict[str, dict]) -> None:
    """Same shape/reasoning as
    watcher/volume_monitor.py::create_or_resolve_volume_incidents — creates
    a PENDING_APPROVAL Incident+Action for every newly-candidate OSD not
    already open, and resolves any open DEVICE_HEALTH_EVACUATE: Incident
    whose osd_id dropped out of `current` (prediction cleared, or the OSD
    is no longer `in` — including because a prior proposal here was
    already approved and executed).

    2026-08-07: also pushes a Telegram alert (shared/telegram_alerts.py::
    send_node_alert, same "Phần cứng" channel as node_health_monitor.py/
    osd_latency_monitor.py) for each NEWLY created Incident only — this
    module previously created the Incident/Action silently with no
    notification at all, unlike its two sibling hardware monitors."""
    with db.SessionLocal() as session:
        open_incidents = (
            session.query(Incident)
            .filter(Incident.ceph_code.like(f"{DEVICE_HEALTH_EVACUATE_PREFIX}%"))
            .filter(Incident.status.in_(_RECOVERABLE_STATUSES))
            .all()
        )
        open_codes = {incident.ceph_code for incident in open_incidents}

        for incident in open_incidents:
            if incident.ceph_code not in current:
                incident.status = IncidentStatus.RESOLVED.value

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
                action_id=DEVICE_HEALTH_EVACUATE_ACTION_ID,
                classification=gate.classify_action(DEVICE_HEALTH_EVACUATE_ACTION_ID).value,
                status=ActionStatus.PENDING_APPROVAL.value,
                rationale=rationale,
                # The MON node run_ceph_json_command actually reached for
                # this scan — already verified reachable, not a guess (same
                # posture as watcher/collector.py's Story A/B additions).
                target_nodes=json.dumps([detail["mon_host"]]),
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

            send_node_alert(detail["host"] or detail["mon_host"], rationale)
        session.commit()
