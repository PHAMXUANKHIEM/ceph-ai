"""Xác minh sau khắc phục: lỗi đã hết thật chưa?

2026-08-20. Trước module này, `worker/llm/router_client.py::
_record_approved_execution_result` coi "lệnh SSH chạy xong exit 0" là
Incident RESOLVED. Đó là một phép suy sai: exit 0 chỉ chứng minh LỆNH chạy
được, không chứng minh VẤN ĐỀ đã hết. Một `systemctl restart ceph-osd@3`
trả về 0 xong OSD vẫn không lên lại vẫn khép Incident như thường, và không
có gì báo cho operator biết sự khác nhau — 175 Incident trên DB production
đang mang trạng thái RESOLVED được đóng theo đúng kiểu ấy.

Vòng đời mới, sau khi lệnh chạy xong:

    EXECUTING --(exit 0)--> VERIFYING --(chờ incident_verify_delay_seconds)
        |                                        |
        |                        đối chiếu `ceph health detail`
        |                                        |
        |                       ceph_code còn?  --- không --> RESOLVED
        |                                |                    + Telegram "✅ ĐÃ KHẮC PHỤC"
        |                               còn
        |                                |
        |                      hết lượt thử? -- rồi --> FAILED
        |                                |               + Telegram "⚠️ CHƯA KHẮC PHỤC ĐƯỢC"
        |                              chưa
        |                                |
        +---------------------- DIAGNOSING (đẩy lại cho AI, kèm ngữ cảnh
                                 "đã thử lệnh X, không hết") -> Action mới
                                 chờ người duyệt

Ba lựa chọn thiết kế đáng nói:

1. KHÔNG kiểm ngay sau khi lệnh chạy xong. Rất nhiều lỗi cần thời gian mới
   hết (PG backfill chạy xong, OSD vào lại quorum, mon clock skew hội tụ),
   nên kiểm tức thì sẽ luôn ra "chưa hết" một cách giả tạo rồi kéo theo một
   vòng chẩn đoán lại hoàn toàn vô ích. `verify_after` giữ mốc sớm nhất
   được phép kiểm.

2. Chạy trong Watcher chứ không phải Worker. Watcher vốn đã poll
   `ceph health detail` mỗi vòng — dùng lại đúng kết quả ấy, không mở thêm
   một đường query Ceph nào, và tự nhiên có sẵn cơ chế "quay lại sau" mà
   Worker (chạy theo message) không có.

3. Có TRẦN số vòng. Nhiều lỗi không lệnh tự động nào chữa được (CRUSH skew
   cần người cân lại weight, đĩa hỏng cần thay). Không có trần thì mỗi lượt
   poll là một lần gọi router tốn phí cộng một loạt Telegram, mãi mãi.
   Chạm trần là lúc hệ thống chủ động bỏ cuộc và nói rõ với người.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from config.settings import settings
from shared import audit, db, remediation_cases, telegram_alerts
from shared.models import Action, ActionStatus, Cluster, Incident, IncidentStatus, LogFinding, RemediationCase
from watcher import publisher
from watcher.ceph_code_families import is_monitor_owned
from worker.policy.playbook_registry import PostcheckResult, resolve_case_postcheck, run_postcheck

logger = logging.getLogger(__name__)
_LOG_ANOMALY_PREFIX = "LOG_ANOMALY:"


def _incident_display_name(session, incident: Incident) -> str | None:
    """Resolve synthetic Log Intelligence codes to the operator-facing title."""
    if not incident.ceph_code.startswith(_LOG_ANOMALY_PREFIX):
        return None
    short_key = incident.ceph_code[len(_LOG_ANOMALY_PREFIX):]
    finding = (
        session.query(LogFinding)
        .filter(LogFinding.cluster_id == incident.cluster_id)
        .filter(LogFinding.dedupe_key.like(f"{short_key}%"))
        .order_by(LogFinding.created_at.desc())
        .first()
    )
    return finding.title if finding is not None and finding.title else "Lỗi được phát hiện từ log"


def _cluster_channel_kwargs(cluster: Cluster | None) -> dict:
    """Kênh Telegram riêng của cụm nếu cụm ấy có cấu hình, ngược lại để
    `None` cho hàm alert dùng 3 kênh toàn cục — cùng quy ước mà
    `watcher/main.py::send_due_incident_reminders` đã dùng."""
    has_own = bool(cluster and cluster.telegram_bot_token and cluster.telegram_chat_id)
    return {
        "cluster_name": cluster.name if cluster else None,
        "bot_token": cluster.telegram_bot_token if has_own else None,
        "chat_id": cluster.telegram_chat_id if has_own else None,
        "enabled": cluster.telegram_enabled if has_own else None,
    }


def _last_attempted_command(session, incident_id: str) -> str | None:
    action = (
        session.query(Action)
        .filter(Action.incident_id == incident_id)
        .filter(Action.status == ActionStatus.EXECUTED.value)
        .order_by(Action.executed_at.desc())
        .first()
    )
    return action.proposed_command if action is not None else None


def _previous_attempts(session, incident_id: str) -> list[dict]:
    """Mọi lệnh ĐÃ chạy cho Incident này, để prompt chẩn đoán lại biết cái
    gì đã thử rồi mà không ăn thua. Thiếu phần này, model rất dễ đề xuất
    lại đúng lệnh vừa thất bại."""
    attempts = []
    for action in (
        session.query(Action)
        .filter(Action.incident_id == incident_id)
        .filter(Action.status == ActionStatus.EXECUTED.value)
        .order_by(Action.executed_at)
        .all()
    ):
        attempts.append(
            {
                "action_id": action.action_id,
                "command": action.proposed_command,
                "executed_at": action.executed_at.isoformat() if action.executed_at else None,
            }
        )
    return attempts


def _evaluate_latest_postcheck(
    session, incident: Incident, *, current_codes: set[str], health: dict | None,
) -> tuple[PostcheckResult, Action | None]:
    action = (
        session.query(Action)
        .filter(Action.incident_id == incident.id)
        .filter(Action.status.in_([
            ActionStatus.EXECUTED.value, ActionStatus.AUTO_EXECUTED.value,
        ]))
        .order_by(Action.executed_at.desc())
        .first()
    )
    if action is None:
        return PostcheckResult("INCONCLUSIVE", "no executed action exists for verification"), None
    case = session.query(RemediationCase).filter_by(action_id=action.id).one_or_none()
    if case is None or case.preflight_snapshot_json is None:
        # Compatibility for pre-Case-Memory rows. New actions always create a
        # Case with a contract snapshot; historical rows (including Pha-1
        # Cases created before Playbook Registry) retain the verified behavior
        # they had before Pha 2 and remain excluded from Trust Engine learning.
        outcome = "FAILED" if incident.ceph_code in current_codes else "PASSED"
        return PostcheckResult(outcome, "legacy action verified by fault absence"), action
    try:
        snapshot = json.loads(case.preflight_snapshot_json or "null")
    except (TypeError, ValueError):
        snapshot = None
    hook_id, error = resolve_case_postcheck(
        action_id=action.action_id, playbook_version=case.playbook_version,
        contract_snapshot=snapshot,
    )
    if error:
        return PostcheckResult("INCONCLUSIVE", error), action
    return run_postcheck(
        hook_id, fault_present=incident.ceph_code in current_codes, health=health,
    ), action


def verify_pending_incidents(
    current_codes: set[str],
    health: dict | None = None,
    cluster: Cluster | None = None,
    cluster_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Một lượt xác minh. Trả về {"verified": n, "retried": n, "exhausted": n}.

    `current_codes` là tập ceph_code mà lượt poll sức khoẻ VỪA RỒI thấy —
    người gọi phải truyền vào kết quả của chính lượt ấy, không được gọi hàm
    này khi lượt poll thất bại: `current_codes` rỗng vì MON không trả lời
    trông y hệt "cụm hoàn toàn khoẻ", và sẽ báo đã khắc phục cho mọi
    Incident đang chờ xác minh. Xem điều kiện gọi trong watcher/main.py.
    """
    now = now or datetime.utcnow()
    counts = {"verified": 0, "retried": 0, "exhausted": 0}
    max_attempts = max(1, settings.incident_verify_max_attempts)
    envelopes: list[dict] = []

    with db.SessionLocal() as session:
        pending = (
            session.query(Incident)
            .filter(Incident.status == IncidentStatus.VERIFYING.value)
            .filter(Incident.verify_after.isnot(None))
            .filter(Incident.verify_after <= now)
        )
        if cluster_id is None:
            pending = pending.filter(Incident.cluster_id.is_(None))
        else:
            pending = pending.filter(Incident.cluster_id == cluster_id)
        # Only one watcher may claim a due verification row.  In particular,
        # a temporary overlap between the container watcher and the legacy
        # remediation process must not produce duplicate “ĐÃ KHẮC PHỤC”
        # messages for one Incident.
        pending = pending.with_for_update(skip_locked=True).all()
        for incident in pending:
            if is_monitor_owned(incident.ceph_code):
                # Không bao giờ nên rơi vào đây (router_client đã lọc), nhưng
                # nếu có thì đối chiếu với current_codes là sai hoàn toàn —
                # để nguyên cho monitor sở hữu nó tự quyết.
                continue

            postcheck, verified_action = _evaluate_latest_postcheck(
                session, incident, current_codes=current_codes, health=health,
            )
            if postcheck.outcome == "INCONCLUSIVE":
                incident.status = IncidentStatus.FAILED.value
                incident.verify_after = None
                if verified_action is not None:
                    remediation_cases.record_inconclusive(
                        session, action_id=verified_action.id, at=now, reason=postcheck.reason,
                    )
                audit.record(
                    session, incident_id=incident.id,
                    action_id=verified_action.id if verified_action else None,
                    event_type=audit.EVENT_PLAYBOOK_POSTCHECK_INCONCLUSIVE,
                    actor=audit.ACTOR_SYSTEM,
                )
                counts["exhausted"] += 1
                continue

            if postcheck.outcome == "PASSED":
                incident.status = IncidentStatus.RESOLVED.value
                incident.verify_after = None
                command = _last_attempted_command(session, incident.id)
                audit.record(
                    session,
                    incident_id=incident.id,
                    action_id=None,
                    event_type=audit.EVENT_INCIDENT_FIX_VERIFIED,
                    actor=audit.ACTOR_SYSTEM,
                )
                remediation_cases.record_verified(
                    session, incident_id=incident.id, succeeded=True,
                    verified_at=now, post_state=health,
                )
                telegram_alerts.send_incident_verified_alert(
                    incident.ceph_code,
                    attempted_command=command,
                    display_name=_incident_display_name(session, incident),
                    **_cluster_channel_kwargs(cluster),
                )
                counts["verified"] += 1
                continue

            # Còn lỗi.
            incident.verify_attempts = (incident.verify_attempts or 0) + 1
            if incident.verify_attempts >= max_attempts:
                incident.status = IncidentStatus.FAILED.value
                incident.verify_after = None
                audit.record(
                    session,
                    incident_id=incident.id,
                    action_id=None,
                    event_type=audit.EVENT_INCIDENT_FIX_GAVE_UP,
                    actor=audit.ACTOR_SYSTEM,
                )
                remediation_cases.record_verified(
                    session, incident_id=incident.id, succeeded=False,
                    verified_at=now, post_state=health,
                )
                telegram_alerts.send_incident_verify_exhausted_alert(
                    incident.ceph_code,
                    incident.verify_attempts,
                    **_cluster_channel_kwargs(cluster),
                )
                counts["exhausted"] += 1
                continue

            # Còn lượt: đẩy lại cho AI chẩn đoán, kèm ngữ cảnh những gì đã thử.
            incident.status = IncidentStatus.DIAGNOSING.value
            incident.verify_after = None
            audit.record(
                session,
                incident_id=incident.id,
                action_id=None,
                event_type=audit.EVENT_INCIDENT_FIX_NOT_EFFECTIVE,
                actor=audit.ACTOR_SYSTEM,
            )
            envelopes.append(
                publisher.build_envelope(
                    incident_id=incident.id,
                    ceph_code=incident.ceph_code,
                    detected_at=now.isoformat(),
                    nodes=_nodes_from_last_action(session, incident.id),
                    log_excerpt=incident.log_excerpt or "",
                    cluster_snapshot=health or {},
                    cluster_id=cluster_id,
                    ssh_user=cluster.ssh_user if cluster else settings.ssh_user,
                    ssh_key_path=cluster.ssh_key_path if cluster else settings.ssh_key_path,
                    ceph_exec_mode=cluster.ceph_exec_mode if cluster else settings.ceph_exec_mode,
                    ceph_container_name=(
                        cluster.ceph_container_name if cluster else settings.ceph_container_name
                    ),
                    previous_attempts=_previous_attempts(session, incident.id),
                )
            )
            counts["retried"] += 1

        session.commit()

    if envelopes:
        try:
            asyncio.run(_publish_all(envelopes))
        except Exception:
            # Incident đã ở DIAGNOSING trong DB dù publish hỏng — không mất
            # dấu, chỉ là chưa vào hàng đợi. Cùng đánh đổi mà
            # build_and_publish_incident đã ghi rõ cho chính nó.
            logger.exception(
                "verify_pending_incidents: publish %d envelope chẩn đoán lại thất bại",
                len(envelopes),
            )

    return counts


def _nodes_from_last_action(session, incident_id: str) -> list[str]:
    """Dùng lại đúng danh sách node của lần thực thi trước — không tự dò
    lại, vì việc dò cần `check_detail` mà ở thời điểm này không còn."""
    action = (
        session.query(Action)
        .filter(Action.incident_id == incident_id)
        .order_by(Action.created_at.desc())
        .first()
    )
    if action is None or not action.target_nodes:
        return []
    try:
        nodes = json.loads(action.target_nodes)
    except (TypeError, ValueError):
        return []
    return [n for n in nodes if isinstance(n, str)] if isinstance(nodes, list) else []


async def _publish_all(envelopes: list[dict]) -> None:
    for envelope in envelopes:
        await publisher.publish_incident(envelope)
