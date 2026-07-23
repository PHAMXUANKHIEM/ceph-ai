from shared import db as db_module
from shared.auto_approve import AUTO_APPROVE_RESTART_OSD_KEY
from shared.models import SystemFlag


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _seed_flag(enabled: bool) -> None:
    with db_module.SessionLocal() as session:
        session.add(SystemFlag(key=AUTO_APPROVE_RESTART_OSD_KEY, value=enabled))
        session.commit()


def test_index_shows_off_state_by_default(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Tự động restart OSD: đang TẮT" in response.text
    assert "Bật tự động restart OSD" in response.text


def test_index_shows_active_state_when_enabled(dashboard_client):
    _seed_flag(True)
    _login(dashboard_client)

    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Tự động restart OSD ĐANG BẬT" in response.text
    assert "Tắt tự động restart OSD" in response.text


def test_post_auto_approve_restart_osd_true_enables_it(dashboard_client):
    _seed_flag(False)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/auto-approve-restart-osd", data={"enabled": "true"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    with db_module.SessionLocal() as session:
        flag = session.get(SystemFlag, AUTO_APPROVE_RESTART_OSD_KEY)
        assert flag.value is True


def test_post_auto_approve_restart_osd_false_disables_it(dashboard_client):
    _seed_flag(True)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/auto-approve-restart-osd", data={"enabled": "false"}, follow_redirects=False
    )

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        flag = session.get(SystemFlag, AUTO_APPROVE_RESTART_OSD_KEY)
        assert flag.value is False


def test_post_auto_approve_restart_osd_requires_login(dashboard_client):
    response = dashboard_client.post(
        "/auto-approve-restart-osd", data={"enabled": "true"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
