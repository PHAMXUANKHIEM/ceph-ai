import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import dashboard.routes.backups as backups_route
from shared import db as db_module
from shared.models import (
    Action,
    ActionStatus,
    AuditEntry,
    BackupAnomaly,
    BackupDigestLog,
    BackupJob,
    Cluster,
    Incident,
    IncidentStatus,
)


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _stub_tracked_images(monkeypatch, tracked):
    monkeypatch.setattr(
        backups_route,
        "load_backup_policy",
        lambda: {"tracked_images": tracked},
    )


def _create_running_action(session, action_id, progress, cluster_id=None):
    incident = Incident(
        cluster_id=cluster_id,
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


def _create_additional_cluster(session, *, name="backup-secondary"):
    cluster = Cluster(
        name=name,
        ceph_mon_nodes="10.20.2.112",
        ssh_user="root",
        ssh_key_path="/tmp/test-key",
        backup_enabled=True,
        backup_tracked_images="vms/disk1",
    )
    session.add(cluster)
    session.commit()
    session.refresh(cluster)
    session.expunge(cluster)
    return cluster


def _seed_successful_full(pool="vms", image="disk1"):
    with db_module.SessionLocal() as session:
        job = BackupJob(
                run_id=f"full-{pool}-{image}",
                pool=pool,
                image=image,
                job_type="full",
                status="SUCCESS",
                backup_target_slot="a",
                remote_key=f"{pool}/{image}/full.bin",
                created_at=datetime.utcnow(),
                finished_at=datetime.utcnow(),
            )
        session.add(job)
        session.commit()
        return job.id


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


def test_backups_page_summarizes_rpo_states_and_latest_drill(dashboard_client, monkeypatch):
    tracked = [{"pool": "vms", "image": name} for name in ("healthy", "risk", "breach", "never")]
    monkeypatch.setattr(backups_route, "load_backup_policy",
                        lambda: {"tracked_images": tracked, "rpo_hours": 24})
    now = datetime.utcnow()
    with db_module.SessionLocal() as session:
        for name, age in (("healthy", 2), ("risk", 19), ("breach", 25)):
            session.add(BackupJob(run_id=f"run-{name}", pool="vms", image=name,
                job_type="full", status="SUCCESS", created_at=now - timedelta(hours=age),
                finished_at=now - timedelta(hours=age)))
        session.add(BackupJob(run_id="drill", pool="vms", image="healthy",
            job_type="restore_drill", status="SUCCESS", duration_seconds=42.5,
            created_at=now, finished_at=now))
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.get("/backups")

    assert response.status_code == 200
    assert "Healthy" in response.text
    assert "RPO at risk" in response.text
    assert "RPO breached" in response.text
    assert "Never backed up" in response.text
    assert "42.5 giây" in response.text
    assert "Mục tiêu RPO: 24 giờ" in response.text


def test_protection_overview_reports_metadata_and_drill_freshness(dashboard_client, monkeypatch):
    now = datetime.utcnow()
    monkeypatch.setattr(backups_route, "load_backup_policy", lambda: {
        "tracked_images": [], "metadata_rpo_hours": 12, "restore_drill_rpo_hours": 192,
        "restore_drill": {"pool": "vms", "image": "web01",
                          "scratch_pool": "scratch", "scratch_image": "drill01"},
    })
    with db_module.SessionLocal() as session:
        session.add_all([
            BackupJob(run_id="meta", job_type="metadata", status="SUCCESS",
                      created_at=now - timedelta(hours=13)),
            BackupJob(run_id="drill", pool="vms", image="web01", job_type="restore_drill",
                      status="FAILED", created_at=now - timedelta(hours=1)),
        ])
        session.commit()

    overview = backups_route._protection_overview([], now=now)

    assert overview["metadata"]["status"] == "overdue"
    assert overview["metadata"]["threshold_hours"] == 12
    assert overview["drill_freshness"]["status"] == "failed"
    assert overview["drill_freshness"]["threshold_hours"] == 192


def test_protection_overview_estimates_rto_from_chain_and_drill_throughput(
    dashboard_client, monkeypatch
):
    now = datetime.utcnow()
    monkeypatch.setattr(backups_route, "load_backup_policy",
                        lambda: {"tracked_images": [], "rpo_hours": 24})
    with db_module.SessionLocal() as session:
        full = BackupJob(run_id="full", pool="vms", image="disk1", job_type="full",
                         status="SUCCESS", backup_target_slot="a", size_bytes=1000,
                         created_at=now - timedelta(hours=2))
        session.add(full)
        session.flush()
        session.add_all([
            BackupJob(run_id="inc", pool="vms", image="disk1", job_type="incremental",
                      status="SUCCESS", backup_target_slot="a", base_job_id=full.id,
                      size_bytes=500, created_at=now - timedelta(hours=1)),
            BackupJob(run_id="drill", pool="vms", image="canary", job_type="restore_drill",
                      status="SUCCESS", size_bytes=1000, duration_seconds=10,
                      created_at=now),
        ])
        session.commit()

    overview = backups_route._protection_overview(
        [{"pool": "vms", "image": "disk1"}], now=now
    )

    assert overview["restore_bytes_per_second"] == 100
    assert overview["rows"][0]["chain_size_bytes"] == 1500
    assert overview["rows"][0]["estimated_rto_seconds"] == 15
    assert overview["estimated_rto_seconds"] == 15


def test_protection_overview_reports_copy_compliance_per_recovery_point(
    dashboard_client, monkeypatch
):
    now = datetime.utcnow()
    tracked = [{"pool": "vms", "image": name} for name in ("ok", "degraded", "missing")]
    monkeypatch.setattr(backups_route, "load_backup_policy", lambda: {
        "tracked_images": tracked,
        "backup_targets": [{"slot": "a"}, {"slot": "b"}],
        "required_copy_count": 2,
    })
    with db_module.SessionLocal() as session:
        session.add_all([
            BackupJob(run_id="ok-run", pool="vms", image="ok", job_type="full",
                      status="SUCCESS", backup_target_slot="a", created_at=now),
            BackupJob(run_id="ok-run", pool="vms", image="ok", job_type="full",
                      status="SUCCESS", backup_target_slot="b", created_at=now),
            BackupJob(run_id="degraded-run", pool="vms", image="degraded", job_type="full",
                      status="SUCCESS", backup_target_slot="a", created_at=now),
        ])
        session.commit()

    overview = backups_route._protection_overview(tracked, now=now)
    rows = {row["image"]: row for row in overview["rows"]}

    assert rows["ok"]["copy_status"] == "compliant"
    assert rows["ok"]["successful_copies"] == 2
    assert rows["degraded"]["copy_status"] == "degraded"
    assert rows["degraded"]["successful_copies"] == 1
    assert rows["missing"]["copy_status"] == "missing"
    assert overview["copy_counts"] == {"compliant": 1, "degraded": 1, "missing": 1}


def test_protection_overview_uses_workload_rpo_override(dashboard_client, monkeypatch):
    now = datetime.utcnow()
    tracked = [{"pool": "vms", "image": "slow", "rpo_hours": 48}]
    monkeypatch.setattr(backups_route, "load_backup_policy",
                        lambda: {"tracked_images": tracked, "rpo_hours": 24})
    with db_module.SessionLocal() as session:
        session.add(BackupJob(run_id="slow-run", pool="vms", image="slow", job_type="full",
                              status="SUCCESS", backup_target_slot="a",
                              created_at=now - timedelta(hours=25)))
        session.commit()

    row = backups_route._protection_overview(tracked, now=now)["rows"][0]

    assert row["rpo_hours"] == 48
    assert row["status"] == "healthy"
    assert row["remaining_hours"] == 23


def test_protection_overview_uses_additional_cluster_rpo(dashboard_client, monkeypatch):
    now = datetime.utcnow()
    cluster = SimpleNamespace(id="cluster-rpo", is_default=False, backup_rpo_hours=48)
    monkeypatch.setattr(backups_route, "load_backup_policy",
                        lambda: {"tracked_images": [], "rpo_hours": 24})
    with db_module.SessionLocal() as session:
        session.add(BackupJob(run_id="cluster-run", cluster_id=cluster.id, pool="rbd",
                              image="vm1", job_type="full", status="SUCCESS",
                              backup_target_slot="cluster",
                              created_at=now - timedelta(hours=25)))
        session.commit()

    row = backups_route._protection_overview(
        [{"pool": "rbd", "image": "vm1"}], cluster=cluster, now=now
    )["rows"][0]

    assert row["rpo_hours"] == 48
    assert row["status"] == "healthy"


def test_queue_success_time_does_not_get_replaced_by_newer_failed_run(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    success_at = datetime.utcnow() - timedelta(hours=2)
    with db_module.SessionLocal() as session:
        session.add_all(
            [
                BackupJob(
                    run_id="ok", pool="vms", image="disk1", job_type="full", status="SUCCESS",
                    created_at=success_at, finished_at=success_at,
                ),
                BackupJob(
                    run_id="failed", pool="vms", image="disk1", job_type="incremental", status="FAILED",
                    created_at=datetime.utcnow(), finished_at=datetime.utcnow(),
                ),
            ]
        )
        session.commit()

    entry = backups_route._queue([{"pool": "vms", "image": "disk1"}])[0]
    assert entry["last_run_at"] == success_at
    assert entry["last_status"] == "FAILED"


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


def test_digests_are_scoped_to_selected_cluster(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [])
    _login(dashboard_client)
    with db_module.SessionLocal() as session:
        cluster = _create_additional_cluster(session)
        session.add_all([
            BackupDigestLog(
                cluster_id=None,
                period_start=datetime.utcnow() - timedelta(hours=24),
                period_end=datetime.utcnow(),
                succeeded_count=1,
                failed_count=0,
                anomaly_count=0,
                summary_text="default digest",
                created_at=datetime.utcnow(),
            ),
            BackupDigestLog(
                cluster_id=cluster.id,
                period_start=datetime.utcnow() - timedelta(hours=24),
                period_end=datetime.utcnow(),
                succeeded_count=2,
                failed_count=0,
                anomaly_count=0,
                summary_text="secondary digest",
                created_at=datetime.utcnow(),
            ),
        ])
        session.commit()

    response = dashboard_client.get(f"/backups?cluster={cluster.id}")

    assert response.status_code == 200
    assert "secondary digest" in response.text
    assert "default digest" not in response.text


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


def test_progress_api_does_not_read_running_action_from_another_cluster(dashboard_client):
    _login(dashboard_client)
    with db_module.SessionLocal() as session:
        cluster = _create_additional_cluster(session)
        _create_running_action(session, "rbd_backup_run", [{"pct": 25}])

    response = dashboard_client.get(f"/api/backups/progress?cluster={cluster.id}")

    assert response.status_code == 200
    assert response.json() == {"action_id": None, "status": None, "progress": []}


def test_manual_backup_is_only_blocked_by_same_cluster(dashboard_client):
    _login(dashboard_client)
    with db_module.SessionLocal() as session:
        cluster = _create_additional_cluster(session)
        _create_running_action(session, "rbd_backup_run", [{"pct": 25}])

    first = dashboard_client.post(
        f"/backups/run-now?cluster={cluster.id}", json={"pool": "vms", "image": "disk1"}
    )
    second = dashboard_client.post(
        f"/backups/run-now?cluster={cluster.id}", json={"pool": "vms", "image": "disk1"}
    )

    assert first.status_code == 201
    assert second.status_code == 409
    with db_module.SessionLocal() as session:
        action = session.get(Action, first.json()["action_id"])
        assert session.get(Incident, action.incident_id).cluster_id == cluster.id


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
    _seed_successful_full()
    _login(dashboard_client)

    response = dashboard_client.post("/backups/restore/propose", json={"pool": "vms", "image": "disk1"})

    assert response.status_code == 201
    action_id = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.action_id == "restore_rbd_image_to_production"
        # AI roadmap Pha 0.4 (2026-08-18): moved risky: -> destructive: —
        # restores a backup OVER a live production image, the exact "ghi
        # đè production" case roadmap section 3.3 names explicitly. Still
        # always required explicit Dashboard approval either way (this
        # test's own PENDING_APPROVAL assertion below is unchanged) — a
        # stricter classification, not a behavior change.
        assert action.classification == "DESTRUCTIVE"
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        params = json.loads(action.action_params)
        assert params["pool"] == "vms"
        assert params["image"] == "disk1"
        assert params["recovery_point_job_id"]
        assert json.loads(action.target_nodes) == ["10.20.1.112"]


def test_restore_propose_rejects_second_proposal_while_one_pending(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}, {"pool": "vms", "image": "disk2"}])
    monkeypatch.setattr(backups_route.settings, "ceph_mon_nodes", "10.20.1.112", raising=False)
    _seed_successful_full(image="disk1")
    _seed_successful_full(image="disk2")
    _login(dashboard_client)

    first = dashboard_client.post("/backups/restore/propose", json={"pool": "vms", "image": "disk1"})
    assert first.status_code == 201

    second = dashboard_client.post("/backups/restore/propose", json={"pool": "vms", "image": "disk2"})
    assert second.status_code == 409


