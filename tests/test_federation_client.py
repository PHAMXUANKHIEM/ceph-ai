"""Customer federation client (shared/federation_client.py, dashboard/routes/federation.py)."""

import json
import stat

import psycopg
import pytest

from config.settings import settings
from shared import db as db_module
from shared import federation_client
from shared.models import Action, Incident
from shared.time import utc_now

CODE = "CAI-ABCD-EFGH-JKMN-PQRS-TUVW"
ENROLLED = ("1ec55fdca1c1", "cust_1ec55fdca1c1", "s3cret-password", "inbox.c_1ec55fdca1c1", "basic")


class FakeConnection:
    def __init__(self, server, user, password):
        self.server, self.user, self.password = server, user, password

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params):
        self.server.calls.append((self.user, self.password, query, params))
        if self.server.error is not None:
            raise self.server.error
        return self

    def fetchone(self):
        return ENROLLED


class FakeServer:
    def __init__(self):
        self.calls, self.error = [], None

    def connect(self, user, password):
        return FakeConnection(self, user, password)


@pytest.fixture
def server(tmp_path, monkeypatch):
    fake = FakeServer()
    (tmp_path / "enroll.pass").write_text("shipped-enroll-password\n")
    monkeypatch.setattr(settings, "federation_enroll_password_file", str(tmp_path / "enroll.pass"))
    monkeypatch.setattr(settings, "federation_state_file", str(tmp_path / "config" / "federation.json"))
    monkeypatch.setattr(federation_client, "_connect", fake.connect)
    return fake


@pytest.fixture
def admin(dashboard_client, server):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    return dashboard_client


def test_enrolling_keeps_the_login_in_a_private_file_and_never_shows_the_password(admin, server):
    response = admin.post("/api/federation/enroll", json={"code": " cai-abcd-efgh-jkmn-pqrs-tuvw "})

    assert response.status_code == 200, response.text
    assert response.json()["customer_id"] == "1ec55fdca1c1" and response.json()["enrolled"] is True
    assert "s3cret-password" not in response.text
    user, password, _query, params = server.calls[0]
    assert (user, password, params[0]) == ("fed_enroll", "shipped-enroll-password", CODE)
    path = federation_client._state_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["password"] == "s3cret-password"
    assert "s3cret-password" not in admin.get("/federation").text
    assert "s3cret-password" not in admin.get("/api/federation").text


@pytest.mark.parametrize("code", ["", "CAI-1234", "ABC-ABCD-EFGH-JKMN-PQRS-TUVW", "CAI-ABCD-EFGH-JKMN-PQRS-TUVW'; --"])
def test_malformed_codes_never_reach_the_server(admin, server, code):
    assert admin.post("/api/federation/enroll", json={"code": code}).status_code == 400
    assert server.calls == []


def test_a_rejected_code_is_reported_and_nothing_is_stored(admin, server):
    server.error = psycopg.errors.InvalidAuthorizationSpecification("activation code rejected")

    response = admin.post("/api/federation/enroll", json={"code": CODE})

    assert response.status_code == 400 and "Mã kích hoạt không hợp lệ" in response.json()["detail"]
    assert federation_client.enrolled(federation_client.load_state()) is False


def test_enrolling_needs_the_shipped_password_file(admin, server, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "federation_enroll_password_file", str(tmp_path / "missing"))
    response = admin.post("/api/federation/enroll", json={"code": CODE})
    assert response.status_code == 400 and "Thiếu file mật khẩu" in response.json()["detail"]


def test_the_payload_is_counts_only(admin):
    with db_module.SessionLocal() as session:
        incident = Incident(ceph_code="OSD_DOWN", severity="high", detected_at=utc_now(), log_excerpt="osd.3 on ceph-secret-host 10.9.8.7 down")
        session.add(incident)
        session.flush()
        session.add(Action(incident_id=incident.id, action_id="restart_osd", classification="SAFE", status="EXECUTED",
                           proposed_command="ceph orch daemon restart osd.3"))
        session.commit()

    payload = admin.get("/api/federation/preview").json()

    assert payload["incidents_by_code"] == {"OSD_DOWN": 1}
    assert payload["actions"] == {"restart_osd (SAFE)": {"EXECUTED": 1}}
    text = json.dumps(payload)
    assert "ceph-secret-host" not in text and "10.9.8.7" not in text and "osd.3" not in text


def test_push_inserts_into_the_installs_own_inbox_as_its_own_login(admin, server):
    admin.post("/api/federation/enroll", json={"code": CODE})

    response = admin.post("/api/federation/push")

    assert response.status_code == 200 and response.json()["last_push_at"]
    user, password, query, params = server.calls[-1]
    assert (user, password) == ("cust_1ec55fdca1c1", "s3cret-password")
    assert '"c_1ec55fdca1c1"' in query.as_string(None)
    assert params[0] == 1 and json.loads(params[1])["payload_version"] == 1


def test_a_failed_push_is_remembered_for_the_page(admin, server):
    admin.post("/api/federation/enroll", json={"code": CODE})
    server.error = psycopg.OperationalError("connection refused")

    assert admin.post("/api/federation/push").status_code == 400
    assert "Không kết nối được" in admin.get("/api/federation").json()["last_push_error"]


def test_forgetting_drops_the_login_but_keeps_the_instance_id(admin, server):
    admin.post("/api/federation/enroll", json={"code": CODE})
    instance = federation_client.load_state()["instance_id"]

    admin.post("/api/federation/forget")

    assert federation_client.load_state() == {"instance_id": instance}
    assert admin.post("/api/federation/push").status_code == 400


def test_non_admins_cannot_use_federation(dashboard_client, server, monkeypatch):
    from dashboard.routes import auth
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    monkeypatch.setattr(auth, "is_admin_user", lambda user: False)
    assert dashboard_client.get("/federation").status_code == 403
    assert dashboard_client.post("/api/federation/enroll", json={"code": CODE}).status_code == 403
