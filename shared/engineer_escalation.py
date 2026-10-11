"""Hand an incident to an engineer when it is outside what Ceph AI has learned (croit-parity C6).

croit backs its software with engineers on call; Ceph AI must at least know
when to call one. An open incident on a production cluster that has been
open for ESCALATE_AFTER and is any of:

- a failed remediation (status FAILED);
- not diagnosed by the AI yet;
- diagnosed with confidence below LOW_CONFIDENCE, or as "not enough evidence";
- a health code that belongs to no fault family the AI has learned;

gets one "cần kỹ sư" card in the incident channel with the reason and the
AI's diagnosis, and a timeline event so it is never sent twice. Operator
requests (pool creation, deploys, OSD replacement…) are never escalated.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from shared import incident_events
from shared.evidence_gaps import family_of_code
from shared.models import Cluster, Incident, IncidentTimelineEvent, RemediationCase
from shared.time import utc_now

logger = logging.getLogger(__name__)

EVENT = "engineer_escalation"
ESCALATE_AFTER = timedelta(minutes=30)
LOW_CONFIDENCE = 0.5
CLOSED = {"RESOLVED", "AUTO_FIXED", "REJECTED"}
_INSUFFICIENT = ("chưa đủ bằng chứng", "không đủ bằng chứng", "insufficient evidence", "not enough evidence")
# Incidents Ceph AI opens for operator requests, not faults (the *_CEPH_CODE constants of the Dashboard routes).
OPERATOR_CODES = frozenset({
    "BACKUP_MANUAL", "BACKUP_SCHEDULED", "CHAT_REQUEST", "CINDER_SNAPSHOT_CREATE", "CINDER_SNAPSHOT_DELETE",
    "CINDER_SNAPSHOT_SCHEDULED", "CINDER_VOLUME_ATTACH", "CINDER_VOLUME_DETACH", "CLUSTER_DELETE", "CLUSTER_DEPLOY",
    "CLUSTER_NODE_ADD", "CLUSTER_NODE_REMOVE", "CLUSTER_UPGRADE", "CONVERT_CLUSTER_TO_CEPHADM", "NODE_MAINTENANCE",
    "NODE_OS_GATE", "OSD_REPLACE", "POOL_ACTION_REQUEST", "POOL_CREATE_REQUEST", "RBD_TRASH_PURGE_ALL",
    "RBD_TRASH_REMOVE", "RBD_VOLUME_CLONE", "RBD_VOLUME_COPY", "RBD_VOLUME_CREATE", "RBD_VOLUME_FLATTEN",
    "RBD_VOLUME_MOVE", "RBD_VOLUME_MOVE_CLEANUP", "RBD_VOLUME_QOS", "RBD_VOLUME_RENAME", "RBD_VOLUME_RESIZE",
    "RBD_VOLUME_TEMPLATE", "RBD_VOLUME_TRASH_MOVE", "RBD_VOLUME_TRASH_RESTORE", "RESTORE_CLUSTER_FROM_BACKUP",
    "RESTORE_RBD_IMAGE_AS_NEW", "RESTORE_RBD_IMAGE_TO_PRODUCTION", "VM_PERF_BENCHMARK", "VOLUME_PERF_SWEEP",
})


def is_operator_request(code: str) -> bool:
    return code in OPERATOR_CODES or ":" in code


def reason_for(incident: Incident, case: RemediationCase | None,
               family_codes: dict[str, tuple[str, ...]]) -> str | None:
    """Why this incident needs an engineer, or None when the AI is on known ground."""
    if incident.status == "FAILED":
        return "khắc phục tự động/được duyệt đã thất bại"
    if family_of_code(incident.ceph_code or "", family_codes) is None:
        return f"{incident.ceph_code} không thuộc họ lỗi nào AI đã học"
    diagnosis = (incident.diagnosis_text or (case.diagnosis if case else "") or "").strip()
    if not diagnosis:
        return "AI chưa chẩn đoán được"
    if case is not None and case.diagnosis_confidence is not None and case.diagnosis_confidence < LOW_CONFIDENCE:
        return f"độ tin chẩn đoán chỉ {case.diagnosis_confidence:.0%}"
    if any(marker in diagnosis.casefold() for marker in _INSUFFICIENT):
        return "AI kết luận chưa đủ bằng chứng"
    return None


def escalation_text(incident: Incident, cluster_name: str, reason: str, minutes: int) -> str:
    diagnosis = (incident.diagnosis_text or "(chưa có chẩn đoán)").strip()
    return (f"🧑‍🔧 CẦN KỸ SƯ — {incident.ceph_code} trên {cluster_name}, mở {minutes} phút.\n"
            f"Lý do: {reason}.\n"
            f"Chẩn đoán của AI: {diagnosis[:900]}\n"
            f"Sự cố {incident.id} (Dashboard › Incidents). Ceph AI không tự xử lý tiếp ngoài các đề xuất chờ duyệt.")


def escalate_due(session, *, family_codes: dict[str, tuple[str, ...]], now: datetime | None = None,
                 enqueue=None) -> list[str]:
    """Queue one card per incident that needs an engineer; returns the incident ids escalated."""
    from shared import telegram_outbox

    enqueue = enqueue or telegram_outbox.enqueue_engineer_escalation
    now = now or utc_now()
    clusters = {cluster.id: cluster for cluster in session.query(Cluster).all()}
    default = next((cluster for cluster in clusters.values() if cluster.is_default), None)
    done = {row[0] for row in session.query(IncidentTimelineEvent.incident_id)
            .filter(IncidentTimelineEvent.event_type == EVENT)}
    escalated = []
    rows = (session.query(Incident).filter(Incident.status.notin_(CLOSED), Incident.detected_at <= now - ESCALATE_AFTER)
            .order_by(Incident.detected_at).limit(500).all())
    for incident in rows:
        cluster = clusters.get(incident.cluster_id) if incident.cluster_id else default
        if (incident.id in done or is_operator_request(incident.ceph_code or "") or cluster is None
                or not cluster.is_active or cluster.autonomy_environment != "production"):
            continue
        if incident.muted_until is not None and incident.muted_until > now:
            continue
        case = (session.query(RemediationCase).filter(RemediationCase.incident_id == incident.id)
                .order_by(RemediationCase.created_at.desc()).first())
        reason = reason_for(incident, case, family_codes)
        if reason is None:
            continue
        minutes = int((now - incident.detected_at).total_seconds() // 60)
        enqueue(session, incident_id=incident.id, message=escalation_text(incident, cluster.name, reason, minutes))
        incident_events.record(session, incident_id=incident.id, event_type=EVENT, actor="watcher",
                               evidence={"reason": reason, "open_minutes": minutes})
        escalated.append(incident.id)
    return escalated
