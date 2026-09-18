import pytest

from config.settings import (
    DEFAULT_DASHBOARD_PASSWORD_HASH,
    DEFAULT_SESSION_SECRET_KEY,
    settings,
)
from dashboard import app as dashboard_app
from shared import db


def test_production_database_rejects_sqlite(monkeypatch):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")

    with pytest.raises(RuntimeError, match="requires a PostgreSQL DATABASE_URL"):
        db.make_engine("sqlite:///:memory:")


def test_production_database_accepts_postgresql(monkeypatch):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")

    engine = db.make_engine("postgresql+psycopg://user:password@db.example/ceph_aiops")
    assert engine.pool.size() == 3
    assert engine.pool._recycle == 300
    engine.dispose()


def test_production_dashboard_rejects_dev_security_defaults(monkeypatch):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")
    monkeypatch.setattr(settings, "dashboard_password_hash", DEFAULT_DASHBOARD_PASSWORD_HASH)
    monkeypatch.setattr(settings, "session_secret_key", DEFAULT_SESSION_SECRET_KEY)

    with pytest.raises(RuntimeError, match="Production security configuration rejected"):
        dashboard_app._warn_if_using_dev_defaults()


def test_non_production_dashboard_keeps_dev_warning_only(monkeypatch, caplog):
    monkeypatch.setattr(settings, "ceph_ai_environment", "development")
    monkeypatch.setattr(settings, "dashboard_password_hash", DEFAULT_DASHBOARD_PASSWORD_HASH)
    monkeypatch.setattr(settings, "session_secret_key", DEFAULT_SESSION_SECRET_KEY)

    dashboard_app._warn_if_using_dev_defaults()

    assert "DEFAULT dev-only password" in caplog.text
    assert "DEFAULT dev-only SESSION_SECRET_KEY" in caplog.text
