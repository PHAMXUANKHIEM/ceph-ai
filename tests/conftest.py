import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import bcrypt

from config.settings import settings
from shared import db as db_module
from shared.db import Base, make_engine

# Fixed test credentials — the fixture pins Settings to these values so tests
# never depend on whatever a real .env happens to contain (e.g. after a
# deployer changes the password before exposing the dashboard on a network).
TEST_USERNAME = "admin"
TEST_PASSWORD = "admin"
_TEST_PASSWORD_HASH = bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt()).decode()

# Fixed test cluster config — config/settings.py's real defaults for these
# fields are intentionally blank (a fresh deploy has no cluster configured
# until an operator sets one up via the Settings page), so the test suite
# must not depend on them. These values just keep every existing test's
# fixtures/assertions (mon hostname parsing, container names, ...) working
# regardless of what a real .env or the production defaults say.
TEST_CEPH_MON_NODES = "10.20.1.150,10.20.1.249,10.20.1.253"
TEST_CEPH_MON_HOSTNAMES = "khiempx-mon1,khiempx-mon2,khiempx-mon3"
TEST_CEPH_CONTAINER_NAME = "ceph-mon-B"
TEST_CEPH_OSD_NODES = "10.20.1.83,10.20.1.78,10.20.1.1"
TEST_CEPH_OSD_CONTAINER_NAME = "ceph-osd-B"
TEST_SSH_KEY_PATH = "/root/.ssh/ceph_lab_watcher"
# Blank by default (like the real production default) — most tests don't
# care about MGR nodes at all, and a nonblank value here would silently leak
# an extra configured host into every test that lists nodes (e.g. the Nodes
# page) unless that test explicitly monkeypatches it, same trap this
# comment is here to prevent for ceph_exec_mode below.
TEST_CEPH_MGR_NODES = ""
# Same reasoning as TEST_CEPH_MGR_NODES above — blank by default so no test
# accidentally picks up an RGW node/container it didn't ask for.
TEST_CEPH_RGW_NODES = ""
TEST_CEPH_RGW_CONTAINER_NAME = ""
# Pinned rather than left to whatever a real .env on this machine says —
# the whole suite was written assuming "docker" (build_exec_command's
# `docker exec <container> <cmd>` shape); a real operator's .env can
# legitimately set CEPH_EXEC_MODE=cephadm for their actual cluster, which
# would otherwise silently change every unrelated test's command shape.
TEST_CEPH_EXEC_MODE = "docker"

# Fixed test 9router config — config/settings.py's real defaults for these
# are blank (a fresh deploy has no AI provider configured until an operator
# connects via the Settings page), so the test suite must not depend on
# whatever a real .env happens to contain either. Every test that exercises
# the actual router call path monkeypatches dashboard.chat_client/
# worker.llm.router_client's own _get_client (or equivalent) to a fake —
# these just need to be non-blank so the "not configured" early-exit path
# isn't accidentally taken instead. Tests exercising THAT path itself
# monkeypatch router_api_key/router_base_url back to "" explicitly (this
# app has no direct-to-vendor fallback — shared/router_client.py's
# build_router_client raises RouterNotConfiguredError on either being
# blank, by policy).
TEST_ROUTER_API_KEY = "sk-test-fake-key"
TEST_ROUTER_MODEL = "gc/gemini-2.5-flash"
TEST_ROUTER_BASE_URL = "http://localhost:20128"


@pytest.fixture(autouse=True)
def _pin_cluster_settings(monkeypatch):
    """Applies to every test in the suite (autouse, defined in the top-level
    conftest.py) — individual tests are still free to monkeypatch their own
    narrower values on top of these within their own test body; monkeypatch
    calls simply layer in call order, same as any other fixture override."""
    monkeypatch.setattr(settings, "router_api_key", TEST_ROUTER_API_KEY)
    monkeypatch.setattr(settings, "router_model", TEST_ROUTER_MODEL)
    monkeypatch.setattr(settings, "router_base_url", TEST_ROUTER_BASE_URL)
    monkeypatch.setattr(settings, "router_enabled", True)
    monkeypatch.setattr(settings, "ceph_mon_nodes", TEST_CEPH_MON_NODES)
    monkeypatch.setattr(settings, "ceph_mon_hostnames", TEST_CEPH_MON_HOSTNAMES)
    monkeypatch.setattr(settings, "ceph_container_name", TEST_CEPH_CONTAINER_NAME)
    monkeypatch.setattr(settings, "ceph_osd_nodes", TEST_CEPH_OSD_NODES)
    monkeypatch.setattr(settings, "ceph_osd_container_name", TEST_CEPH_OSD_CONTAINER_NAME)
    monkeypatch.setattr(settings, "ceph_mgr_nodes", TEST_CEPH_MGR_NODES)
    monkeypatch.setattr(settings, "ceph_rgw_nodes", TEST_CEPH_RGW_NODES)
    monkeypatch.setattr(settings, "ceph_rgw_container_name", TEST_CEPH_RGW_CONTAINER_NAME)
    monkeypatch.setattr(settings, "ceph_exec_mode", TEST_CEPH_EXEC_MODE)
    monkeypatch.setattr(settings, "ssh_key_path", TEST_SSH_KEY_PATH)


@pytest.fixture()
def db_session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)


@pytest.fixture()
def dashboard_client(monkeypatch):
    """TestClient wired to an isolated in-memory DB and fixed test credentials.

    Route modules call `db.SessionLocal()` (module attribute lookup at call
    time, not `from shared.db import SessionLocal`), so monkeypatching these
    two attributes here is picked up by the app without needing dependency
    overrides.

    Uses StaticPool (one shared connection) instead of plain `make_engine`:
    TestClient runs the ASGI app on a background thread via an anyio portal,
    and SQLite's default per-thread pooling for `:memory:` DBs means that
    thread would otherwise see a *different*, empty in-memory database than
    the one `create_all` populated on the main test thread.

    `settings.dashboard_username`/`dashboard_password_hash` are pinned to
    TEST_USERNAME/TEST_PASSWORD regardless of any real `.env` on disk —
    without this, a deployer changing the real password (e.g. before
    exposing the dashboard on a network) silently breaks these tests.
    """
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=test_engine, autoflush=False, autocommit=False))
    monkeypatch.setattr(settings, "dashboard_username", TEST_USERNAME)
    monkeypatch.setattr(settings, "dashboard_password_hash", _TEST_PASSWORD_HASH)

    from dashboard.app import app
    from dashboard.routes import auth as auth_module

    # The login rate limiter is process-global state keyed by client host,
    # and TestClient always reports the same synthetic host — without
    # clearing it here, failed-login attempts in one test would count
    # towards another test's lockout threshold.
    auth_module._failed_attempts.clear()

    with TestClient(app) as client:
        yield client
