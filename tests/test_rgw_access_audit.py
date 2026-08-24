from datetime import datetime, timezone

from config.settings import settings
from shared.models import Cluster, RgwAccessAuditEvent
from worker import rgw_access_audit as audit


def _row(method="PUT", path="/photos/a.jpg", status=200):
    return {"timestamp": datetime(2026, 8, 24, 1, 2, 3, 4000, timezone.utc),
            "timestamp_raw": "24/Aug/2026:01:02:03.004 +0000", "method": method,
            "path": path, "bucket": "photos", "object": "a.jpg", "action": "Tải lên",
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
        method="PUT", action="Tải lên", bucket="khiem.mmt204.test", object_key="test",
        requester="admin", remote_addr="1.2.3.4", http_status=200, bytes_sent=12,
        encryption="SSE-S3 (AES256)", event_at=datetime(2026, 3, 26, 8, 31, 14))
    message = audit._message(event, "ceph")
    assert "Hành động: ObjectCreated:Put" in message
    assert "Size: 12.00 B" in message
    assert "User: admin" in message
    assert "Mã hóa: 🔐 SSE-S3 (AES256)" in message
    assert "Giờ VN: 15:31:14 - 26/03/2026" in message


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
