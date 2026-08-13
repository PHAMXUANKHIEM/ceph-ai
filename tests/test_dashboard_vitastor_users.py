import bcrypt

from shared import db as db_module
from shared.models import User, VitastorUser


def _login_vitastor(client, username="admin", password="admin"):
    return client.post(
        "/login",
        data={"username": username, "password": password, "product": "vitastor"},
        follow_redirects=True,
    )


def test_root_admin_can_open_vitastor_users(dashboard_client):
    _login_vitastor(dashboard_client)

    response = dashboard_client.get("/vitastor/users")

    assert response.status_code == 200
    assert 'action="/vitastor/users/create"' in response.text
    assert "chỉ có quyền đăng nhập Vitastor" in response.text


def test_create_vitastor_user_uses_separate_table(dashboard_client):
    _login_vitastor(dashboard_client)

    response = dashboard_client.post(
        "/vitastor/users/create",
        data={
            "new_username": "vita-op",
            "new_password": "s3cret-pw",
            "new_password_confirm": "s3cret-pw",
        },
    )

    assert response.status_code == 200
    assert "Đã tạo Vitastor user" in response.text
    with db_module.SessionLocal() as session:
        assert session.query(VitastorUser).filter_by(username="vita-op").one()
        assert session.query(User).filter_by(username="vita-op").first() is None


def test_vitastor_user_can_login_only_to_vitastor(dashboard_client):
    _login_vitastor(dashboard_client)
    dashboard_client.post(
        "/vitastor/users/create",
        data={
            "new_username": "vita-op",
            "new_password": "s3cret-pw",
            "new_password_confirm": "s3cret-pw",
        },
    )
    dashboard_client.post("/logout")

    vita_login = _login_vitastor(dashboard_client, "vita-op", "s3cret-pw")
    assert vita_login.status_code == 200
    assert vita_login.url.path == "/vitastor"
    dashboard_client.post("/logout")

    ceph_login = dashboard_client.post(
        "/login",
        data={"username": "vita-op", "password": "s3cret-pw", "product": "ceph"},
        follow_redirects=False,
    )
    assert ceph_login.status_code == 401


def test_ceph_user_does_not_gain_vitastor_access(dashboard_client):
    with db_module.SessionLocal() as session:
        session.add(User(
            username="ceph-only",
            password_hash=bcrypt.hashpw(b"s3cret-pw", bcrypt.gensalt()).decode(),
            is_admin=True,
            is_active=True,
            created_by="admin",
        ))
        session.commit()

    response = dashboard_client.post(
        "/login",
        data={"username": "ceph-only", "password": "s3cret-pw", "product": "vitastor"},
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_vitastor_admin_can_toggle_and_delete_user(dashboard_client):
    _login_vitastor(dashboard_client)
    dashboard_client.post(
        "/vitastor/users/create",
        data={
            "new_username": "temporary",
            "new_password": "s3cret-pw",
            "new_password_confirm": "s3cret-pw",
        },
    )
    with db_module.SessionLocal() as session:
        user_id = session.query(VitastorUser).filter_by(username="temporary").one().id

    dashboard_client.post(f"/vitastor/users/{user_id}/toggle-active")
    with db_module.SessionLocal() as session:
        assert session.get(VitastorUser, user_id).is_active is False

    response = dashboard_client.post(f"/vitastor/users/{user_id}/delete")
    assert "Đã xoá Vitastor user" in response.text
    with db_module.SessionLocal() as session:
        assert session.get(VitastorUser, user_id) is None


def test_non_admin_vitastor_user_cannot_manage_users(dashboard_client):
    with db_module.SessionLocal() as session:
        session.add(VitastorUser(
            username="viewer",
            password_hash=bcrypt.hashpw(b"s3cret-pw", bcrypt.gensalt()).decode(),
            is_admin=False,
            is_active=True,
            created_by="admin",
        ))
        session.commit()
    _login_vitastor(dashboard_client, "viewer", "s3cret-pw")

    assert dashboard_client.get("/vitastor/users").status_code == 403
