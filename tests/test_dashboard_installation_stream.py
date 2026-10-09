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


_LAB_FSID = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"


def _staging_cluster(name="CS-STG"):
    from shared.models import Cluster

    with db.SessionLocal() as session:
        cluster = Cluster(name=name, is_default=False, is_active=True, ceph_mon_nodes="10.0.0.9",
                          ssh_user="root", ssh_key_path="/tmp/k")
        session.add(cluster)
        session.commit()
        return cluster.id


def _bootstrap(page: str, element_id: str):
    import json
    import re

    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', page, re.S)
    return json.loads(match.group(1))


def test_stream_has_no_staging_tab_until_one_is_configured(dashboard_client):
    _login(dashboard_client)

    assert _bootstrap(dashboard_client.get("/stream").text, "staging-topology-bootstrap") is None
    assert dashboard_client.get("/api/stream/staging-topology").status_code == 404


def test_stream_shows_the_staging_cluster_nodes_marked_for_failure_lab(dashboard_client):
    from shared import failure_lab_config

    staging_id = _staging_cluster()
    failure_lab_config.save("admin", cluster_id=staging_id, fsid=_LAB_FSID, fault_enabled=True, window="01:00-04:00")
    _login(dashboard_client)

    staging = _bootstrap(dashboard_client.get("/stream").text, "staging-topology-bootstrap")
    assert staging["cluster"] == {"id": staging_id, "name": "CS-STG"}
    assert staging["failure_lab"] == {"staging": True, "fsid_pinned": True, "fault_enabled": True,
                                      "window": "01:00-04:00"}
    refreshed = dashboard_client.get("/api/stream/staging-topology").json()
    assert refreshed["cluster"]["id"] == staging_id and refreshed["failure_lab"]["staging"] is True


def test_a_selected_staging_cluster_is_marked_in_its_own_tab(dashboard_client, default_cluster_id):
    from shared import failure_lab_config

    failure_lab_config.save("admin", cluster_id=default_cluster_id)
    _login(dashboard_client)

    page = dashboard_client.get(f"/stream?cluster={default_cluster_id}").text
    assert _bootstrap(page, "staging-topology-bootstrap") is None  # no second tab for the same cluster
    assert _bootstrap(page, "ceph-topology-bootstrap")["failure_lab"]["fsid_pinned"] is False


def test_staging_topology_api_is_admin_only(dashboard_client):
    with db.SessionLocal() as session:
        session.add(User(username="stream-viewer", password_hash=bcrypt.hashpw(b"viewer-secret", bcrypt.gensalt()).decode(),
                         is_admin=False, is_active=True, created_by="admin"))
        session.commit()
    _login(dashboard_client, "stream-viewer", "viewer-secret")

    assert dashboard_client.get("/api/stream/staging-topology").status_code == 403
