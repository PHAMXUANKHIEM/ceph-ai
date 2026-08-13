import bcrypt

from dashboard.routes.auth import is_admin_user
from shared import db as db_module
from shared.models import User


def _add_user(username, password, *, is_admin=False, is_active=True, created_by="admin"):
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


def test_unauthenticated_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_first_shows_product_selector(dashboard_client):
    response = dashboard_client.get("/login")

    assert response.status_code == 200
    assert "Chọn hệ thống bạn muốn quản trị" in response.text
    assert 'value="ceph"' in response.text
    assert 'value="vitastor"' in response.text


def test_select_ceph_then_shows_ceph_login(dashboard_client):
    response = dashboard_client.post(
        "/product/select", data={"product": "ceph"}, follow_redirects=True
    )

    assert response.status_code == 200
    assert "CEPH AIOPS PLATFORM" in response.text
    assert 'name="product" value="ceph"' in response.text


def test_select_vitastor_then_shows_independent_login(dashboard_client):
    response = dashboard_client.post(
        "/product/select", data={"product": "vitastor"}, follow_redirects=True
    )

    assert response.status_code == 200
    assert "VITASTOR CONTROL PLANE" in response.text
    assert "không gian quản trị Vitastor độc lập" in response.text
    assert 'name="product" value="vitastor"' in response.text


def test_vitastor_login_reaches_vitastor_only(dashboard_client):
    response = dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "admin", "product": "vitastor"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert response.url.path == "/vitastor"
    assert "Storage overview" in response.text

    ceph_page = dashboard_client.get("/", follow_redirects=False)
    assert ceph_page.status_code == 303
    assert ceph_page.headers["location"] == "/vitastor"


def test_ceph_login_cannot_open_vitastor_namespace(dashboard_client):
    dashboard_client.post(
        "/login", data={"username": "admin", "password": "admin", "product": "ceph"}
    )

    response = dashboard_client.get("/vitastor", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_wrong_password_is_rejected(dashboard_client):
    response = dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "wrong-password"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "sai" in response.text.lower() or "incorrect" in response.text.lower()


def test_wrong_username_is_rejected(dashboard_client):
    response = dashboard_client.post(
        "/login",
        data={"username": "not-admin", "password": "admin"},
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_correct_password_logs_in_and_reaches_index(dashboard_client):
    response = dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert response.url.path == "/"


def test_logout_invalidates_session(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    dashboard_client.post("/logout")

    response = dashboard_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_already_logged_in_get_login_redirects_to_index(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    response = dashboard_client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_overly_long_password_is_rejected_cleanly(dashboard_client):
    # bcrypt silently truncates input at 72 bytes — a password beyond that
    # must be rejected explicitly rather than relying on the truncated match.
    long_password = "a" * 100
    response = dashboard_client.post(
        "/login",
        data={"username": "admin", "password": long_password},
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_login_locks_out_after_repeated_failures(dashboard_client):
    for _ in range(5):
        dashboard_client.post(
            "/login",
            data={"username": "admin", "password": "wrong-password"},
            follow_redirects=False,
        )

    # even the CORRECT password must now be rejected while locked out
    response = dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )
    assert response.status_code == 429


def test_db_backed_user_can_log_in(dashboard_client):
    _add_user("alice", "s3cret-pw")

    response = dashboard_client.post(
        "/login",
        data={"username": "alice", "password": "s3cret-pw"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert response.url.path == "/"


def test_db_backed_user_wrong_password_is_rejected(dashboard_client):
    _add_user("alice", "s3cret-pw")

    response = dashboard_client.post(
        "/login",
        data={"username": "alice", "password": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_inactive_db_user_cannot_log_in_even_with_correct_password(dashboard_client):
    _add_user("bob", "s3cret-pw", is_active=False)

    response = dashboard_client.post(
        "/login",
        data={"username": "bob", "password": "s3cret-pw"},
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_is_admin_user_true_for_env_account(dashboard_client):
    assert is_admin_user("admin") is True


def test_is_admin_user_true_for_active_db_admin(dashboard_client):
    _add_user("alice", "s3cret-pw", is_admin=True)
    assert is_admin_user("alice") is True


def test_is_admin_user_false_for_db_non_admin(dashboard_client):
    _add_user("bob", "s3cret-pw", is_admin=False)
    assert is_admin_user("bob") is False


def test_is_admin_user_false_for_disabled_db_admin(dashboard_client):
    _add_user("carol", "s3cret-pw", is_admin=True, is_active=False)
    assert is_admin_user("carol") is False


def test_is_admin_user_false_for_unknown_username(dashboard_client):
    assert is_admin_user("nobody") is False
