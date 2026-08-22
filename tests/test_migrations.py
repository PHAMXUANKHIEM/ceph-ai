import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from config.settings import settings
from shared.models import (
    Action, Incident, ObjectStorageAuditEntry, PatchDocument, PlaybookStat,
    RemediationCase, User,
)

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


def test_alembic_upgrade_head_creates_users_table_matching_model(tmp_path, monkeypatch):
    db_path = tmp_path / "migration_test.db"
    _run_alembic_upgrade(db_path, monkeypatch)

    con = sqlite3.connect(db_path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(users)")}
    con.close()

    model_columns = {c.name for c in User.__table__.columns}
    assert columns == model_columns


def test_alembic_upgrade_head_creates_object_storage_audit_table(tmp_path, monkeypatch):
    db_path = tmp_path / "migration_test.db"
    _run_alembic_upgrade(db_path, monkeypatch)
    con = sqlite3.connect(db_path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(object_storage_audit_entries)")}
    con.close()
    assert columns == {column.name for column in ObjectStorageAuditEntry.__table__.columns}


def test_alembic_upgrade_head_creates_patch_documents_table_matching_model(tmp_path, monkeypatch):
    db_path = tmp_path / "migration_test.db"
    _run_alembic_upgrade(db_path, monkeypatch)

    con = sqlite3.connect(db_path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(patch_documents)")}
    con.close()

    model_columns = {c.name for c in PatchDocument.__table__.columns}
    assert columns == model_columns


def test_alembic_upgrade_head_creates_actions_table_matching_model(tmp_path, monkeypatch):
    db_path = tmp_path / "migration_test.db"
    _run_alembic_upgrade(db_path, monkeypatch)

    con = sqlite3.connect(db_path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(actions)")}
    con.close()

    model_columns = {c.name for c in Action.__table__.columns}
    assert columns == model_columns


@pytest.mark.parametrize("model", [RemediationCase, PlaybookStat])
def test_alembic_upgrade_head_creates_case_memory_tables_matching_models(
    tmp_path, monkeypatch, model,
):
    db_path = tmp_path / f"{model.__tablename__}.db"
    _run_alembic_upgrade(db_path, monkeypatch)
    con = sqlite3.connect(db_path)
    columns = {row[1] for row in con.execute(f"PRAGMA table_info({model.__tablename__})")}
    con.close()
    assert columns == {column.name for column in model.__table__.columns}


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


def test_alembic_upgrade_head_accepts_database_url_containing_percent_characters(tmp_path, monkeypatch):
    """Regression test: a DATABASE_URL with a percent-encoded password (e.g.
    `...:pass%40word@...` — routine for a generated password containing `@`,
    `:`, `/`, or `%` itself) used to raise
    `ValueError: invalid interpolation syntax` from env.py's
    `config.set_main_option("sqlalchemy.url", ...)` — a bare `%` is stdlib
    ConfigParser's own interpolation escape character, unrelated to URL
    percent-encoding. Encoded here as an extra sqlite URL query param (sqlite
    ignores unknown query params) purely to get a literal `%` into the exact
    string env.py hands to set_main_option, without needing a real
    percent-encoded password."""
    db_path = tmp_path / "migration_test_percent.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}?note=pass%40word%3Bhere")
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(cfg, "head")  # must not raise ValueError: invalid interpolation syntax

    con = sqlite3.connect(db_path)
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "incidents" in tables
