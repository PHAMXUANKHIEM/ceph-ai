import json
from datetime import datetime, timedelta

import dashboard.routes.backups as backups_route
from shared import db as db_module
from shared.models import Action, ActionStatus, BackupAnomaly, BackupDigestLog, BackupJob, Incident, IncidentStatus


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _stub_tracked_images(monkeypatch, tracked):
    monkeypatch.setattr(
        backups_route,
        "load_backup_policy",
        lambda: {"tracked_images": tracked},
    )


def _create_running_action(session, action_id, progress):
    incident = Incident(
        ceph_code="BACKUP_JOB",
        status=IncidentStatus.PENDING_APPROVAL.value,
        detected_at=datetime.utcnow(),
    )
    session.add(incident)
    session.flush()
    action = Action(
        incident_id=incident.id,
        action_id=action_id,
        classification="SAFE",
        status=ActionStatus.APPROVED.value,
        target_nodes=json.dumps(["10.20.1.112"]),
        execution_progress=json.dumps(progress),
    )
    session.add(action)
    session.commit()
    return action.id


def test_unauthenticated_get_backups_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/backups", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_backups_page_renders_with_no_tracked_images(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [])
    _login(dashboard_client)

    response = dashboard_client.get("/backups")

    assert response.status_code == 200
    assert "Chưa cấu hình" in response.text


