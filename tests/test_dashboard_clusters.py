from datetime import datetime

import dashboard.routes.clusters as clusters_route
from shared import db as db_module
from shared.models import Action, AuditEntry, BackupAnomaly, BackupJob, Cluster, Incident, WatcherHeartbeat


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _cluster_form_data(**overrides):
    data = {
        "name": "cluster-b",
        "ceph_mon_nodes": "10.30.1.10,10.30.1.11",
        "ceph_container_name": "ceph-mon",
        "ssh_user": "root",
        "ssh_key_path": "/root/.ssh/ceph_aiops_watcher",
        "ceph_exec_mode": "docker",
    }
    data.update(overrides)
    return data


def _stub_restart_watcher(monkeypatch):
    """create_cluster()/toggle_cluster_active()/delete_cluster() call the
    REAL restart_watcher() (dashboard/routes/settings.py) on success — that
    spawns an actual `python -m watcher.main` OS subprocess via
    subprocess.Popen (see _start_process()), same as every other route
    that touches Worker/Watcher config. Every test below that reaches a
    successful create/toggle/delete MUST stub this out, same convention
    test_dashboard_settings.py already uses for restart_worker — verified
    live, 2026-08-10: forgetting this once actually spawned a real Watcher
    process mid test-run, inheriting pytest's own process env (test fixture
    MON IPs) into a real background process still running after the test
    suite exited.

    2026-08-11: create_cluster()/delete_cluster() (multi-tenant remediation
    Phase 3) ALSO call the real restart_worker() now — every existing
    caller of this helper below reaches create_cluster (either directly or
    to set up a second cluster before toggling/deleting it), so this stubs
    BOTH unconditionally rather than requiring every call site to remember
    a second helper call. Verified live, same session: running this file's
    full suite without this repeatedly restarted the REAL production
    Worker on this box (rapid "Scheduler started" bursts in
    /var/log/ceph-aiops-worker.log, one per unstubbed create/toggle) —
    disruptive (backup scheduler momentarily down each time) even though
    it self-healed back to exactly one live process afterward."""
    monkeypatch.setattr(clusters_route, "restart_watcher", lambda: {"restarted": True, "new_pid": 1, "error": None})
    monkeypatch.setattr(clusters_route, "restart_worker", lambda: {"restarted": True, "new_pid": 1, "error": None})


def test_unauthenticated_get_clusters_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/clusters", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_clusters_page_shows_default_cluster(dashboard_client, default_cluster_id):
    _login(dashboard_client)
    response = dashboard_client.get("/clusters")

    assert response.status_code == 200
    assert "mặc định" in response.text


def test_create_cluster_tests_connection_before_saving(dashboard_client, monkeypatch):
    monkeypatch.setattr(clusters_route, "query_cluster_health_with", lambda *a, **kw: {"status": "HEALTH_OK"})
    _stub_restart_watcher(monkeypatch)

    _login(dashboard_client)
    response = dashboard_client.post("/clusters/create", data=_cluster_form_data())

    assert response.status_code == 200
    assert "Đã thêm cụm" in response.text
    with db_module.SessionLocal() as session:
        created = session.query(Cluster).filter_by(name="cluster-b").one()
        assert created.is_default is False
        assert created.is_active is True
        assert created.ceph_mon_nodes == "10.30.1.10,10.30.1.11"


def test_create_cluster_never_touches_the_default_clusters_sticky_mon_node(dashboard_client, monkeypatch):
    """Regression guard: watcher/ceph_client.py::query_cluster_health_with's
    update_sticky_fallback must be False for this call site (see clusters.py's
    own comment) — a successful test-connection here must never overwrite
    the DEFAULT cluster's sticky MON node used by its own log collection."""
    from watcher import ceph_client

    ceph_client.last_successful_mon_node = "10.20.1.150"  # the default cluster's real sticky value

    def fake_query(mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode, update_sticky_fallback=True):
        assert update_sticky_fallback is False
        return {"status": "HEALTH_OK"}

    monkeypatch.setattr(clusters_route, "query_cluster_health_with", fake_query)
    _stub_restart_watcher(monkeypatch)

    _login(dashboard_client)
    dashboard_client.post("/clusters/create", data=_cluster_form_data())

    assert ceph_client.last_successful_mon_node == "10.20.1.150"  # unchanged


