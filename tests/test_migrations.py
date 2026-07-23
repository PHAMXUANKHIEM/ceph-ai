import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from config.settings import settings
from shared.models import Incident

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_alembic_upgrade(db_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(cfg, "head")


def test_alembic_upgrade_head_creates_incidents_table_matching_model(tmp_path, monkeypatch):
    db_path = tmp_path / "migration_test.db"
    _run_alembic_upgrade(db_path, monkeypatch)

    con = sqlite3.connect(db_path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(incidents)")}
    con.close()

    model_columns = {c.name for c in Incident.__table__.columns}
    assert columns == model_columns


def test_alembic_upgrade_head_enforces_status_check_constraint(tmp_path, monkeypatch):
    db_path = tmp_path / "migration_test.db"
    _run_alembic_upgrade(db_path, monkeypatch)

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO incidents (id, ceph_code, status, detected_at, created_at, updated_at) "
            "VALUES ('x', 'OSD_DOWN', 'NOT_REAL', '2026-01-01', '2026-01-01', '2026-01-01')"
        )
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        con.close()

    assert raised, "migration-created table should reject a status outside IncidentStatus"
