from datetime import datetime, timedelta

import bcrypt

import dashboard.routes.maintenance as maintenance_route
from shared import db as db_module
from shared.db import Base, make_engine
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    AuditEntry,
    BackupDigestLog,
    ChatMessage,
    Incident,
    IncidentStatus,
    NodeDiagnosticRun,
    User,
    VolumeMetric,
)


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _create_user(username, password, *, is_admin=False):
    with db_module.SessionLocal() as session:
        session.add(
            User(
                username=username,
                password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
                is_admin=is_admin,
                is_active=True,
                created_by="admin",
            )
        )
        session.commit()


def _login_as(client, username, password):
    client.post("/login", data={"username": username, "password": password})


def _seed_incident_with_action_and_audit(incident_id: str, detected_at: datetime) -> None:
    with db_module.SessionLocal() as session:
        session.add(
            Incident(id=incident_id, ceph_code="OSD_DOWN", status=IncidentStatus.RESOLVED.value, detected_at=detected_at)
        )
        session.flush()
        action = Action(
            incident_id=incident_id,
            action_id="resync_ntp",
            classification=ActionClassification.SAFE.value,
            status=ActionStatus.AUTO_EXECUTED.value,
        )
        session.add(action)
        session.flush()
        session.add(
            AuditEntry(
                incident_id=incident_id,
                action_id=action.id,
                event_type="safe_action_executed",
                actor="system",
                created_at=detected_at,
            )
        )
        session.commit()


def _seed_diagnostic_run(created_at: datetime) -> None:
    with db_module.SessionLocal() as session:
        session.add(
            NodeDiagnosticRun(
                host="10.20.1.150",
                command_id="ceph_status",
                command_label="ceph -s",
                actor="admin",
                success=True,
                output_excerpt="HEALTH_OK",
                created_at=created_at,
            )
        )
        session.commit()


def _seed_volume_metric(polled_at: datetime) -> None:
    with db_module.SessionLocal() as session:
        session.add(
            VolumeMetric(
                pool="vms",
                image="disk-1",
                iops=100.0,
                read_latency_ms=1.0,
                write_latency_ms=1.0,
                saturated=False,
                polled_at=polled_at,
            )
        )
        session.commit()


def _seed_backup_digest_log(created_at: datetime) -> None:
    with db_module.SessionLocal() as session:
        session.add(
            BackupDigestLog(
                period_start=created_at - timedelta(hours=24),
                period_end=created_at,
                succeeded_count=1,
                failed_count=0,
                anomaly_count=0,
                summary_text="OK",
                created_at=created_at,
            )
        )
        session.commit()


def test_unauthenticated_post_cleanup_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/settings/cleanup", data={}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_cleanup_requires_at_least_one_target(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post("/settings/cleanup", data={"cutoff_date": ""})

    assert response.status_code == 200
    assert "Chọn ít nhất 1 loại dữ liệu" in response.text


def test_cleanup_rejects_invalid_cutoff_date(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cleanup", data={"target_db": "on", "cutoff_date": "not-a-date"}
    )

    assert response.status_code == 200
    assert "Ngày không hợp lệ" in response.text


def test_cleanup_db_with_cutoff_deletes_old_and_keeps_new(dashboard_client):
    old_id = "old-incident"
    new_id = "new-incident"
    _seed_incident_with_action_and_audit(old_id, datetime(2026, 1, 1))
    _seed_incident_with_action_and_audit(new_id, datetime(2026, 7, 1))
    _seed_diagnostic_run(datetime(2026, 1, 1))
    _seed_diagnostic_run(datetime(2026, 7, 1))
    _seed_volume_metric(datetime(2026, 1, 1))
    _seed_volume_metric(datetime(2026, 7, 1))

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cleanup", data={"target_db": "on", "cutoff_date": "2026-03-01"}
    )

    assert response.status_code == 200
    assert "Đã xóa xong" in response.text
    with db_module.SessionLocal() as session:
        remaining_incident_ids = {row.id for row in session.query(Incident).all()}
        assert remaining_incident_ids == {new_id}
        assert session.query(Action).filter_by(incident_id=old_id).count() == 0
        assert session.query(Action).filter_by(incident_id=new_id).count() == 1
        assert session.query(AuditEntry).filter_by(incident_id=old_id).count() == 0
        assert session.query(AuditEntry).filter_by(incident_id=new_id).count() == 1
        assert session.query(NodeDiagnosticRun).count() == 1
        assert session.query(NodeDiagnosticRun).first().created_at == datetime(2026, 7, 1)
        assert session.query(VolumeMetric).count() == 1
        assert session.query(VolumeMetric).first().polled_at == datetime(2026, 7, 1)


