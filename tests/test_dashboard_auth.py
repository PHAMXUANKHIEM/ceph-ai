def test_unauthenticated_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


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
