import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import bcrypt

from config.settings import settings
from shared import db as db_module
from shared import env_config
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
def _pin_cluster_settings(monkeypatch, tmp_path):
    """Applies to every test in the suite (autouse, defined in the top-level
    conftest.py) — individual tests are still free to monkeypatch their own
    narrower values on top of these within their own test body; monkeypatch
    calls simply layer in call order, same as any other fixture override.

    2026-07-23 fix: ssh_key_path used to be pinned to a hardcoded real path
    (`/root/.ssh/ceph_lab_watcher`) that happens to exist on this specific
    lab machine (the actual Watcher SSH key) — every test that never
    explicitly overrides ssh_key_path itself (most of tests/
    test_dashboard_settings.py's cluster-connection tests) was silently
    passing an "does this key file exist" check only because of that
    coincidence, not because the test suite is actually hermetic. A truly
    clean environment (verified: GitHub Actions CI) has no such file, so
    those tests failed there while passing here — not flakiness, a real
    portability bug. Pointing this at a `tmp_path`-created file (guaranteed
    to exist, unique per test, cleaned up by pytest automatically) makes
    the suite depend on nothing outside its own tmp dir.
    """
    test_ssh_key_path = tmp_path / "ceph_lab_watcher_test_key"
    test_ssh_key_path.write_text("fake test-only private key, never used for a real SSH connection\n")

    # 2026-07-26 fix, verified live: real production incident — several
    # worker/executor/cluster_deploy.py tests exercise a FULL successful
    # run() (deploy or delete) without individually monkeypatching
    # env_config.update_env_file_batch (they only care about the SSH
    # commands sent), which let the code's own real _write_cluster_config/
    # _clear_cluster_config reach the REAL env_config.update_env_file_batch
    # — and because this suite runs on the SAME checkout as the live
    # Dashboard/Worker (not an isolated CI runner), that real function
    # wrote test-fixture node data straight into the ACTUAL project .env,
    # corrupting the real cluster's CEPH_MON_NODES/CEPH_OSD_NODES/etc. This
    # must be impossible regardless of whether any individual test author
    # remembers to mock the write function — redirecting ENV_PATH here,
    # autouse for the WHOLE suite, is the one place that guarantees it.
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_path / "test_dot_env")

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
    monkeypatch.setattr(settings, "ssh_key_path", str(test_ssh_key_path))

    # 2026-08-05 fix (found live), updated 2026-08-06 for the 3-independent-
    # channel redesign: a real .env on THIS machine had genuine Telegram
    # credentials configured (an operator actually testing the feature) —
    # every pre-existing test in tests/test_backup_alerting.py that never
    # touches these fields inherited those REAL values from the global
    # `settings` singleton, so worker/backup/alerting.py::send_alert's
    # Telegram delivery path fired for real (or against whatever the
    # test's own unrelated httpx.post fake returned, crashing with
    # AttributeError since that fake wasn't shaped like a real response).
    # Same "test suite must not depend on whatever a real .env happens to
    # contain" principle as every other field pinned above — blank by
    # default for all 3 channels here (blank = "not configured"); any test
    # that specifically exercises Telegram delivery already monkeypatches
    # these back to real-looking test values itself.
    monkeypatch.setattr(settings, "telegram_backup_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_backup_chat_id", "", raising=False)
    monkeypatch.setattr(settings, "telegram_incident_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_incident_chat_id", "", raising=False)
    monkeypatch.setattr(settings, "telegram_node_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_node_chat_id", "", raising=False)
    # 2026-08-07: separate per-channel `_enabled` toggle reintroduced (Alert
    # Telegram page) — pinned to True (its own default) for the same
    # leak-across-tests reason as the 6 fields above: it's written via
    # plain setattr in dashboard/routes/telegram_alerts.py::
    # telegram_channel_toggle, not monkeypatch.setattr, so a test that
    # flips one off would otherwise leave it off for every later test.
    monkeypatch.setattr(settings, "telegram_backup_enabled", True, raising=False)
    monkeypatch.setattr(settings, "telegram_incident_enabled", True, raising=False)
    monkeypatch.setattr(settings, "telegram_node_enabled", True, raising=False)
    # 2026-08-07: cluster_name is written the same way (plain setattr in
    # dashboard/routes/telegram_alerts.py::telegram_cluster_name_submit,
    # not monkeypatch.setattr) — same leak-across-tests/inherits-real-.env
    # risk as the 6 fields above, same fix.
    monkeypatch.setattr(settings, "cluster_name", "", raising=False)


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