def test_cleanup_db_blank_cutoff_deletes_everything(dashboard_client):
    _seed_incident_with_action_and_audit("incident-a", datetime(2026, 7, 1))
    _seed_diagnostic_run(datetime(2026, 7, 1))
    _seed_volume_metric(datetime(2026, 7, 1))

    _login(dashboard_client)
    response = dashboard_client.post("/settings/cleanup", data={"target_db": "on", "cutoff_date": ""})

    assert response.status_code == 200
    with db_module.SessionLocal() as session:
        assert session.query(Incident).count() == 0
        assert session.query(Action).count() == 0
        assert session.query(AuditEntry).count() == 0
        assert session.query(NodeDiagnosticRun).count() == 0
        assert session.query(VolumeMetric).count() == 0


# -- target_backup_digest (BackupDigestLog cleanup) -------------------------
# Own separate checkbox/DB bucket — no FK relationship to Incident/Action/
# AuditEntry/NodeDiagnosticRun/VolumeMetric, so it must be clearable
# independently of target_db (and vice versa: checking target_db alone
# must leave digest logs untouched).


def test_cleanup_backup_digest_alone_satisfies_at_least_one_target(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cleanup", data={"target_backup_digest": "on", "cutoff_date": ""}
    )

    assert response.status_code == 200
    assert "Chọn ít nhất 1 loại dữ liệu" not in response.text


def test_cleanup_backup_digest_with_cutoff_deletes_old_and_keeps_new(dashboard_client):
    _seed_backup_digest_log(datetime(2026, 1, 1))
    _seed_backup_digest_log(datetime(2026, 7, 1))

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cleanup", data={"target_backup_digest": "on", "cutoff_date": "2026-03-01"}
    )

    assert response.status_code == 200
    assert "Digest backup: đã xóa 1 bản ghi" in response.text
    with db_module.SessionLocal() as session:
        remaining = session.query(BackupDigestLog).all()
        assert len(remaining) == 1
        assert remaining[0].created_at == datetime(2026, 7, 1)


def test_cleanup_backup_digest_blank_cutoff_deletes_everything(dashboard_client):
    _seed_backup_digest_log(datetime(2026, 7, 1))
    _seed_backup_digest_log(datetime(2026, 7, 2))

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cleanup", data={"target_backup_digest": "on", "cutoff_date": ""}
    )

    assert response.status_code == 200
    with db_module.SessionLocal() as session:
        assert session.query(BackupDigestLog).count() == 0


def test_cleanup_target_db_does_not_touch_backup_digest_logs(dashboard_client):
    _seed_incident_with_action_and_audit("incident-x", datetime(2026, 1, 1))
    _seed_backup_digest_log(datetime(2026, 1, 1))

    _login(dashboard_client)
    response = dashboard_client.post("/settings/cleanup", data={"target_db": "on", "cutoff_date": ""})

    assert response.status_code == 200
    with db_module.SessionLocal() as session:
        assert session.query(Incident).count() == 0
        # target_db alone must leave BackupDigestLog completely untouched.
        assert session.query(BackupDigestLog).count() == 1


