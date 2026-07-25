import io
from datetime import datetime

import dashboard.routes.patch as patch_route
from config.settings import settings
from shared import db as db_module
from shared.models import Action, ActionStatus, Incident, IncidentStatus, PatchDocument


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _set_patch_pipeline_settings(monkeypatch, *, build_node="10.9.9.9", mon_nodes="10.20.1.150"):
    monkeypatch.setattr(settings, "ceph_patch_build_node", build_node)
    monkeypatch.setattr(settings, "ceph_patch_source_dir", "/root/ceph")
    monkeypatch.setattr(settings, "ceph_patch_build_command", "./make-srpm.sh")
    monkeypatch.setattr(settings, "ceph_patch_output_dir", "/root/rpmbuild/RPMS/x86_64")
    monkeypatch.setattr(settings, "ceph_patch_node_staging_dir", "/opt/ceph-aiops-patch-staging")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    monkeypatch.setattr(settings, "ceph_mon_nodes", mon_nodes)
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")


def _stub_command_preview(monkeypatch):
    """Avoids the real SSH round trip _safe_command_preview makes for
    patch_install (unit discovery on the first target node) — worker/
    executor/commands.py's own builder tests already cover that command
    shape directly."""
    monkeypatch.setattr(patch_route, "_safe_command_preview", lambda action_id, host, params: "STUB_PREVIEW")


def _upload_patch(client, content=b"diff --git a/x b/x\n+hello\n", filename="fix.patch"):
    return client.post(
        "/patch/upload",
        files={"file": (filename, io.BytesIO(content), "text/plain")},
    )


def _upsert_patch_document(content="diff --git a/x b/x\n+hello\n"):
    with db_module.SessionLocal() as session:
        doc = PatchDocument(id=1, filename="fix.patch", content=content, uploaded_by="admin", uploaded_at=datetime.utcnow())
        session.add(doc)
        session.commit()


# Action.status and Incident.status are separate enums (shared/models.py) —
# ActionStatus.EXECUTED, for instance, isn't a valid IncidentStatus value at
# all (that maps to IncidentStatus.RESOLVED in the real execution path, see
# worker/llm/router_client.py::_execute_approved_action). This map only
# needs to cover statuses this test file actually passes in.
_ACTION_TO_INCIDENT_STATUS = {
    ActionStatus.PENDING_APPROVAL.value: IncidentStatus.PENDING_APPROVAL.value,
    ActionStatus.APPROVED.value: IncidentStatus.APPROVED.value,
    ActionStatus.EXECUTED.value: IncidentStatus.RESOLVED.value,
    ActionStatus.FAILED.value: IncidentStatus.FAILED.value,
    ActionStatus.REJECTED.value: IncidentStatus.REJECTED.value,
}


def _pending_patch_action(action_id: str, status: str) -> str:
    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code=patch_route.CLUSTER_PATCH_CEPH_CODE,
            status=_ACTION_TO_INCIDENT_STATUS[status],
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id=action_id,
            classification="RISKY",
            status=status,
        )
        session.add(action)
        session.commit()
        return action.id


# --- GET /patch --------------------------------------------------------


def test_unauthenticated_get_patch_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/patch", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_patch_page_shows_upload_form(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/patch")

    assert response.status_code == 200
    assert 'action="/patch/upload"' in response.text
    assert 'action="/patch/propose-build"' in response.text
    assert 'action="/patch/propose-install"' in response.text


# --- Upload --------------------------------------------------------------


def test_upload_patch_success_and_shows_in_page(dashboard_client):
    _login(dashboard_client)

    response = _upload_patch(dashboard_client)
    assert response.status_code == 200  # after following the redirect

    page = dashboard_client.get("/patch")
    assert "fix.patch" in page.text
    with db_module.SessionLocal() as session:
        doc = session.get(PatchDocument, 1)
        assert doc is not None
        assert doc.uploaded_by == "admin"


def test_upload_patch_rejects_wrong_extension(dashboard_client):
    _login(dashboard_client)

    response = _upload_patch(dashboard_client, filename="fix.txt")

    assert response.status_code == 400


def test_upload_patch_rejects_oversized_file(dashboard_client):
    _login(dashboard_client)

    big_content = b"x" * (patch_route.MAX_PATCH_FILE_BYTES + 1)
    response = _upload_patch(dashboard_client, content=big_content)

    assert response.status_code == 400