def test_restore_propose_requires_ceph_mon_nodes_configured(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    monkeypatch.setattr(backups_route.settings, "ceph_mon_nodes", "", raising=False)
    _seed_successful_full()
    _login(dashboard_client)

    response = dashboard_client.post("/backups/restore/propose", json={"pool": "vms", "image": "disk1"})

    assert response.status_code == 400


def test_backups_page_shows_pending_restore_action_with_approve_reject(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    monkeypatch.setattr(backups_route.settings, "ceph_mon_nodes", "10.20.1.112", raising=False)
    _seed_successful_full()
    _login(dashboard_client)

    propose = dashboard_client.post("/backups/restore/propose", json={"pool": "vms", "image": "disk1"})
    action_id = propose.json()["action_id"]

    response = dashboard_client.get("/backups")

    assert response.status_code == 200
    assert f"/actions/{action_id}/approve" in response.text
    assert f"/actions/{action_id}/reject" in response.text
    # The restore button for other tracked images is disabled while one is pending.
    assert "btn-restore-image" in response.text


def test_restore_propose_rejects_when_no_successful_full_exists(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    monkeypatch.setattr(backups_route.settings, "ceph_mon_nodes", "10.20.1.112", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post("/backups/restore/propose", json={"pool": "vms", "image": "disk1"})

    assert response.status_code == 409
    assert "full backup" in response.json()["detail"]


def _stub_restore_as_new_preflight(monkeypatch, inventory=None, max_available=10_000, **overview):
    monkeypatch.setattr(backups_route.ceph_client, "query_rbd_inventory", lambda pool: inventory or [])
    monkeypatch.setattr(
        backups_route.ceph_client,
        "query_rbd_pool_overview",
        lambda pool: {"max_available": max_available, **overview},
    )
    monkeypatch.setattr(
        backups_route.ceph_client, "query_rbd_image_detail",
        lambda pool, image: {"watchers": [], "snapshots": [], "children": [], "partial_errors": {}},
    )


def test_restore_as_new_propose_creates_pending_risky_action(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    _stub_restore_as_new_preflight(monkeypatch)
    monkeypatch.setattr(backups_route.settings, "ceph_mon_nodes", "10.20.1.112", raising=False)
    full_id = _seed_successful_full()
    _login(dashboard_client)

    response = dashboard_client.post(
        "/backups/restore-as-new/propose",
        json={"pool": "vms", "image": "disk1", "dest_pool": "recovery", "dest_image": "disk1-copy"},
    )

    assert response.status_code == 201
    with db_module.SessionLocal() as session:
        action = session.get(Action, response.json()["action_id"])
        assert action.action_id == "restore_rbd_image_as_new"
        assert action.classification == "RISKY"
        params = json.loads(action.action_params)
        assert {key: params[key] for key in ("pool", "image", "dest_pool", "dest_image",
                                               "recovery_point_job_id")} == {
            "pool": "vms", "image": "disk1", "dest_pool": "recovery", "dest_image": "disk1-copy",
            "recovery_point_job_id": full_id}
        assert params["preflight"]["passed"] is True
        assert params["preflight"]["destination"]["exists"] is False
        assert "không thay đổi volume nguồn" in action.rationale


def test_restore_as_new_rejects_existing_destination(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    _stub_restore_as_new_preflight(monkeypatch, inventory=[{"name": "disk1-copy"}])
    monkeypatch.setattr(backups_route.settings, "ceph_mon_nodes", "10.20.1.112", raising=False)
    _seed_successful_full()
    _login(dashboard_client)

    response = dashboard_client.post(
        "/backups/restore-as-new/propose",
        json={"pool": "vms", "image": "disk1", "dest_pool": "recovery", "dest_image": "disk1-copy"},
    )

    assert response.status_code == 409
    assert "destination_exists" in response.json()["detail"]["blockers"]


def test_restore_as_new_preflight_rejects_near_full_pool(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    _stub_restore_as_new_preflight(monkeypatch, near_full=True)
    _seed_successful_full()
    _login(dashboard_client)

    response = dashboard_client.post("/backups/restore-as-new/propose", json={
        "pool": "vms", "image": "disk1", "dest_pool": "recovery", "dest_image": "copy"})

    assert response.status_code == 409
    assert "destination_pool_near_full" in response.json()["detail"]["blockers"]


def test_restore_as_new_rejects_same_source_and_destination(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    _login(dashboard_client)

    response = dashboard_client.post(
        "/backups/restore-as-new/propose",
        json={"pool": "vms", "image": "disk1", "dest_pool": "vms", "dest_image": "disk1"},
    )

    assert response.status_code == 400
    assert "phải khác" in response.json()["detail"]


def test_backups_page_uses_restore_as_new_as_safe_default(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    _seed_successful_full()
    _login(dashboard_client)

    response = dashboard_client.get("/backups")

    assert "Khôi phục thành volume mới" in response.text
    assert "không thay đổi volume nguồn" in response.text


def test_recovery_points_api_returns_exact_full_and_incremental_chains(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    full_id = _seed_successful_full()
    with db_module.SessionLocal() as session:
        diff = BackupJob(run_id="diff-1", pool="vms", image="disk1", job_type="incremental",
            status="SUCCESS", base_job_id=full_id, backup_target_slot="a", remote_key="diff.bin",
            size_bytes=123, created_at=datetime.utcnow(), finished_at=datetime.utcnow())
        session.add(diff)
        session.commit()
        diff_id = diff.id
    _login(dashboard_client)

    response = dashboard_client.get("/api/backups/recovery-points?pool=vms&image=disk1")

    assert response.status_code == 200
    points = response.json()["recovery_points"]
    assert points[0]["job_id"] == diff_id
    assert points[0]["chain_job_ids"] == [full_id, diff_id]
    assert points[0]["chain_length"] == 2


def test_restore_as_new_rejects_unknown_recovery_point(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    _stub_restore_as_new_preflight(monkeypatch)
    _seed_successful_full()
    _login(dashboard_client)

    response = dashboard_client.post("/backups/restore-as-new/propose", json={
        "pool": "vms", "image": "disk1", "dest_pool": "recovery", "dest_image": "copy",
        "recovery_point_job_id": "not-a-real-job",
    })

    assert response.status_code == 409
    assert "Recovery point" in response.json()["detail"]


def test_admin_can_queue_manual_rbd_backup(dashboard_client, monkeypatch):
    _stub_tracked_images(monkeypatch, [{"pool": "vms", "image": "disk1"}])
    monkeypatch.setattr(backups_route.settings, "ceph_mon_nodes", "10.20.1.112", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post("/backups/run-now", json={"pool": "vms", "image": "disk1"})

    assert response.status_code == 201
    with db_module.SessionLocal() as session:
        action = session.get(Action, response.json()["action_id"])
        assert action.action_id == "rbd_backup_run"
        assert action.status == ActionStatus.APPROVED.value
        assert json.loads(action.action_params) == {"pool": "vms", "image": "disk1"}
        audit_entry = session.query(AuditEntry).filter_by(action_id=action.id).one()
        assert audit_entry.event_type == "backup_manual_requested"
        assert audit_entry.actor == "admin"


def test_admin_can_queue_manual_metadata_backup(dashboard_client, monkeypatch):
    monkeypatch.setattr(backups_route.settings, "ceph_mon_nodes", "10.20.1.112", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post("/backups/metadata/run-now", json={})

    assert response.status_code == 201
    with db_module.SessionLocal() as session:
        action = session.get(Action, response.json()["action_id"])
        assert action.action_id == "backup_metadata_run"
        assert action.status == ActionStatus.APPROVED.value


def test_backup_operations_reject_non_admin(dashboard_client, monkeypatch):
    _login(dashboard_client)
    monkeypatch.setattr(backups_route.auth, "is_admin_user", lambda _user: False)

    assert dashboard_client.post("/backups/run-now", json={}).status_code == 403
    assert dashboard_client.post("/backups/metadata/run-now", json={}).status_code == 403
    assert dashboard_client.post("/backups/restore/propose", json={}).status_code == 403
