from datetime import datetime, timedelta, timezone

from config.settings import settings
from sqlalchemy.orm import sessionmaker
from shared import db
from shared.models import (
    Cluster, LogIngestRun, RgwAccessAuditEvent, RgwAnalysisJob, RgwErrorNotification,
)
from worker import rgw_access_audit as audit


def _row(method="PUT", path="/photos/a.jpg", status=200):
    return {"timestamp": datetime(2026, 8, 24, 1, 2, 3, 4000, timezone.utc),
            "timestamp_raw": "24/Aug/2026:01:02:03.004 +0000", "method": method,
            "path": path, "bucket": "photos", "object": "a.jpg", "action": "Tải lên",
            "transaction_id": "tx-test-123",
            "requester": "alice", "remote_addr": "10.0.0.4", "status": status,
            "bytes_sent": 12, "latency_ms": 1.5}


def test_first_scan_baselines_then_new_request_is_sent(db_session, monkeypatch):
    cluster = Cluster(name="audit", ceph_mon_nodes="", ceph_rgw_nodes="rgw1", is_default=False,
                      is_active=True, ssh_user="root", ssh_key_path="/key", ceph_exec_mode="none")
    db_session.add(cluster)
    db_session.commit()
    rows = [_row()]
    monkeypatch.setattr(audit, "_fetch", lambda _cluster, _host: list(rows))
    sent = []
    monkeypatch.setattr(audit, "send_telegram_message", lambda token, chat, text: sent.append(text))
    monkeypatch.setattr(settings, "rgw_access_audit_enabled", True)
    monkeypatch.setattr(settings, "telegram_rgw_enabled", True)
    monkeypatch.setattr(settings, "telegram_rgw_bot_token", "token")
    monkeypatch.setattr(settings, "telegram_rgw_chat_id", "chat")

    audit._ingest_host(db_session, cluster, "rgw1")
    audit._deliver_pending(db_session)
    assert sent == []
    rows.append(_row("GET", "/photos/a.jpg?X-Amz-Signature=secret"))
    audit._ingest_host(db_session, cluster, "rgw1")
    audit._deliver_pending(db_session)
    assert len(sent) == 1
    assert "ObjectAccessed:Get" in sent[0]
    assert "Request ID: tx-test-123" in sent[0]
    assert "Bucket: photos" in sent[0] and "File: a.jpg" in sent[0]
    assert "Giờ VN: 08:02:03 - 24/08/2026" in sent[0]
    assert "secret" not in sent[0]

    events = db_session.query(RgwAccessAuditEvent).all()
    assert len(events) == 2
    assert all(event.telegram_sent for event in events)


def test_business_message_formats_upload_size_and_event_name(db_session):
    cluster = Cluster(name="audit", ceph_mon_nodes="", ceph_rgw_nodes="", is_default=False,
                      is_active=True, ssh_user="root", ssh_key_path="/key", ceph_exec_mode="none")
    db_session.add(cluster)
    db_session.flush()
    event = RgwAccessAuditEvent(cluster_id=cluster.id, rgw_host="rgw1", fingerprint="a" * 64,
        transaction_id="tx-put-456", method="PUT", action="Tải lên", bucket="khiem.mmt204.test", object_key="test",
        requester="admin", remote_addr="1.2.3.4", http_status=200, bytes_sent=12,
        encryption="SSE-S3 (AES256)", event_at=datetime(2026, 3, 26, 8, 31, 14))
    message = audit._message(event, "ceph")
    assert "Hành động: ObjectCreated:Put" in message
    assert "Request ID: tx-put-456" in message
    assert "Size: 12.00 B" in message
    assert "User: admin" in message
    assert "Mã hóa: 🔐 SSE-S3 (AES256)" in message
    assert "Giờ VN: 15:31:14 - 26/03/2026" in message


def test_delete_message_uses_previous_object_size_not_empty_response(db_session, monkeypatch):
    cluster = Cluster(name="audit", ceph_mon_nodes="", ceph_rgw_nodes="", is_default=False,
                      is_active=True, ssh_user="root", ssh_key_path="/key", ceph_exec_mode="none")
    db_session.add(cluster)
    db_session.flush()
    uploaded = RgwAccessAuditEvent(
        cluster_id=cluster.id, rgw_host="rgw1", fingerprint="b" * 64,
        method="PUT", action="Tải lên", bucket="test-s3", object_key="concurrent-put-144.bin",
        requester="admin", remote_addr="10.20.1.39", http_status=200, bytes_sent=65536,
        event_at=datetime(2026, 8, 24, 9, 58, 35), telegram_sent=True,
    )
    deleted = RgwAccessAuditEvent(
        cluster_id=cluster.id, rgw_host="rgw1", fingerprint="c" * 64,
        method="DELETE", action="Xoá tệp", bucket="test-s3", object_key="concurrent-put-144.bin",
        requester="admin", remote_addr="10.20.1.39", http_status=204, bytes_sent=0,
        event_at=datetime(2026, 8, 25, 1, 55, 1),
    )
    db_session.add_all((uploaded, deleted))
    db_session.commit()
    sent = []
    monkeypatch.setattr(settings, "telegram_rgw_enabled", True)
    monkeypatch.setattr(settings, "telegram_rgw_bot_token", "token")
    monkeypatch.setattr(settings, "telegram_rgw_chat_id", "chat")
    monkeypatch.setattr(audit, "send_telegram_message", lambda _token, _chat, text: sent.append(text))

    audit._deliver_pending(db_session)

    assert len(sent) == 1
    assert "ObjectRemoved:Delete" in sent[0]
    assert "Size: 64.00 KB" in sent[0]


