import bcrypt
import pytest

import dashboard.routes.nodes as nodes_route
from shared import db as db_module
from shared.models import User


@pytest.fixture(autouse=True)
def _fast_list_osds_default(monkeypatch):
    """2026-08-04: this file's own tests hit GET /nodes (nav-link checks),
    which now also calls watcher.ceph_client.list_osds() on every page
    load — see tests/test_dashboard_nodes.py's identical fixture for the
    full reasoning (real, slow SSH against conftest.py's fake mon IPs if
    left unmocked)."""
    monkeypatch.setattr(nodes_route, "list_osds", lambda: [])


def _login(client):
    # dashboard_client fixture (conftest.py) pins these credentials.
    client.post("/login", data={"username": "admin", "password": "admin"})


def _login_as(client, username, password):
    client.post("/login", data={"username": username, "password": password})


def _create_user(username, password, *, is_admin=False, is_active=True, created_by="admin"):
    with db_module.SessionLocal() as session:
        session.add(
            User(
                username=username,
                password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
                is_admin=is_admin,
                is_active=is_active,
                created_by=created_by,
            )
        )
        session.commit()


# --- Standalone /users page (2026-07-24: moved out of the Settings "Người
# dùng" card into its own top-nav item) ---------------------------------


def test_unauthenticated_get_users_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/users", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_users_page_for_admin(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/users")

    assert response.status_code == 200
    assert "Users" in response.text
    assert 'action="/users/create"' in response.text


def test_get_users_page_rejects_non_admin(dashboard_client):
    """Unlike the old Settings card (which just hid itself for a non-admin
    viewing the shared /settings page), the standalone page must reject a
    non-admin outright — a hidden nav link alone would not stop someone
    from typing the URL directly."""
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.get("/users")

    assert response.status_code == 403


def test_nav_shows_users_link_for_admin_on_other_pages(dashboard_client):
    _login(dashboard_client)

    for path in ("/", "/nodes", "/upgrade", "/settings"):
        response = dashboard_client.get(path)
        assert 'href="/users"' in response.text, f"missing Users nav link on {path}"


