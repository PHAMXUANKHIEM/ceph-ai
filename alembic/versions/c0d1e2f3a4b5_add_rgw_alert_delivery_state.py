"""persist RGW Telegram placeholder and external delivery state"""

import sqlalchemy as sa
from alembic import op

revision = "c0d1e2f3a4b5"
down_revision = "b9c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rgw_access_audit_events",
        sa.Column("external_alert_queued", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "rgw_access_audit_events",
        sa.Column("external_alert_queued_at", sa.DateTime(), nullable=True),
    )

    op.add_column(
        "rgw_error_notifications",
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "rgw_error_notifications",
        sa.Column(
            "telegram_humanization_status",
            sa.String(32),
            nullable=False,
            server_default="not_started",
        ),
    )
    op.add_column(
        "rgw_error_notifications",
        sa.Column("telegram_humanization_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "rgw_error_notifications",
        sa.Column("telegram_humanized_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "rgw_error_notifications",
        sa.Column("external_alert_queued", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "rgw_error_notifications",
        sa.Column("external_alert_queued_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_rgw_error_notification_humanization_pending",
        "rgw_error_notifications",
        ["telegram_sent", "telegram_humanization_status", "external_alert_queued"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rgw_error_notification_humanization_pending",
        table_name="rgw_error_notifications",
    )
    with op.batch_alter_table("rgw_error_notifications") as batch_op:
        for name in (
            "external_alert_queued_at",
            "external_alert_queued",
            "telegram_humanized_at",
            "telegram_humanization_error",
            "telegram_humanization_status",
            "telegram_message_id",
        ):
            batch_op.drop_column(name)
    with op.batch_alter_table("rgw_access_audit_events") as batch_op:
        batch_op.drop_column("external_alert_queued_at")
        batch_op.drop_column("external_alert_queued")