def test_delete_message_reports_unknown_when_no_prior_size(db_session):
    cluster = Cluster(name="audit", ceph_mon_nodes="", ceph_rgw_nodes="", is_default=False,
                      is_active=True, ssh_user="root", ssh_key_path="/key", ceph_exec_mode="none")
    db_session.add(cluster)
    db_session.flush()
    deleted = RgwAccessAuditEvent(
        cluster_id=cluster.id, rgw_host="rgw1", fingerprint="d" * 64,
        method="DELETE", action="Xoá tệp", bucket="b", object_key="unknown.bin",
        requester="admin", remote_addr="10.20.1.39", http_status=204, bytes_sent=0,
        event_at=datetime(2026, 8, 25, 1, 55, 1),
    )
    db_session.add(deleted)
    db_session.commit()

    assert audit._notification_size(db_session, deleted) is None
    assert "Size: -" in audit._message(deleted, "ceph", None)


def test_failed_delivery_remains_pending_for_retry(db_session, monkeypatch):
    cluster = Cluster(name="audit", ceph_mon_nodes="", ceph_rgw_nodes="", is_default=False,
                      is_active=True, ssh_user="root", ssh_key_path="/key", ceph_exec_mode="none")
    db_session.add(cluster)
    db_session.flush()
    event = RgwAccessAuditEvent(cluster_id=cluster.id, rgw_host="rgw1", fingerprint="f" * 64,
        method="DELETE", action="Xoá tệp", bucket="b", object_key="x", requester="u",
        remote_addr="1.2.3.4", http_status=204, event_at=datetime.utcnow())
    db_session.add(event)
    db_session.commit()
    monkeypatch.setattr(settings, "telegram_rgw_enabled", True)
    monkeypatch.setattr(settings, "telegram_rgw_bot_token", "token")
    monkeypatch.setattr(settings, "telegram_rgw_chat_id", "chat")
    from shared.telegram_client import TelegramSendError
    monkeypatch.setattr(audit, "send_telegram_message", lambda *_: (_ for _ in ()).throw(TelegramSendError("down")))
    audit._deliver_pending(db_session)
    db_session.refresh(event)
    assert not event.telegram_sent
    assert event.telegram_attempts == 1


def _error_event(db_session, cluster, message, *, fingerprint):
    event = RgwErrorNotification(
        cluster_id=cluster.id, rgw_host="10.3.53.1", fingerprint=fingerprint,
        message=message, event_at=datetime.utcnow(),
    )
    db_session.add(event)
    db_session.flush()
    return event


def test_vault_errors_share_one_immediate_analysis_job(db_session):
    cluster = Cluster(name="rgw-jobs", ceph_mon_nodes="", ceph_rgw_nodes="10.3.53.1",
                      is_default=False, is_active=True, ssh_user="root",
                      ssh_key_path="/key", ceph_exec_mode="none")
    db_session.add(cluster)
    db_session.flush()
    first = _error_event(db_session, cluster, "Request to Vault failed with error -13",
                         fingerprint="1" * 64)
    first.created_at = datetime.utcnow()
    assert audit._queue_analysis_job(db_session, first) is not None
    db_session.flush()
    second = _error_event(
        db_session, cluster,
        "failed to retrieve actual key from key_id: 74e81cdc-d01b-4fcb-ac7e-8708583c6d51.1",
        fingerprint="2" * 64,
    )
    second.created_at = first.created_at + timedelta(seconds=1)
    assert audit._queue_analysis_job(db_session, second) is None
    assert db_session.query(RgwAnalysisJob).count() == 1