def test_purge_old_records_dereferences_chat_message_pointing_at_deleted_incident(monkeypatch):
    # dashboard_client's fixture engine doesn't enable SQLite FK enforcement
    # (unlike production's shared.db.make_engine), so it can't catch this —
    # use a real FK-enforcing engine instead. ChatMessage.proposed_incident_id
    # FKs to Incident (set once a chat proposal is confirmed,
    # dashboard/routes/chat.py:446); before the fix, purging an incident that
    # had ever been confirmed from chat raised "FOREIGN KEY constraint
    # failed" and aborted the ENTIRE purge (DB and, if selected together,
    # log files too), rolled back by the `with SessionLocal()` block's
    # implicit close-time rollback.
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", db_module.sessionmaker(bind=engine))

    with db_module.SessionLocal() as session:
        session.add(
            Incident(id="inc-1", ceph_code="OSD_DOWN", status=IncidentStatus.RESOLVED.value, detected_at=datetime(2026, 1, 1))
        )
        session.flush()
        session.add(
            ChatMessage(
                session_id="s1",
                role="assistant",
                content="confirmed remediation",
                proposed_incident_id="inc-1",
                created_at=datetime(2026, 1, 1),
            )
        )
        session.commit()

    counts = maintenance_route.purge_old_records(None)

    assert counts["incidents"] == 1
    with db_module.SessionLocal() as session:
        assert session.query(Incident).count() == 0
        message = session.query(ChatMessage).one()  # chat history itself is out of purge scope, kept
        assert message.proposed_incident_id is None


def test_cleanup_files_only_does_not_touch_db(dashboard_client, monkeypatch, tmp_path):
    log_path = tmp_path / "dashboard.log"
    log_path.write_text("2026-01-01 00:00:00 INFO:x:old line\n")
    monkeypatch.setitem(maintenance_route.LOG_PATHS, "dashboard", log_path)
    monkeypatch.setitem(maintenance_route.LOG_PATHS, "watcher", tmp_path / "watcher.log")
    monkeypatch.setitem(maintenance_route.LOG_PATHS, "worker", tmp_path / "worker.log")

    _seed_incident_with_action_and_audit("incident-a", datetime(2026, 1, 1))

    _login(dashboard_client)
    response = dashboard_client.post("/settings/cleanup", data={"target_files": "on", "cutoff_date": ""})

    assert response.status_code == 200
    assert log_path.read_text() == ""
    with db_module.SessionLocal() as session:
        assert session.query(Incident).count() == 1  # untouched


def test_purge_log_file_with_cutoff_keeps_lines_on_or_after_cutoff(tmp_path):
    log_path = tmp_path / "watcher.log"
    log_path.write_text(
        "2026-01-01 00:00:00 INFO:x:old entry\n"
        "  continuation of old entry\n"
        "2026-07-01 00:00:00 INFO:x:new entry\n"
        "  continuation of new entry\n"
    )

    freed = maintenance_route._purge_log_file(log_path, datetime(2026, 3, 1))

    remaining = log_path.read_text()
    assert "old entry" not in remaining
    assert "continuation of old entry" not in remaining
    assert "new entry" in remaining
    assert "continuation of new entry" in remaining
    assert freed > 0


def test_purge_log_file_with_no_cutoff_clears_whole_file(tmp_path):
    log_path = tmp_path / "watcher.log"
    log_path.write_text("2026-07-01 00:00:00 INFO:x:some entry\n")

    freed = maintenance_route._purge_log_file(log_path, None)

    assert log_path.read_text() == ""
    assert freed > 0


def test_purge_log_file_untimestamped_content_is_treated_as_older_than_any_cutoff(tmp_path):
    # dashboard.log (uvicorn's own format) has no per-line timestamp at all —
    # a date cutoff must clear it entirely, matching what the Settings page
    # hint tells the operator to expect.
    log_path = tmp_path / "dashboard.log"
    log_path.write_text("INFO:     113.160.0.10:1234 - \"GET / HTTP/1.1\" 200 OK\n")

    maintenance_route._purge_log_file(log_path, datetime(2020, 1, 1))

    assert log_path.read_text() == ""