def test_upload_patch_rejects_empty_file(dashboard_client):
    _login(dashboard_client)

    response = _upload_patch(dashboard_client, content=b"")

    assert response.status_code == 400


def test_upload_patch_rejects_non_utf8(dashboard_client):
    _login(dashboard_client)

    response = _upload_patch(dashboard_client, content=b"\xff\xfe\x00\x01")

    assert response.status_code == 400


# --- Propose build & stage -----------------------------------------------


def test_propose_build_requires_build_server_configured(dashboard_client, monkeypatch):
    _set_patch_pipeline_settings(monkeypatch, build_node="")
    _upsert_patch_document()
    _login(dashboard_client)

    response = dashboard_client.post("/patch/propose-build")

    assert response.status_code == 400


def test_propose_build_requires_patch_uploaded(dashboard_client, monkeypatch):
    _set_patch_pipeline_settings(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post("/patch/propose-build")

    assert response.status_code == 400


def test_propose_build_creates_pending_action(dashboard_client, monkeypatch):
    _set_patch_pipeline_settings(monkeypatch)
    _upsert_patch_document()
    _login(dashboard_client)

    response = dashboard_client.post("/patch/propose-build", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        action = (
            session.query(Action)
            .filter(Action.action_id == patch_route.PATCH_BUILD_ACTION_ID)
            .order_by(Action.created_at.desc())
            .first()
        )
        assert action is not None
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.classification == "RISKY"


def test_propose_build_rejects_duplicate_while_in_flight(dashboard_client, monkeypatch):
    _set_patch_pipeline_settings(monkeypatch)
    _upsert_patch_document()
    _pending_patch_action(patch_route.PATCH_BUILD_ACTION_ID, ActionStatus.PENDING_APPROVAL.value)
    _login(dashboard_client)

    response = dashboard_client.post("/patch/propose-build")

    assert response.status_code == 409


# --- Propose install -------------------------------------------------------


def test_propose_install_requires_prior_successful_build(dashboard_client, monkeypatch):
    _set_patch_pipeline_settings(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post("/patch/propose-install")

    assert response.status_code == 409


def test_propose_install_requires_configured_ceph_nodes(dashboard_client, monkeypatch):
    _set_patch_pipeline_settings(monkeypatch, mon_nodes="")
    _pending_patch_action(patch_route.PATCH_BUILD_ACTION_ID, ActionStatus.EXECUTED.value)
    _login(dashboard_client)

    response = dashboard_client.post("/patch/propose-install")

    assert response.status_code == 400


def test_propose_install_creates_pending_action(dashboard_client, monkeypatch):
    _set_patch_pipeline_settings(monkeypatch)
    _stub_command_preview(monkeypatch)
    _pending_patch_action(patch_route.PATCH_BUILD_ACTION_ID, ActionStatus.EXECUTED.value)
    _login(dashboard_client)

    response = dashboard_client.post("/patch/propose-install", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        action = (
            session.query(Action)
            .filter(Action.action_id == patch_route.PATCH_INSTALL_ACTION_ID)
            .order_by(Action.created_at.desc())
            .first()
        )
        assert action is not None
        assert action.status == ActionStatus.PENDING_APPROVAL.value


def test_propose_install_rejects_duplicate_while_in_flight(dashboard_client, monkeypatch):
    _set_patch_pipeline_settings(monkeypatch)
    _stub_command_preview(monkeypatch)
    _pending_patch_action(patch_route.PATCH_BUILD_ACTION_ID, ActionStatus.EXECUTED.value)
    _pending_patch_action(patch_route.PATCH_INSTALL_ACTION_ID, ActionStatus.PENDING_APPROVAL.value)
    _login(dashboard_client)

    response = dashboard_client.post("/patch/propose-install")

    assert response.status_code == 409


def test_is_patch_install_pending_or_approved(dashboard_client):
    with db_module.SessionLocal() as session:
        assert patch_route.is_patch_install_pending_or_approved(session) is False

    _pending_patch_action(patch_route.PATCH_INSTALL_ACTION_ID, ActionStatus.PENDING_APPROVAL.value)

    with db_module.SessionLocal() as session:
        assert patch_route.is_patch_install_pending_or_approved(session) is True
