import bcrypt

from shared import db
from shared.models import User


def _login(client, username="admin", password="admin"):
    client.post("/login", data={"username": username, "password": password})


def test_installation_stream_redirects_anonymous_user_to_login(dashboard_client):
    response = dashboard_client.get("/stream", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_installation_stream_is_admin_only(dashboard_client):
    with db.SessionLocal() as session:
        session.add(
            User(
                username="stream-viewer",
                password_hash=bcrypt.hashpw(b"viewer-secret", bcrypt.gensalt()).decode(),
                is_admin=False,
                is_active=True,
                created_by="admin",
            )
        )
        session.commit()
    _login(dashboard_client, "stream-viewer", "viewer-secret")

    response = dashboard_client.get("/stream")

    assert response.status_code == 403


def test_installation_stream_renders_current_profile_and_nav_for_admin(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/stream")

    assert response.status_code == 200
    assert 'id="installation-stream-root"' in response.text
    assert 'id="installation-stream-bootstrap"' in response.text
    assert '<div class="app-body">' in response.text
    assert 'src="/static/app.js?' in response.text
    assert 'href="/stream"' in response.text
    assert "Monitoring &amp; Metrics" in response.text
    assert "secrets_included" in response.text