def test_purge_log_file_truncates_in_place_not_replacing_inode(tmp_path):
    # A running process holds this file open in append mode for its whole
    # lifetime, keyed to the inode — purging must truncate that SAME inode
    # (not os.replace with a new one), or the process's writes after this
    # point would go into a file no longer visible at this path.
    log_path = tmp_path / "watcher.log"
    log_path.write_text("2026-01-01 00:00:00 INFO:x:old\n")

    fh = open(log_path, "a")
    try:
        fh.write("2026-01-01 00:00:01 INFO:x:written before purge\n")
        fh.flush()

        maintenance_route._purge_log_file(log_path, None)

        fh.write("2026-07-01 00:00:00 INFO:x:written after purge\n")
        fh.flush()
    finally:
        fh.close()

    assert log_path.read_text() == "2026-07-01 00:00:00 INFO:x:written after purge\n"


def test_cleanup_both_targets_reports_both_in_success_message(dashboard_client, monkeypatch, tmp_path):
    for name in maintenance_route.LOG_PATHS:
        monkeypatch.setitem(maintenance_route.LOG_PATHS, name, tmp_path / f"{name}.log")
    maintenance_route.LOG_PATHS["watcher"].write_text("2026-01-01 00:00:00 INFO:x:line\n")

    _seed_incident_with_action_and_audit("incident-a", datetime(2026, 1, 1))

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cleanup", data={"target_files": "on", "target_db": "on", "cutoff_date": ""}
    )

    assert response.status_code == 200
    assert "DB:" in response.text
    assert "File log:" in response.text


# --- GET /api/settings/server-log (2026-07-28, admin-only in-browser log
# viewer — every other error message in this app says "xem log server để
# biết chi tiết" without offering a way to actually do that). ------------


def test_unauthenticated_server_log_api_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/api/settings/server-log?name=dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_server_log_api_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.get("/api/settings/server-log?name=dashboard")

    assert response.status_code == 403


def test_server_log_api_rejects_unknown_log_name(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/api/settings/server-log?name=not-a-real-log")
    assert response.status_code == 404


def test_server_log_api_returns_tail_of_requested_log(dashboard_client, monkeypatch, tmp_path):
    log_path = tmp_path / "watcher.log"
    log_path.write_text(
        "2026-01-01 00:00:00 INFO:x:line one\n"
        "2026-01-01 00:00:01 ERROR:x:line two\n"
    )
    monkeypatch.setitem(maintenance_route.LOG_PATHS, "watcher", log_path)
    _login(dashboard_client)

    response = dashboard_client.get("/api/settings/server-log?name=watcher")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "watcher"
    assert body["lines"] == [
        "2026-01-01 00:00:00 INFO:x:line one",
        "2026-01-01 00:00:01 ERROR:x:line two",
    ]


def test_server_log_api_filters_by_term_case_insensitively(dashboard_client, monkeypatch, tmp_path):
    log_path = tmp_path / "watcher.log"
    log_path.write_text(
        "2026-01-01 00:00:00 INFO:x:all good here\n"
        "2026-01-01 00:00:01 ERROR:x:something broke\n"
    )
    monkeypatch.setitem(maintenance_route.LOG_PATHS, "watcher", log_path)
    _login(dashboard_client)

    response = dashboard_client.get("/api/settings/server-log?name=watcher&filter=error")

    assert response.status_code == 200
    assert response.json()["lines"] == ["2026-01-01 00:00:01 ERROR:x:something broke"]


def test_server_log_api_returns_empty_list_for_missing_log_file(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setitem(maintenance_route.LOG_PATHS, "watcher", tmp_path / "does-not-exist.log")
    _login(dashboard_client)

    response = dashboard_client.get("/api/settings/server-log?name=watcher")

    assert response.status_code == 200
    assert response.json()["lines"] == []


def test_tail_log_lines_caps_at_max_lines(tmp_path):
    log_path = tmp_path / "big.log"
    log_path.write_text("".join(f"line {i}\n" for i in range(maintenance_route.MAX_SERVER_LOG_LINES + 50)))

    lines = maintenance_route._tail_log_lines(log_path)

    assert len(lines) == maintenance_route.MAX_SERVER_LOG_LINES
    assert lines[0] == f"line {50}"  # oldest 50 lines dropped, tail kept
    assert lines[-1] == f"line {maintenance_route.MAX_SERVER_LOG_LINES + 49}"