def test_create_cluster_connection_failure_does_not_save(dashboard_client, monkeypatch):
    from watcher.ceph_client import CephQueryError

    def failing_query(*a, **kw):
        raise CephQueryError("no MON node reachable")

    monkeypatch.setattr(clusters_route, "query_cluster_health_with", failing_query)

    _login(dashboard_client)
    response = dashboard_client.post("/clusters/create", data=_cluster_form_data())

    assert response.status_code == 200
    assert "Không kết nối được tới cụm" in response.text
    with db_module.SessionLocal() as session:
        assert session.query(Cluster).filter_by(name="cluster-b").first() is None


def test_create_cluster_missing_required_field_skips_connection_test(dashboard_client, monkeypatch):
    called = []
    monkeypatch.setattr(clusters_route, "query_cluster_health_with", lambda *a, **kw: called.append(1))

    _login(dashboard_client)
    response = dashboard_client.post("/clusters/create", data=_cluster_form_data(ceph_mon_nodes=""))

    assert response.status_code == 200
    assert called == []


def test_toggle_active_flips_an_additional_cluster(dashboard_client, monkeypatch):
    monkeypatch.setattr(clusters_route, "query_cluster_health_with", lambda *a, **kw: {"status": "HEALTH_OK"})
    _stub_restart_watcher(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/clusters/create", data=_cluster_form_data())
    with db_module.SessionLocal() as session:
        cluster_id = session.query(Cluster).filter_by(name="cluster-b").one().id

    response = dashboard_client.post(f"/clusters/{cluster_id}/toggle-active")

    assert response.status_code == 200
    with db_module.SessionLocal() as session:
        assert session.get(Cluster, cluster_id).is_active is False


def test_backup_config_saves_fields_and_restarts_worker_only(dashboard_client, monkeypatch):
    """Multi-tenant remediation Phase 3 — POST /clusters/{id}/backup-config
    persists the submitted fields and restarts ONLY Worker (its scheduler
    is what actually reads this config), never Watcher."""
    monkeypatch.setattr(clusters_route, "query_cluster_health_with", lambda *a, **kw: {"status": "HEALTH_OK"})
    _stub_restart_watcher(monkeypatch)
    watcher_restarts = []
    worker_restarts = []
    monkeypatch.setattr(
        clusters_route, "restart_watcher", lambda: watcher_restarts.append(1) or {"restarted": True, "new_pid": 1, "error": None}
    )
    monkeypatch.setattr(
        clusters_route, "restart_worker", lambda: worker_restarts.append(1) or {"restarted": True, "new_pid": 1, "error": None}
    )
    _login(dashboard_client)
    dashboard_client.post("/clusters/create", data=_cluster_form_data())
    with db_module.SessionLocal() as session:
        cluster_id = session.query(Cluster).filter_by(name="cluster-b").one().id
    watcher_restarts.clear()
    worker_restarts.clear()

    response = dashboard_client.post(
        f"/clusters/{cluster_id}/backup-config",
        data={
            "backup_enabled": "true",
            "backup_tracked_images": "rbd/vm1, rbd/vm2",
            "backup_full_refresh_days": "14",
            "backup_transport": "s3",
            "backup_s3_endpoint": "https://s3.example.test",
            "backup_s3_access_key": "AKIA_TEST",
            "backup_s3_secret_key": "shh",
            "backup_s3_bucket": "cluster-b-backups",
            "backup_immutable_enabled": "true",
            "backup_immutable_lock_days": "10",
        },
    )

    assert response.status_code == 200
    assert "Đã lưu cấu hình backup" in response.text
    assert worker_restarts == [1]
    assert watcher_restarts == []  # backup config must never restart Watcher
    with db_module.SessionLocal() as session:
        saved = session.get(Cluster, cluster_id)
        assert saved.backup_enabled is True
        assert saved.backup_tracked_images == "rbd/vm1, rbd/vm2"
        assert saved.backup_full_refresh_days == 14
        assert saved.backup_transport == "s3"
        assert saved.backup_s3_access_key == "AKIA_TEST"
        assert saved.backup_s3_secret_key == "shh"
        assert saved.backup_s3_bucket == "cluster-b-backups"
        assert saved.backup_immutable_enabled is True
        assert saved.backup_immutable_lock_days == 10


def test_backup_config_blank_secret_keeps_previous_value(dashboard_client, monkeypatch):
    """Secret-shaped fields (backup_s3_secret_key etc.) are write-only in
    the template — submitting blank must NOT clear a previously-saved
    secret."""
    monkeypatch.setattr(clusters_route, "query_cluster_health_with", lambda *a, **kw: {"status": "HEALTH_OK"})
    _stub_restart_watcher(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/clusters/create", data=_cluster_form_data())
    with db_module.SessionLocal() as session:
        cluster_id = session.query(Cluster).filter_by(name="cluster-b").one().id

    dashboard_client.post(
        f"/clusters/{cluster_id}/backup-config",
        data={
            "backup_enabled": "true",
            "backup_transport": "s3",
            "backup_s3_secret_key": "original-secret",
            "backup_s3_bucket": "bucket-1",
        },
    )
    response = dashboard_client.post(
        f"/clusters/{cluster_id}/backup-config",
        data={
            "backup_enabled": "true",
            "backup_transport": "s3",
            "backup_s3_secret_key": "",  # left blank on this second save
            "backup_s3_bucket": "bucket-2",  # non-secret field DOES get overwritten
        },
    )

    assert response.status_code == 200
    with db_module.SessionLocal() as session:
        saved = session.get(Cluster, cluster_id)
        assert saved.backup_s3_secret_key == "original-secret"
        assert saved.backup_s3_bucket == "bucket-2"


def test_backup_config_enabled_without_transport_is_rejected(dashboard_client, monkeypatch):
    _stub_restart_watcher(monkeypatch)
    monkeypatch.setattr(clusters_route, "query_cluster_health_with", lambda *a, **kw: {"status": "HEALTH_OK"})
    _login(dashboard_client)
    dashboard_client.post("/clusters/create", data=_cluster_form_data())
    with db_module.SessionLocal() as session:
        cluster_id = session.query(Cluster).filter_by(name="cluster-b").one().id

    response = dashboard_client.post(
        f"/clusters/{cluster_id}/backup-config", data={"backup_enabled": "true", "backup_transport": ""}
    )

    assert response.status_code == 200
    assert "cần chọn nơi lưu" in response.text
    with db_module.SessionLocal() as session:
        assert session.get(Cluster, cluster_id).backup_enabled is False


def test_backup_config_rejects_the_default_cluster(dashboard_client, default_cluster_id, monkeypatch):
    _login(dashboard_client)
    response = dashboard_client.post(
        f"/clusters/{default_cluster_id}/backup-config", data={"backup_enabled": "true", "backup_transport": "s3"}
    )

    assert response.status_code == 200
    assert "cụm mặc định" in response.text


def test_toggle_active_rejects_the_default_cluster(dashboard_client, default_cluster_id):
    _login(dashboard_client)
    response = dashboard_client.post(f"/clusters/{default_cluster_id}/toggle-active")

    assert response.status_code == 200
    assert "Không thể vô hiệu hoá cụm mặc định" in response.text
    with db_module.SessionLocal() as session:
        assert session.get(Cluster, default_cluster_id).is_active is True


# --- Hard-delete an additional cluster --------------------------------------


def _seed_cluster_with_full_data(cluster_id: str) -> None:
    """Writes one row into every table that hangs off `cluster_id` via a
    real or FK chain — Incident -> Action -> AuditEntry, and
    BackupJob -> BackupAnomaly — plus a WatcherHeartbeat row, so
    delete_cluster()'s purge can be asserted against every table it
    touches, not just Incident."""
    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code="SEED_ISSUE",
            status="NEW",
            detected_at=datetime.utcnow(),
            cluster_id=cluster_id,
        )
        session.add(incident)
        session.flush()

        action = Action(
            incident_id=incident.id,
            action_id="restart_osd_daemon",
            classification="SAFE",
            status="PENDING",
        )
        session.add(action)
        session.flush()

        session.add(AuditEntry(incident_id=incident.id, action_id=action.id, event_type="seed", actor="test"))

        backup_job = BackupJob(
            cluster_id=cluster_id,
            run_id="seed-run",
            pool="rbd",
            image="seed-image",
            job_type="full",
            status="SUCCESS",
        )
        session.add(backup_job)
        session.flush()

        session.add(BackupAnomaly(backup_job_id=backup_job.id, kind="duration", severity="warning"))
        session.add(WatcherHeartbeat(cluster_id=cluster_id, success=True, polled_at=datetime.utcnow()))
        session.commit()


def test_delete_cluster_rejects_the_default_cluster(dashboard_client, default_cluster_id):
    _login(dashboard_client)
    response = dashboard_client.post(f"/clusters/{default_cluster_id}/delete")

    assert response.status_code == 200
    assert "Không thể xoá cụm mặc định" in response.text
    with db_module.SessionLocal() as session:
        assert session.get(Cluster, default_cluster_id) is not None


def test_delete_cluster_unknown_id_shows_error(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post("/clusters/does-not-exist/delete")

    assert response.status_code == 200
    assert "Không tìm thấy cụm" in response.text


def test_delete_cluster_purges_only_that_clusters_own_data(dashboard_client, monkeypatch, default_cluster_id):
    """The core AC the user asked for: deleting cluster B must wipe every
    Incident/Action/AuditEntry/BackupJob/BackupAnomaly/WatcherHeartbeat row
    scoped to B, and leave cluster A's (and the default cluster's) own
    rows completely untouched."""
    monkeypatch.setattr(clusters_route, "query_cluster_health_with", lambda *a, **kw: {"status": "HEALTH_OK"})
    _stub_restart_watcher(monkeypatch)
    _login(dashboard_client)

    dashboard_client.post("/clusters/create", data=_cluster_form_data(name="cluster-a"))
    dashboard_client.post("/clusters/create", data=_cluster_form_data(name="cluster-b"))
    with db_module.SessionLocal() as session:
        cluster_a_id = session.query(Cluster).filter_by(name="cluster-a").one().id
        cluster_b_id = session.query(Cluster).filter_by(name="cluster-b").one().id

    _seed_cluster_with_full_data(cluster_a_id)
    _seed_cluster_with_full_data(cluster_b_id)

    response = dashboard_client.post(f"/clusters/{cluster_b_id}/delete")

    assert response.status_code == 200
    assert "Đã xoá cụm" in response.text
    assert "cluster-b" in response.text
    with db_module.SessionLocal() as session:
        # Cluster B itself, and every row scoped to it, is gone.
        assert session.get(Cluster, cluster_b_id) is None
        assert session.query(Incident).filter_by(cluster_id=cluster_b_id).count() == 0
        assert session.query(BackupJob).filter_by(cluster_id=cluster_b_id).count() == 0
        assert session.query(WatcherHeartbeat).filter_by(cluster_id=cluster_b_id).count() == 0
        assert (
            session.query(Action)
            .join(Incident, Action.incident_id == Incident.id)
            .filter(Incident.cluster_id == cluster_b_id)
            .count()
            == 0
        )
        assert session.query(BackupAnomaly).count() == 1  # only cluster A's own row survives

        # Cluster A (a different additional cluster) is completely untouched.
        assert session.get(Cluster, cluster_a_id) is not None
        assert session.query(Incident).filter_by(cluster_id=cluster_a_id).count() == 1
        assert session.query(BackupJob).filter_by(cluster_id=cluster_a_id).count() == 1
        assert session.query(WatcherHeartbeat).filter_by(cluster_id=cluster_a_id).count() == 1
        assert (
            session.query(Action)
            .join(Incident, Action.incident_id == Incident.id)
            .filter(Incident.cluster_id == cluster_a_id)
            .count()
            == 1
        )

        # The default cluster's own (pre-existing) data is unaffected too.
        assert session.get(Cluster, default_cluster_id) is not None


# --- Cluster switcher on the main Dashboard page ----------------------------


def test_index_filters_incidents_by_selected_cluster(dashboard_client, monkeypatch, default_cluster_id):
    monkeypatch.setattr(clusters_route, "query_cluster_health_with", lambda *a, **kw: {"status": "HEALTH_OK"})
    _stub_restart_watcher(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/clusters/create", data=_cluster_form_data())
    with db_module.SessionLocal() as session:
        other_cluster_id = session.query(Cluster).filter_by(name="cluster-b").one().id
        session.add(
            Incident(
                ceph_code="DEFAULT_CLUSTER_ISSUE",
                status="NEW",
                detected_at=datetime.utcnow(),
                cluster_id=default_cluster_id,
            )
        )
        session.add(
            Incident(
                ceph_code="OTHER_CLUSTER_ISSUE",
                status="NEW",
                detected_at=datetime.utcnow(),
                cluster_id=other_cluster_id,
            )
        )
        session.commit()

    default_view = dashboard_client.get("/")
    other_view = dashboard_client.get(f"/?cluster={other_cluster_id}")

    assert "DEFAULT_CLUSTER_ISSUE" in default_view.text
    assert "OTHER_CLUSTER_ISSUE" not in default_view.text
    assert "OTHER_CLUSTER_ISSUE" in other_view.text
    assert "DEFAULT_CLUSTER_ISSUE" not in other_view.text


def test_index_falls_back_to_default_cluster_for_unknown_cluster_query_param(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/?cluster=does-not-exist")

    assert response.status_code == 200  # no 404/500 on a stale/bad bookmark


# 2026-08-11: user-reported bug — picking a non-default cluster on the
# Dashboard, then navigating to another page via the nav bar (which never
# forwards `?cluster=`) and back to "Dashboard" (a plain `href="/"`, also no
# `?cluster=`) silently landed back on the DEFAULT cluster, even though the
# switcher still looked selected on the other cluster. `?cluster=` alone was
# never persisted anywhere — see _resolve_selected_cluster's docstring
# (dashboard/routes/incidents.py) for the session-backed fix.
def test_selected_cluster_persists_across_navigation_without_query_param(
    dashboard_client, monkeypatch, default_cluster_id
):
    monkeypatch.setattr(clusters_route, "query_cluster_health_with", lambda *a, **kw: {"status": "HEALTH_OK"})
    _stub_restart_watcher(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/clusters/create", data=_cluster_form_data())
    with db_module.SessionLocal() as session:
        other_cluster_id = session.query(Cluster).filter_by(name="cluster-b").one().id
        session.add(
            Incident(
                ceph_code="DEFAULT_CLUSTER_ISSUE",
                status="NEW",
                detected_at=datetime.utcnow(),
                cluster_id=default_cluster_id,
            )
        )
        session.add(
            Incident(
                ceph_code="OTHER_CLUSTER_ISSUE",
                status="NEW",
                detected_at=datetime.utcnow(),
                cluster_id=other_cluster_id,
            )
        )
        session.commit()

    # Pick the other cluster explicitly once (same as clicking the switcher).
    dashboard_client.get(f"/?cluster={other_cluster_id}")
    # Simulate leaving via the nav bar to a page that has no cluster concept
    # at all, then clicking "Dashboard" — both are plain links, neither
    # carries `?cluster=`.
    dashboard_client.get("/volumes")
    back_on_dashboard = dashboard_client.get("/")

    assert "OTHER_CLUSTER_ISSUE" in back_on_dashboard.text
    assert "DEFAULT_CLUSTER_ISSUE" not in back_on_dashboard.text


# 2026-08-10: user-reported bug — the Clusters nav link was missing from
# every template EXCEPT clusters.html/crush_map.html/index.html, because
# this app has no shared nav partial (every template hand-copies its own
# topbar, same root cause test_dashboard_users.py's own
# test_nav_shows_users_link_for_admin_on_other_pages already documents for
# the Users link). Covers every admin-nav page that got the fix, not just
# a small sample, since the actual bug spanned all of them.
def test_nav_shows_clusters_link_for_admin_on_every_page(dashboard_client):
    _login(dashboard_client)

    for path in (
        "/",
        "/nodes",
        "/volumes",
        "/deploy-cluster",
        "/delete-cluster",
        "/convert-cluster",
        "/upgrade",
        "/patch",
        "/backups",
        "/restore-cluster",
        "/bucket-access-log",
        "/settings",
        "/telegram-alerts",
        "/telegram-alerts/help",
        "/users",
        "/crush-map",
        "/clusters",
    ):
        response = dashboard_client.get(path)
        assert 'href="/clusters"' in response.text, f"missing Clusters nav link on {path}"


def test_nav_hides_clusters_link_for_non_admin_on_other_pages(dashboard_client):
    from tests.test_dashboard_users import _create_user, _login_as

    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    for path in ("/", "/nodes", "/upgrade", "/settings"):
        response = dashboard_client.get(path)
        assert 'href="/clusters"' not in response.text, f"Clusters nav link leaked on {path}"
