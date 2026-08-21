"""Đóng hàng chờ duyệt khi Incident cha đã khép lại.

2026-08-20 -- vá một lỗ hổng có thật, đo được trên DB production: 141 trong
153 Action đang PENDING_APPROVAL thuộc về Incident đã RESOLVED. Mọi
`create_or_resolve_*` trong watcher/ đều đóng Incident khi vấn đề tự hết,
nhưng KHÔNG cái nào đụng tới Action con -- nên yêu cầu duyệt nằm lại vĩnh
viễn, và:

  * `dashboard/telegram_approval_bot.py::_notify_pending_actions` quét TOÀN
    BỘ hàng PENDING_APPROVAL mỗi 10 giây, đời đời;
  * operator nhìn thấy một hàng dài đề xuất cho những sự cố không còn tồn
    tại, không cách nào phân biệt với đề xuất còn giá trị;
  * tệ nhất: bấm "✅ Duyệt" trên một đề xuất zombie vẫn chạy lệnh thật, dựa
    trên bằng chứng đã cũ hàng tuần.

Không thêm giá trị mới vào `ActionStatus` (cột có CHECK constraint, thêm
giá trị là phải migration) -- dùng REJECTED, đúng nghĩa "sẽ không bao giờ
được thực thi", đã là trạng thái cuối mà mọi màn hình/truy vấn hiện có xử
lý đúng. Cái phân biệt "máy tự huỷ" với "người từ chối" nằm ở AuditEntry:
EVENT_RISKY_ACTION_AUTO_CANCELLED_INCIDENT_RESOLVED, actor `system:watcher`.

Đặt ở shared/ chứ không phải watcher/ vì worker/ cũng đóng Incident
(worker/llm/router_client.py) và AD-3 cấm watcher/worker import lẫn nhau.
"""

from __future__ import annotations

from shared import audit
from shared.models import Action, ActionStatus, Incident, IncidentStatus

# Chỉ những trạng thái CHƯA chốt mới bị huỷ theo. Một Action đã APPROVED
# là đã có người bấm duyệt và có thể đang chạy dở trên cụm -- huỷ nó ở đây
# sẽ nói dối về một lệnh vẫn đang thực thi. Để nguyên cho worker chốt.
_CANCELLABLE_STATUSES = (
    ActionStatus.PENDING.value,
    ActionStatus.PENDING_APPROVAL.value,
)

SYSTEM_ACTOR = "system:watcher"

_TERMINAL_INCIDENT_STATUSES = (
    IncidentStatus.AUTO_FIXED.value,
    IncidentStatus.RESOLVED.value,
    IncidentStatus.REJECTED.value,
)


def cancel_pending_actions(session, incident_id: str, actor: str = SYSTEM_ACTOR) -> int:
    """Huỷ mọi Action chưa chốt của `incident_id`, trả về số dòng đã huỷ.

    KHÔNG commit -- giống `shared/audit.py::record`, người gọi giữ quyền
    quyết định ranh giới transaction, để việc đóng Action luôn nguyên tử
    cùng việc đóng Incident đã kéo theo nó.
    """
    actions = (
        session.query(Action)
        .filter(Action.incident_id == incident_id)
        .filter(Action.status.in_(_CANCELLABLE_STATUSES))
        .all()
    )
    for action in actions:
        action.status = ActionStatus.REJECTED.value
        audit.record(
            session,
            incident_id=incident_id,
            action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_AUTO_CANCELLED_INCIDENT_RESOLVED,
            actor=actor,
        )
    return len(actions)


def reconcile_terminal_incident_actions(session, actor: str = SYSTEM_ACTOR) -> int:
    """Huỷ action mồ côi còn chờ dù Incident cha đã kết thúc.

    Đây là hàng rào tự sửa dữ liệu lịch sử và các luồng đóng Incident không
    gọi trực tiếp ``cancel_pending_actions``. Không commit; caller giữ ranh
    giới transaction. Action APPROVED/EXECUTED/FAILED không bị thay đổi.
    """
    incident_ids = (
        session.query(Action.incident_id)
        .join(Incident, Incident.id == Action.incident_id)
        .filter(
            Incident.status.in_(_TERMINAL_INCIDENT_STATUSES),
            Action.status.in_(_CANCELLABLE_STATUSES),
        )
        .distinct()
        .all()
    )
    return sum(
        cancel_pending_actions(session, incident_id, actor=actor)
        for (incident_id,) in incident_ids
    )