def test_nav_hides_users_link_for_non_admin_on_other_pages(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    for path in ("/", "/nodes", "/upgrade", "/settings"):
        response = dashboard_client.get(path)
        assert 'href="/users"' not in response.text, f"Users nav link leaked on {path}"


# --- Create user -------------------------------------------------------


def test_create_user_success_shows_in_list(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post(
        "/users/create",
        data={"new_username": "newbie", "new_password": "s3cret-pw", "new_password_confirm": "s3cret-pw"},
    )

    assert response.status_code == 200
    assert "Đã tạo user" in response.text
    assert "newbie" in response.text


def test_created_admin_user_can_manage_other_users(dashboard_client):
    _login(dashboard_client)
    dashboard_client.post(
        "/users/create",
        data={
            "new_username": "otheradmin",
            "new_password": "s3cret-pw",
            "new_password_confirm": "s3cret-pw",
            "new_is_admin": "on",
        },
    )
    dashboard_client.post("/logout")
    _login_as(dashboard_client, "otheradmin", "s3cret-pw")

    response = dashboard_client.get("/users")
    assert response.status_code == 200

    create_response = dashboard_client.post(
        "/users/create",
        data={"new_username": "created-by-other-admin", "new_password": "s3cret-pw2", "new_password_confirm": "s3cret-pw2"},
    )
    assert "Đã tạo user" in create_response.text


def test_create_user_rejects_duplicate_username(dashboard_client):
    _create_user("existing", "s3cret-pw")
    _login(dashboard_client)

    response = dashboard_client.post(
        "/users/create",
        data={"new_username": "existing", "new_password": "another-pw", "new_password_confirm": "another-pw"},
    )

    assert response.status_code == 200
    assert "đã tồn tại" in response.text


def test_create_user_rejects_reserved_env_username(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post(
        "/users/create",
        data={"new_username": "admin", "new_password": "another-pw", "new_password_confirm": "another-pw"},
    )

    assert response.status_code == 200
    assert "admin gốc" in response.text


def test_create_user_rejects_password_mismatch(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post(
        "/users/create",
        data={"new_username": "newbie", "new_password": "s3cret-pw", "new_password_confirm": "different"},
    )

    assert response.status_code == 200
    assert "không khớp" in response.text


def test_create_user_rejects_short_password(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post(
        "/users/create",
        data={"new_username": "newbie", "new_password": "short", "new_password_confirm": "short"},
    )

    assert response.status_code == 200
    assert "ít nhất" in response.text


def test_create_user_route_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post(
        "/users/create",
        data={"new_username": "newbie", "new_password": "s3cret-pw", "new_password_confirm": "s3cret-pw"},
    )

    assert response.status_code == 403


# --- Toggle active ------------------------------------------------------


def test_toggle_user_active_flips_state_and_blocks_login(dashboard_client):
    _login(dashboard_client)
    with db_module.SessionLocal() as session:
        target = User(
            username="togglable",
            password_hash=bcrypt.hashpw(b"s3cret-pw", bcrypt.gensalt()).decode(),
            is_admin=False,
            is_active=True,
            created_by="admin",
        )
        session.add(target)
        session.commit()
        target_id = target.id

    response = dashboard_client.post(f"/users/{target_id}/toggle-active")
    assert response.status_code == 200
    assert "Đã vô hiệu hoá" in response.text

    login_response = dashboard_client.post(
        "/login", data={"username": "togglable", "password": "s3cret-pw"}, follow_redirects=False
    )
    assert login_response.status_code == 401


def test_toggle_user_active_rejects_self_disable(dashboard_client):
    _create_user("otheradmin", "s3cret-pw", is_admin=True)
    _login_as(dashboard_client, "otheradmin", "s3cret-pw")
    with db_module.SessionLocal() as session:
        self_row = session.query(User).filter(User.username == "otheradmin").first()
        self_id = self_row.id

    response = dashboard_client.post(f"/users/{self_id}/toggle-active")

    assert response.status_code == 200
    assert "tự vô hiệu hoá" in response.text


def test_toggle_user_active_route_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    with db_module.SessionLocal() as session:
        target = User(
            username="other",
            password_hash=bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode(),
            is_admin=False,
            is_active=True,
            created_by="admin",
        )
        session.add(target)
        session.commit()
        target_id = target.id
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post(f"/users/{target_id}/toggle-active")

    assert response.status_code == 403


# --- Delete user ---------------------------------------------------------


def test_delete_user_removes_row_and_blocks_login(dashboard_client):
    _login(dashboard_client)
    _create_user("deletable", "s3cret-pw")
    with db_module.SessionLocal() as session:
        target_id = session.query(User).filter(User.username == "deletable").first().id

    response = dashboard_client.post(f"/users/{target_id}/delete")

    assert response.status_code == 200
    assert "Đã xoá user" in response.text
    with db_module.SessionLocal() as session:
        assert session.get(User, target_id) is None

    login_response = dashboard_client.post(
        "/login", data={"username": "deletable", "password": "s3cret-pw"}, follow_redirects=False
    )
    assert login_response.status_code == 401


def test_delete_user_rejects_self_delete(dashboard_client):
    _create_user("otheradmin", "s3cret-pw", is_admin=True)
    _login_as(dashboard_client, "otheradmin", "s3cret-pw")
    with db_module.SessionLocal() as session:
        self_id = session.query(User).filter(User.username == "otheradmin").first().id

    response = dashboard_client.post(f"/users/{self_id}/delete")

    assert response.status_code == 200
    assert "tự xoá" in response.text
    with db_module.SessionLocal() as session:
        assert session.get(User, self_id) is not None


def test_delete_user_missing_row_shows_error(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post("/users/does-not-exist/delete")

    assert response.status_code == 200
    assert "Không tìm thấy user" in response.text


def test_delete_user_route_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    with db_module.SessionLocal() as session:
        target = User(
            username="other2",
            password_hash=bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode(),
            is_admin=False,
            is_active=True,
            created_by="admin",
        )
        session.add(target)
        session.commit()
        target_id = target.id
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post(f"/users/{target_id}/delete")

    assert response.status_code == 403
    with db_module.SessionLocal() as session:
        assert session.get(User, target_id) is not None


def test_unauthenticated_delete_user_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/users/some-id/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
