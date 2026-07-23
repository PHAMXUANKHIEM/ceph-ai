import dashboard.routes.upgrade as upgrade_route
from shared import db as db_module
from shared.models import UpgradeProcedureDocument


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _stub_summarize(monkeypatch, summary_text=None, error=None):
    async def fake_summarize(raw_text):
        if error is not None:
            raise upgrade_route.UpgradeProcedureSummaryError(error)
        return summary_text or f"TOM TAT: {raw_text[:20]}"

    monkeypatch.setattr(upgrade_route, "_summarize_upgrade_procedure", fake_summarize)


def _upload(client, content: bytes = b"Buoc 1: kiem tra HEALTH_OK\nBuoc 2: chay upgrade", filename="quy-trinh.txt"):
    return client.post(
        "/upgrade/procedure/upload",
        files={"file": (filename, content, "text/plain")},
        follow_redirects=False,
    )


def test_unauthenticated_upload_redirects_to_login(dashboard_client):
    response = _upload(dashboard_client)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_upload_saves_document_and_ai_summary(dashboard_client, monkeypatch):
    _stub_summarize(monkeypatch, summary_text="Tom tat quy trinh nang cap do AI tao ra.")
    _login(dashboard_client)

    response = _upload(dashboard_client)

    assert response.status_code == 303
    assert response.headers["location"] == "/upgrade"

    with db_module.SessionLocal() as session:
        doc = session.get(UpgradeProcedureDocument, 1)
    assert doc is not None
    assert doc.filename == "quy-trinh.txt"
    assert "Buoc 1" in doc.raw_text
    assert doc.summary_text == "Tom tat quy trinh nang cap do AI tao ra."
    assert doc.summary_error is None
    assert doc.uploaded_by == "admin"

    page = dashboard_client.get("/upgrade")
    assert "quy-trinh.txt" in page.text
    assert "Tom tat quy trinh nang cap do AI tao ra." in page.text


def test_upload_rejects_unsupported_extension(dashboard_client, monkeypatch):
    _stub_summarize(monkeypatch, summary_text="unused")
    _login(dashboard_client)

    response = _upload(dashboard_client, filename="quy-trinh.pdf")

    assert response.status_code == 400
    with db_module.SessionLocal() as session:
        assert session.get(UpgradeProcedureDocument, 1) is None


def test_upload_rejects_file_too_large(dashboard_client, monkeypatch):
    _stub_summarize(monkeypatch, summary_text="unused")
    monkeypatch.setattr(upgrade_route, "MAX_PROCEDURE_FILE_BYTES", 10)
    _login(dashboard_client)

    response = _upload(dashboard_client, content=b"a" * 100)

    assert response.status_code == 400


def test_upload_rejects_empty_file(dashboard_client, monkeypatch):
    _stub_summarize(monkeypatch, summary_text="unused")
    _login(dashboard_client)

    response = _upload(dashboard_client, content=b"   \n  ")

    assert response.status_code == 400


def test_upload_rejects_undecodable_content(dashboard_client, monkeypatch):
    _stub_summarize(monkeypatch, summary_text="unused")
    _login(dashboard_client)

    response = _upload(dashboard_client, content=b"\xff\xfe\x00\x01binary")

    assert response.status_code == 400


def test_upload_succeeds_even_when_ai_summary_fails(dashboard_client, monkeypatch):
    _stub_summarize(monkeypatch, error="Chưa cấu hình 9router (API key/Base URL) — vào Cài đặt để kết nối.")
    _login(dashboard_client)

    response = _upload(dashboard_client)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        doc = session.get(UpgradeProcedureDocument, 1)
    assert doc is not None
    assert doc.summary_text is None
    assert "9router" in doc.summary_error

    page = dashboard_client.get("/upgrade")
    assert "Tóm tắt bằng AI thất bại" in page.text
    assert "Thử tóm tắt lại" in page.text


def test_reupload_replaces_previous_document(dashboard_client, monkeypatch):
    _stub_summarize(monkeypatch, summary_text="tom tat 1")
    _login(dashboard_client)
    _upload(dashboard_client, content=b"noi dung cu", filename="cu.txt")

    _stub_summarize(monkeypatch, summary_text="tom tat 2")
    _upload(dashboard_client, content=b"noi dung moi", filename="moi.txt")

    with db_module.SessionLocal() as session:
        docs = session.query(UpgradeProcedureDocument).all()
    assert len(docs) == 1
    assert docs[0].filename == "moi.txt"
    assert docs[0].summary_text == "tom tat 2"


def test_resummarize_requires_existing_document(dashboard_client, monkeypatch):
    _login(dashboard_client)

    response = dashboard_client.post("/upgrade/procedure/resummarize")

    assert response.status_code == 404


def test_resummarize_retries_ai_using_stored_raw_text(dashboard_client, monkeypatch):
    _stub_summarize(monkeypatch, error="Không thể kết nối 9router")
    _login(dashboard_client)
    _upload(dashboard_client, content=b"quy trinh goc")

    _stub_summarize(monkeypatch, summary_text="tom tat sau khi thu lai")
    response = dashboard_client.post("/upgrade/procedure/resummarize", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        doc = session.get(UpgradeProcedureDocument, 1)
    assert doc.summary_text == "tom tat sau khi thu lai"
    assert doc.summary_error is None
    assert "quy trinh goc" in doc.raw_text


def test_pending_action_view_shows_uploaded_procedure_summary(dashboard_client, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.150")
    monkeypatch.setattr(upgrade_route, "summarize_cluster_versions", lambda: {
        "raw": {}, "per_type": {}, "distinct_versions": [], "is_mixed": False, "current_version": "18.2.4",
    })
    monkeypatch.setattr(upgrade_route, "propose_next_version", lambda v: "19.2.0")
    monkeypatch.setattr(upgrade_route, "get_upgrade_status", lambda: {"in_progress": False})
    _stub_summarize(monkeypatch, summary_text="Runbook noi bo: kiem tra backup truoc khi nang cap")
    _login(dashboard_client)
    _upload(dashboard_client)

    dashboard_client.post("/upgrade/propose", data={"target_version": "19.2.0"})
    page = dashboard_client.get("/upgrade")

    assert "Tóm tắt quy trình nâng cấp đã upload (AI)" in page.text
    assert "Runbook noi bo: kiem tra backup truoc khi nang cap" in page.text
