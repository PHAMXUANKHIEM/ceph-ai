"""Dọn các Action còn treo PENDING_APPROVAL dưới một Incident đã khép lại.

Chạy MỘT LẦN để làm sạch hậu quả đã tích luỹ của lỗi mà
`shared/incident_actions.py` mô tả: trước 2026-08-20, mọi
`create_or_resolve_*` trong watcher/ đóng Incident khi vấn đề tự hết nhưng
không đụng tới Action con, nên hàng chờ duyệt chỉ có tăng không có giảm.
Trên DB production lúc phát hiện: 141/153 Action PENDING_APPROVAL thuộc về
Incident đã RESOLVED, cái cũ nhất 13 ngày tuổi.

Từ nay watcher tự dọn ngay tại thời điểm đóng Incident, nên script này
KHÔNG cần chạy định kỳ — nó chỉ xử lý phần nợ cũ.

Dùng đúng `cancel_pending_actions` mà watcher dùng, không viết lại logic:
cùng một chuyển trạng thái, cùng một dấu vết audit, nên hàng dọn tay và
hàng watcher tự dọn về sau không thể phân kỳ.

Cách chạy (từ ceph-aiops/, có venv):
    .venv/bin/python -m scripts.cancel_orphan_actions            # chỉ xem, không sửa
    .venv/bin/python -m scripts.cancel_orphan_actions --apply    # thực sự ghi
"""

from __future__ import annotations

import argparse
from collections import Counter

from shared import db
from shared.incident_actions import cancel_pending_actions
from shared.models import Action, ActionStatus, Incident, IncidentStatus

# Một Incident ở các trạng thái này đã chốt xong — không hành động nào bên
# dưới nó còn nghĩa lý gì nữa. Cố ý KHÔNG gồm FAILED: một Incident thất bại
# vẫn là vấn đề chưa xử lý xong, đề xuất dưới nó vẫn còn giá trị.
_CLOSED_INCIDENT_STATUSES = (
    IncidentStatus.RESOLVED.value,
    IncidentStatus.REJECTED.value,
    IncidentStatus.AUTO_FIXED.value,
)

_OPEN_ACTION_STATUSES = (
    ActionStatus.PENDING.value,
    ActionStatus.PENDING_APPROVAL.value,
)


def find_orphans(session) -> list[tuple[Action, Incident]]:
    rows = (
        session.query(Action, Incident)
        .join(Incident, Action.incident_id == Incident.id)
        .filter(Action.status.in_(_OPEN_ACTION_STATUSES))
        .filter(Incident.status.in_(_CLOSED_INCIDENT_STATUSES))
        .order_by(Action.created_at)
        .all()
    )
    return [(action, incident) for action, incident in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Thực sự ghi vào DB. Không có cờ này thì chỉ liệt kê.",
    )
    args = parser.parse_args()

    with db.SessionLocal() as session:
        orphans = find_orphans(session)

        if not orphans:
            print("Không có Action mồ côi nào. Không cần làm gì.")
            return 0

        by_code = Counter(incident.ceph_code for _, incident in orphans)
        oldest = orphans[0][0].created_at

        print(f"Tìm thấy {len(orphans)} Action treo dưới Incident đã khép lại.")
        print(f"Cũ nhất: {oldest}")
        print("\nTheo ceph_code:")
        for code, count in by_code.most_common():
            print(f"  {count:4}  {code}")

        if not args.apply:
            print("\n(chỉ xem — chạy lại với --apply để thực sự huỷ)")
            return 0

        # Một Incident có thể có nhiều Action mồ côi, mà
        # cancel_pending_actions() đã xử lý trọn cả nhóm trong một lần gọi
        # — gọi lặp lại cho cùng incident_id chỉ tốn thêm truy vấn rỗng.
        cancelled = 0
        for incident_id in dict.fromkeys(action.incident_id for action, _ in orphans):
            cancelled += cancel_pending_actions(session, incident_id, actor="system:cleanup")
        session.commit()
        print(f"\nĐã huỷ {cancelled} Action (status -> REJECTED, kèm bản ghi audit).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
