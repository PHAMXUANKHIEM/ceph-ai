"""add incident verification state

Thêm IncidentStatus.VERIFYING cùng hai cột theo dõi vòng xác minh.

Trước đây "lệnh SSH chạy xong exit 0" bị coi thẳng là Incident RESOLVED
(worker/llm/router_client.py::_record_approved_execution_result), không ai
hỏi lại cụm xem lỗi đã thật sự hết chưa. VERIFYING là khoảng giữa: sau một
khoảng chờ, watcher/verify.py đối chiếu ceph_code với `ceph health detail`
rồi mới quyết RESOLVED (kèm Telegram báo OK) hay quay lại chẩn đoán.

Revision ID: 2c06619773a5
Revises: 452507aa3c32
Create Date: 2026-08-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2c06619773a5"
down_revision: Union[str, Sequence[str], None] = "452507aa3c32"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CONSTRAINT_NAME = "ck_incidents_status_valid"

# Phải khớp shared/models.py::IncidentStatus. Alembic autogenerate không
# phát hiện được thay đổi của CheckConstraint nên migration này viết tay,
# cùng nếp alembic/versions/1bd5de967b1a_add_status_check_constraint.py.
_STATUS_VALUES_BEFORE = (
    "NEW", "DIAGNOSING", "AUTO_FIXED", "PENDING_APPROVAL",
    "APPROVED", "EXECUTING", "RESOLVED", "REJECTED", "FAILED",
)
_STATUS_VALUES_AFTER = (
    "NEW", "DIAGNOSING", "AUTO_FIXED", "PENDING_APPROVAL",
    "APPROVED", "EXECUTING", "VERIFYING", "RESOLVED", "REJECTED", "FAILED",
)


def _replace_status_constraint(values: tuple[str, ...]) -> None:
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.drop_constraint(CONSTRAINT_NAME, type_="check")
        batch_op.create_check_constraint(
            CONSTRAINT_NAME, "status IN ('" + "','".join(values) + "')"
        )


def upgrade() -> None:
    op.add_column("incidents", sa.Column("verify_after", sa.DateTime(), nullable=True))
    # server_default="0" để những dòng đã tồn tại có giá trị hợp lệ ngay,
    # không cần backfill riêng — cột NOT NULL mà thiếu default sẽ hỏng ở
    # đúng bước ALTER trên một bảng đã có dữ liệu.
    op.add_column(
        "incidents",
        sa.Column("verify_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    _replace_status_constraint(_STATUS_VALUES_AFTER)


def downgrade() -> None:
    # Hạ cấp phải đưa mọi dòng VERIFYING về một giá trị mà constraint cũ
    # chấp nhận, nếu không chính lệnh tạo lại constraint sẽ thất bại.
    # FAILED chứ không phải RESOLVED: một Incident đang VERIFYING theo định
    # nghĩa là CHƯA được xác nhận đã khắc phục, đánh nó là RESOLVED lúc hạ
    # cấp là nói dối về trạng thái cụm.
    op.execute("UPDATE incidents SET status = 'FAILED' WHERE status = 'VERIFYING'")
    _replace_status_constraint(_STATUS_VALUES_BEFORE)
    op.drop_column("incidents", "verify_attempts")
    op.drop_column("incidents", "verify_after")