def test_backups_page_lists_queue_and_history(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    _login(dashboard_client)

    with db_module.SessionLocal() as session:
        session.add(
            BackupJob(
                run_id="run-1",
                pool="vms",
                image="disk1",
                job_type="full",
                status="SUCCESS",
                duration_seconds=12.5,
                size_bytes=1024,
                created_at=datetime.utcnow() - timedelta(hours=1),
                finished_at=datetime.utcnow(),
            )
        )
        session.commit()

    response = dashboard_client.get("/backups")

    assert response.status_code == 200
    assert "vms/disk1" in response.text
    assert "SUCCESS" in response.text


def test_backups_page_renders_with_no_digests_or_anomalies(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [])
    _login(dashboard_client)

    response = dashboard_client.get("/backups")

    assert response.status_code == 200
    assert "Chưa phát hiện bất thường nào." in response.text
    assert "Chưa có digest nào được tạo." in response.text


def test_backups_page_lists_anomalies(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [])
    _login(dashboard_client)

    with db_module.SessionLocal() as session:
        job = BackupJob(
            run_id="run-2",
            pool="vms",
            image="disk1",
            job_type="full",
            status="SUCCESS",
            duration_seconds=999.0,
            size_bytes=1024,
            created_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
        session.add(job)
        session.flush()
        session.add(
            BackupAnomaly(
                backup_job_id=job.id,
                kind="duration",
                severity="critical",
                ai_summary="Job chạy lâu bất thường so với lịch sử.",
                created_at=datetime.utcnow(),
            )
        )
        session.commit()

    response = dashboard_client.get("/backups")

    assert response.status_code == 200
    assert "vms/disk1" in response.text
    assert "Job chạy lâu bất thường so với lịch sử." in response.text
    assert "severity-err" in response.text


def test_backups_page_lists_digests(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [])
    _login(dashboard_client)

    with db_module.SessionLocal() as session:
        session.add(
            BackupDigestLog(
                period_start=datetime.utcnow() - timedelta(hours=24),
                period_end=datetime.utcnow(),
                succeeded_count=5,
                failed_count=1,
                anomaly_count=2,
                summary_text="Trong 24h qua: 5 job thành công, 1 job thất bại.",
                created_at=datetime.utcnow(),
            )
        )
        session.commit()

    response = dashboard_client.get("/backups")

    assert response.status_code == 200
    assert "Trong 24h qua: 5 job thành công, 1 job thất bại." in response.text


def test_progress_api_no_running_action_returns_null(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/api/backups/progress")
    assert response.status_code == 200
    assert response.json() == {"action_id": None, "status": None, "progress": []}


def test_progress_api_returns_latest_running_rbd_backup(dashboard_client):
    _login(dashboard_client)
    progress = [
        {
            "pct": 42,
            "bytes_transferred": 420,
            "total_bytes": 1000,
            "speed_mbps": 3.5,
            "eta_seconds": 90,
        }
    ]
    with db_module.SessionLocal() as session:
        _create_running_action(session, "rbd_backup_run", progress)

    response = dashboard_client.get("/api/backups/progress")

    assert response.status_code == 200
    body = response.json()
    assert body["action_id"] == "rbd_backup_run"
    assert body["status"] == "APPROVED"
    assert body["progress"][0]["pct"] == 42
    assert body["progress"][0]["speed_mbps"] == 3.5


def test_progress_api_formats_step_timestamps_as_vietnam_local_clock(dashboard_client):
    _login(dashboard_client)
    progress = [
        {
            "step": "restore_drill",
            "status": "running",
            "started_at": "2026-07-29T03:29:30",
        }
    ]
    with db_module.SessionLocal() as session:
        _create_running_action(session, "restore_drill_execute", progress)

    response = dashboard_client.get("/api/backups/progress")

    body = response.json()
    assert body["progress"][0]["started_at_display"] == "10:29:30"
    assert body["progress"][0]["finished_at_display"] is None


def test_progress_api_ignores_non_in_flight_actions(dashboard_client):
    _login(dashboard_client)
    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code="BACKUP_JOB",
            status=IncidentStatus.PENDING_APPROVAL.value,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id="rbd_backup_run",
            classification="SAFE",
            status=ActionStatus.EXECUTED.value,
            target_nodes=json.dumps(["10.20.1.112"]),
            execution_progress=json.dumps([{"pct": 100}]),
        )
        session.add(action)
        session.commit()

    response = dashboard_client.get("/api/backups/progress")

    assert response.json() == {"action_id": None, "status": None, "progress": []}


# --- Story 9.7 Task 3 UI: restore_rbd_image_to_production propose route ---


def test_restore_propose_rejects_image_not_in_tracked_images(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    monkeypatch.setattr(backups_route.settings, "ceph_mon_nodes", "10.20.1.112", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post("/backups/restore/propose", json={"pool": "vms", "image": "not-tracked"})

    assert response.status_code == 400
    assert "tracked_images" in response.json()["detail"]


def test_restore_propose_creates_pending_risky_action(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    monkeypatch.setattr(backups_route.settings, "ceph_mon_nodes", "10.20.1.112", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post("/backups/restore/propose", json={"pool": "vms", "image": "disk1"})

    assert response.status_code == 201
    action_id = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.action_id == "restore_rbd_image_to_production"
        assert action.classification == "RISKY"
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert json.loads(action.action_params) == {"pool": "vms", "image": "disk1"}
        assert json.loads(action.target_nodes) == ["10.20.1.112"]


def test_restore_propose_rejects_second_proposal_while_one_pending(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}, {"pool": "vms", "image": "disk2"}])
    monkeypatch.setattr(backups_route.settings, "ceph_mon_nodes", "10.20.1.112", raising=False)
    _login(dashboard_client)

    first = dashboard_client.post("/backups/restore/propose", json={"pool": "vms", "image": "disk1"})
    assert first.status_code == 201

    second = dashboard_client.post("/backups/restore/propose", json={"pool": "vms", "image": "disk2"})
    assert second.status_code == 409


def test_restore_propose_requires_ceph_mon_nodes_configured(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    monkeypatch.setattr(backups_route.settings, "ceph_mon_nodes", "", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post("/backups/restore/propose", json={"pool": "vms", "image": "disk1"})

    assert response.status_code == 400


def test_backups_page_shows_pending_restore_action_with_approve_reject(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    monkeypatch.setattr(backups_route.settings, "ceph_mon_nodes", "10.20.1.112", raising=False)
    _login(dashboard_client)

    propose = dashboard_client.post("/backups/restore/propose", json={"pool": "vms", "image": "disk1"})
    action_id = propose.json()["action_id"]

    response = dashboard_client.get("/backups")

    assert response.status_code == 200
    assert f"/actions/{action_id}/approve" in response.text
    assert f"/actions/{action_id}/reject" in response.text
    # The restore button for other tracked images is disabled while one is pending.
    assert "btn-restore-image" in response.text