def test_error_telegram_exposes_real_job_state(db_session, monkeypatch):
    cluster = Cluster(name="rgw-message", ceph_mon_nodes="", ceph_rgw_nodes="10.3.53.1",
                      is_default=False, is_active=True, ssh_user="root",
                      ssh_key_path="/key", ceph_exec_mode="none")
    db_session.add(cluster)
    db_session.flush()
    event = _error_event(db_session, cluster, "Request to Vault failed with error -13",
                         fingerprint="3" * 64)
    job = audit._queue_analysis_job(db_session, event)
    db_session.commit()
    sent = []
    monkeypatch.setattr(settings, "telegram_rgw_enabled", True)
    monkeypatch.setattr(settings, "telegram_rgw_bot_token", "token")
    monkeypatch.setattr(settings, "telegram_rgw_chat_id", "chat")
    monkeypatch.setattr(audit, "send_telegram_message", lambda _token, _chat, text: sent.append(text))

    audit._deliver_pending(db_session)

    assert f"RGW-{job.id[:8]}" in sent[0]
    assert "RGW không thể tạo hoặc lấy khóa mã hóa từ Vault" in sent[0]
    assert "Các request S3 cần khóa này có thể thất bại" in sent[0]
    assert "Kiểm tra trạng thái Vault" in sent[0]
    assert "Đã chuyển vào Log Intelligence" not in sent[0]


def test_duplicate_vault_error_explains_that_analysis_is_deduplicated(db_session, monkeypatch):
    cluster = Cluster(name="rgw-message", ceph_mon_nodes="", ceph_rgw_nodes="10.3.53.1",
                      is_default=False, is_active=True, ssh_user="root",
                      ssh_key_path="/key", ceph_exec_mode="none")
    db_session.add(cluster)
    db_session.flush()
    first = _error_event(db_session, cluster, "Request to Vault failed with error -22",
                         fingerprint="5" * 64)
    audit._queue_analysis_job(db_session, first)
    duplicate = _error_event(
        db_session, cluster, "failed to retrieve actual key from key_id: key.1",
        fingerprint="6" * 64,
    )
    duplicate.created_at = first.created_at + timedelta(seconds=1)
    assert audit._queue_analysis_job(db_session, duplicate) is None
    db_session.commit()
    sent = []
    monkeypatch.setattr(settings, "telegram_rgw_enabled", True)
    monkeypatch.setattr(settings, "telegram_rgw_bot_token", "token")
    monkeypatch.setattr(settings, "telegram_rgw_chat_id", "chat")
    monkeypatch.setattr(audit, "send_telegram_message", lambda _token, _chat, text: sent.append(text))

    audit._deliver_pending(db_session)

    assert any("Cùng sự cố vừa báo" in message for message in sent)
    assert all("đã gộp với job gần nhất" not in message for message in sent)


def test_immediate_job_runs_log_intelligence_and_reports_completion(db_session, monkeypatch):
    monkeypatch.setattr(
        db, "SessionLocal",
        sessionmaker(bind=db_session.get_bind(), autoflush=False, autocommit=False),
    )
    cluster = Cluster(name="rgw-run", ceph_mon_nodes="10.3.53.1",
                      ceph_rgw_nodes="10.3.53.1", is_default=False, is_active=True,
                      ssh_user="root", ssh_key_path="/key", ceph_exec_mode="none")
    db_session.add(cluster)
    db_session.flush()
    event = _error_event(db_session, cluster, "Request to Vault failed with error -13",
                         fingerprint="4" * 64)
    job = audit._queue_analysis_job(db_session, event)
    db_session.commit()
    sent = []
    monkeypatch.setattr(audit, "_send_analysis_status", sent.append)

    def fake_scan(
        cluster_id, cluster=None, *, target_host=None, target_daemon_type=None,
        focus_message=None,
    ):
        # Regression: the real scanner reads detached cluster attributes.
        # They must remain loaded after the job claim transaction commits.
        assert cluster.name == "rgw-run"
        assert target_host == "10.3.53.1"
        assert target_daemon_type == "rgw"
        assert focus_message == "Request to Vault failed with error -13"
        with db.SessionLocal() as session:
            run = LogIngestRun(
                cluster_id=cluster_id, source="loki",
                window_start=datetime.utcnow() - timedelta(minutes=20),
                window_end=datetime.utcnow(), status="OK", hosts_scanned=1,
                hosts_failed=0, lines_scanned=2, patterns_seen=1, patterns_new=1,
                patterns_flagged=1,
            )
            session.add(run)
            session.commit()
            return run.id

    from watcher import log_intel
    monkeypatch.setattr(log_intel, "scan_and_store", fake_scan)
    audit._process_analysis_jobs()

    db_session.expire_all()
    persisted = db_session.get(RgwAnalysisJob, job.id)
    assert persisted.status == "COMPLETED"
    assert persisted.ingest_run_id
    assert any("BẮT ĐẦU" in message and f"RGW-{job.id[:8]}" in message for message in sent)
    result = next(message for message in sent if "KẾT QUẢ PHÂN TÍCH" in message)
    assert "Chưa đủ bằng chứng để xác định nguyên nhân gốc" in result
    assert "Đã đối chiếu: 1 nhóm log liên quan" in result
    assert "CẦN KIỂM TRA — chưa được coi là đã khắc phục" in result
