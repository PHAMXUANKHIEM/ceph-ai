from shared import db as db_module
from shared.models import SystemFlag


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _seed_flag(enabled: bool) -> None:
    with db_module.SessionLocal() as session:
        session.add(SystemFlag(key="kill_switch_enabled", value=enabled))
        session.commit()


def test_index_shows_kill_switch_off_button_when_disabled(dashboard_client):
    _seed_flag(False)
    _login(dashboard_client)

    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "KILL-SWITCH ĐANG BẬT" not in response.text
    assert "Bật khẩn cấp" in response.text


def test_index_shows_kill_switch_active_banner_when_enabled(dashboard_client):
    _seed_flag(True)
    _login(dashboard_client)

    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "KILL-SWITCH ĐANG BẬT" in response.text


def test_post_kill_switch_true_enables_it(dashboard_client):
    _seed_flag(False)
    _login(dashboard_client)

    response = dashboard_client.post("/kill-switch", data={"enabled": "true"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    with db_module.SessionLocal() as session:
        flag = session.get(SystemFlag, "kill_switch_enabled")
        assert flag.value is True


def test_post_kill_switch_false_disables_it(dashboard_client):
    _seed_flag(True)
    _login(dashboard_client)

    response = dashboard_client.post("/kill-switch", data={"enabled": "false"}, follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        flag = session.get(SystemFlag, "kill_switch_enabled")
        assert flag.value is False


def test_post_kill_switch_requires_login(dashboard_client):
    response = dashboard_client.post("/kill-switch", data={"enabled": "true"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
