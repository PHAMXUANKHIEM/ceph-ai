"""Record the online-learning backend identity in learner evidence."""

from alembic import op
import sqlalchemy as sa


revision = "m20260921onlinebackend"
down_revision = "m20260921forecastscopefix"
branch_labels = None
depends_on = None


_TABLES = ("online_learner_audit", "online_learner_cycle_audit")
_COLUMNS = (
    ("backend_name", "river"),
    ("backend_version", "0.25.0"),
    ("feature_schema", "scalar-v1"),
)


def upgrade() -> None:
    for table in _TABLES:
        for column, default in _COLUMNS:
            op.add_column(
                table,
                sa.Column(
                    column,
                    sa.String(length=64),
                    nullable=False,
                    server_default=sa.text(f"'{default}'"),
                ),
            )
        # Keep the server default for compatibility with older writers and
        # SQLite's limited ALTER TABLE support. New consumers always write
        # the runtime identity explicitly.


def downgrade() -> None:
    for table in reversed(_TABLES):
        for column, _default in reversed(_COLUMNS):
            op.drop_column(table, column)
