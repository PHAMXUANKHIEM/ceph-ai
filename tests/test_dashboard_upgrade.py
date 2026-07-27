import json
from datetime import datetime

from sqlalchemy.exc import OperationalError

import dashboard.routes.upgrade as upgrade_route
from config.settings import settings
from shared import db as db_module
from shared.models import Action, ActionStatus, Incident, IncidentStatus


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _set_cephadm(monkeypatch, mon_nodes="10.20.1.150"):
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(settings, "ceph_mon_nodes", mon_nodes)


def _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes=""):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    monkeypatch.setattr(settings, "ceph_mon_nodes", mon_nodes)
    monkeypatch.setattr(settings, "ceph_osd_nodes", osd_nodes)
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")


def _stub_package_command_preview(monkeypatch):
    """Avoids the real SSH round trip _safe_command_preview makes (unit
    discovery on the first target node) — worker/executor/commands.py's own
    builder tests already cover that command shape directly."""
    monkeypatch.setattr(upgrade_route, "_safe_command_preview", lambda action_id, host, params: "STUB_PREVIEW")


def _stub_no_versions_or_progress(monkeypatch):
    """Avoids the route's real SSH calls (summarize_cluster_versions/
    get_upgrade_status/run_ceph_json_command) in tests that don't care
    about their content — every dashboard-level test monkeypatches
    watcher.ceph_client functions at the name they were imported under in
    dashboard/routes/upgrade.py. run_ceph_json_command added 2026-07-27
    (build_upgrade_log_markdown's post-upgrade "ceph -s" summary) — without
    it, any test exercising an EXECUTED action's markdown log makes a REAL
    SSH attempt against whatever settings.ceph_mon_nodes happens to be
    (this project's real .env, verified live: took 6+ seconds and depended
    on real lab-cluster reachability)."""
    monkeypatch.setattr(upgrade_route, "summarize_cluster_versions", lambda: {
        "raw": {}, "per_type": {}, "distinct_versions": [], "is_mixed": False, "current_version": "18.2.4",
    })
    monkeypatch.setattr(upgrade_route, "propose_next_version", lambda v: "19.2.0")
    monkeypatch.setattr(upgrade_route, "get_upgrade_status", lambda: {"in_progress": False})
    monkeypatch.setattr(
        upgrade_route,
        "run_ceph_json_command",
        lambda inner_command: (
            "10.20.1.112",
            {
                "health": {"status": "HEALTH_OK"},
                "monmap": {"mons": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
                "mgrmap": {"active_name": "a", "standbys": [{"name": "b"}]},
                "osdmap": {"num_osds": 3, "num_up_osds": 3, "num_in_osds": 3},
                "pgmap": {"bytes_used": 900 * 1024**2, "bytes_total": 60 * 1024**3},
            },
        ),
    )


def test_unauthenticated_get_upgrade_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/upgrade", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_upgrade_handles_db_error_gracefully(dashboard_client, monkeypatch):
    # Regression: a pending Alembic migration (e.g. this feature's own
    # upgrade_procedure_documents table not yet created) used to surface as
    # a raw unhandled 500 here — every other DB error on this page was
    # already handled, this fetch just wasn't wrapped like index()'s is.
    _login(dashboard_client)

    def _broken_session_local():
        raise OperationalError("SELECT 1", {}, Exception("no such table: upgrade_procedure_documents"))

    monkeypatch.setattr(db_module, "SessionLocal", _broken_session_local)
    response = dashboard_client.get("/upgrade")

    assert response.status_code == 503


def test_get_upgrade_shows_not_supported_message_for_non_cephadm_cluster(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "docker")
    _login(dashboard_client)

    response = dashboard_client.get("/upgrade")

    assert response.status_code == 200
    assert "cephadm" in response.text


def test_get_upgrade_shows_current_version_and_suggested_target(dashboard_client, monkeypatch):
    _set_cephadm(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/upgrade")

    assert response.status_code == 200
    assert "18.2.4" in response.text
    assert 'value="19.2.0"' in response.text


def test_propose_upgrade_creates_pending_action_and_synthetic_incident(dashboard_client, monkeypatch):
    _set_cephadm(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/propose", data={"target_version": "19.2.0"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/upgrade"

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(action_id=upgrade_route.CLUSTER_UPGRADE_ACTION_ID).one()
        incident = session.get(Incident, action.incident_id)

    assert action.status == ActionStatus.PENDING_APPROVAL.value
    assert incident.ceph_code == upgrade_route.CLUSTER_UPGRADE_CEPH_CODE
    assert incident.status == IncidentStatus.PENDING_APPROVAL.value
    assert json.loads(action.action_params) == {"target_version": "19.2.0"}
    assert json.loads(action.target_nodes) == ["10.20.1.150"]
    assert action.proposed_command == "cephadm shell -- ceph orch upgrade start --ceph-version 19.2.0"
    assert "ceph orch upgrade start" in action.rationale


def test_propose_upgrade_rejects_malformed_target_version(dashboard_client, monkeypatch):
    _set_cephadm(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post("/upgrade/propose", data={"target_version": "not-a-version"})

    assert response.status_code == 400
    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(action_id=upgrade_route.CLUSTER_UPGRADE_ACTION_ID).count() == 0


def test_propose_upgrade_rejects_when_not_cephadm(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "docker")
    _login(dashboard_client)

    response = dashboard_client.post("/upgrade/propose", data={"target_version": "19.2.0"})

    assert response.status_code == 400


def test_propose_upgrade_rejects_second_proposal_while_one_already_pending(dashboard_client, monkeypatch):
    _set_cephadm(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)

    first = dashboard_client.post(
        "/upgrade/propose", data={"target_version": "19.2.0"}, follow_redirects=False
    )
    assert first.status_code == 303

    second = dashboard_client.post("/upgrade/propose", data={"target_version": "19.2.1"})

    assert second.status_code == 409
    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(action_id=upgrade_route.CLUSTER_UPGRADE_ACTION_ID).count() == 1


def test_pending_upgrade_shows_plan_and_approve_reject_buttons(dashboard_client, monkeypatch):
    _set_cephadm(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/upgrade/propose", data={"target_version": "19.2.0"})

    response = dashboard_client.get("/upgrade")

    assert response.status_code == 200
    assert "ceph orch upgrade start" in response.text
    assert "/approve" in response.text
    assert "/reject" in response.text
    # No second propose form while one is pending.
    assert 'action="/upgrade/propose"' not in response.text


def test_approving_pending_upgrade_via_existing_actions_route_marks_approved(dashboard_client, monkeypatch):
    """The Cluster Upgrade feature deliberately adds NO new approve/reject
    route — POST /actions/{id}/approve (Story 4.3, unchanged) is what the
    admin actually clicks, exactly like any other RISKY action."""
    _set_cephadm(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/upgrade/propose", data={"target_version": "19.2.0"})

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(action_id=upgrade_route.CLUSTER_UPGRADE_ACTION_ID).one()
        action_id = action.id

    response = dashboard_client.post(f"/actions/{action_id}/approve", follow_redirects=False)
    assert response.status_code == 303

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        incident = session.get(Incident, action.incident_id)
    assert action.status == ActionStatus.APPROVED.value
    assert incident.status == IncidentStatus.APPROVED.value


def test_rejecting_pending_upgrade_allows_a_new_proposal(dashboard_client, monkeypatch):
    _set_cephadm(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/upgrade/propose", data={"target_version": "19.2.0"})

    with db_module.SessionLocal() as session:
        action_id = (
            session.query(Action).filter_by(action_id=upgrade_route.CLUSTER_UPGRADE_ACTION_ID).one().id
        )
    dashboard_client.post(f"/actions/{action_id}/reject")

    # A rejected upgrade is no longer "pending" — the propose form must
    # reappear so the operator can try again (e.g. a different target).
    response = dashboard_client.get("/upgrade")
    assert 'action="/upgrade/propose"' in response.text

    second = dashboard_client.post(
        "/upgrade/propose", data={"target_version": "19.2.0"}, follow_redirects=False
    )
    assert second.status_code == 303
    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(action_id=upgrade_route.CLUSTER_UPGRADE_ACTION_ID).count() == 2


def test_upgrade_incident_excluded_from_cluster_health_status(dashboard_client, monkeypatch):
    """Mirrors the existing CHAT_REQUEST fix (dashboard/routes/incidents.py) —
    a rejected/failed cluster-upgrade Incident must not flip the dashboard's
    cluster-wide health badge to ERR/WARN."""
    from dashboard.routes.incidents import compute_cluster_status

    incident = Incident(
        ceph_code=upgrade_route.CLUSTER_UPGRADE_CEPH_CODE,
        status=IncidentStatus.FAILED.value,
        severity="HEALTH_ERR",
        detected_at=datetime.utcnow(),
    )
    assert compute_cluster_status([incident], heartbeat_stale=False) == "OK"


def test_pause_upgrade_route_calls_pause_and_audits(dashboard_client, monkeypatch):
    _set_cephadm(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/upgrade/propose", data={"target_version": "19.2.0"})

    calls = []
    monkeypatch.setattr(upgrade_route, "pause_upgrade", lambda: calls.append("paused"))

    response = dashboard_client.post("/upgrade/pause", follow_redirects=False)

    assert response.status_code == 303
    assert calls == ["paused"]


def test_resume_upgrade_route_calls_resume(dashboard_client, monkeypatch):
    _set_cephadm(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/upgrade/propose", data={"target_version": "19.2.0"})

    calls = []
    monkeypatch.setattr(upgrade_route, "resume_upgrade", lambda: calls.append("resumed"))

    response = dashboard_client.post("/upgrade/resume", follow_redirects=False)

    assert response.status_code == 303
    assert calls == ["resumed"]


# --- is_cluster_upgrade_pending_or_approved / is_cluster_upgrade_physically_running --


def test_is_cluster_upgrade_pending_or_approved_true_for_pending_approval(dashboard_client):
    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code=upgrade_route.CLUSTER_UPGRADE_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        session.add(
            Action(
                incident_id=incident.id,
                action_id=upgrade_route.CLUSTER_UPGRADE_ACTION_ID,
                classification="RISKY",
                status=ActionStatus.PENDING_APPROVAL.value,
            )
        )
        session.commit()

        assert upgrade_route.is_cluster_upgrade_pending_or_approved(session) is True


def test_is_cluster_upgrade_pending_or_approved_true_for_approved(dashboard_client):
    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code=upgrade_route.CLUSTER_UPGRADE_CEPH_CODE,
            status=IncidentStatus.APPROVED.value,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        session.add(
            Action(
                incident_id=incident.id,
                action_id=upgrade_route.CLUSTER_UPGRADE_ACTION_ID,
                classification="RISKY",
                status=ActionStatus.APPROVED.value,
            )
        )
        session.commit()

        assert upgrade_route.is_cluster_upgrade_pending_or_approved(session) is True


def test_is_cluster_upgrade_pending_or_approved_false_when_resolved(dashboard_client):
    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code=upgrade_route.CLUSTER_UPGRADE_CEPH_CODE,
            status=IncidentStatus.RESOLVED.value,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        session.add(
            Action(
                incident_id=incident.id,
                action_id=upgrade_route.CLUSTER_UPGRADE_ACTION_ID,
                classification="RISKY",
                status=ActionStatus.EXECUTED.value,
            )
        )
        session.commit()

        assert upgrade_route.is_cluster_upgrade_pending_or_approved(session) is False


def test_is_cluster_upgrade_pending_or_approved_false_when_no_upgrade_ever_proposed(dashboard_client):
    with db_module.SessionLocal() as session:
        assert upgrade_route.is_cluster_upgrade_pending_or_approved(session) is False


def test_is_cluster_upgrade_physically_running_false_when_not_cephadm(monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "docker")
    assert upgrade_route.is_cluster_upgrade_physically_running() is False


def test_is_cluster_upgrade_physically_running_reflects_live_status(monkeypatch):
    _set_cephadm(monkeypatch)
    monkeypatch.setattr(upgrade_route, "get_upgrade_status", lambda: {"in_progress": True})
    assert upgrade_route.is_cluster_upgrade_physically_running() is True

    monkeypatch.setattr(upgrade_route, "get_upgrade_status", lambda: {"in_progress": False})
    assert upgrade_route.is_cluster_upgrade_physically_running() is False


def test_is_cluster_upgrade_physically_running_fails_open_on_query_error(monkeypatch):
    _set_cephadm(monkeypatch)

    def fake_get_upgrade_status():
        raise upgrade_route.CephQueryError("cluster unreachable")

    monkeypatch.setattr(upgrade_route, "get_upgrade_status", fake_get_upgrade_status)

    assert upgrade_route.is_cluster_upgrade_physically_running() is False


# --- ceph-deploy / package-based upgrade paths ------------------------------


def test_get_upgrade_shows_package_based_sections_for_none_exec_mode(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/upgrade")

    assert response.status_code == 200
    assert "ceph-deploy" in response.text
    assert 'action="/upgrade/propose-package-download"' in response.text
    assert 'action="/upgrade/propose-package-local"' in response.text
    # cephadm-only sections must not appear for this exec mode.
    assert 'action="/upgrade/propose"' not in response.text
    assert "Tiến độ nâng cấp (trực tiếp từ cụm)" not in response.text


def test_propose_package_download_creates_pending_action(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    _stub_no_versions_or_progress(monkeypatch)
    _stub_package_command_preview(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/propose-package-download",
        data={"target_version": "19.2.0"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(
            action_id=upgrade_route.PACKAGE_DOWNLOAD_ACTION_ID
        ).one()
        incident = session.get(Incident, action.incident_id)

    assert action.status == ActionStatus.PENDING_APPROVAL.value
    assert incident.ceph_code == upgrade_route.CLUSTER_UPGRADE_CEPH_CODE
    assert json.loads(action.action_params) == {"target_version": "19.2.0"}
    assert json.loads(action.target_nodes) == ["10.20.1.150", "10.20.1.83"]
    assert "squid" in action.rationale
    assert "download.ceph.com" in action.rationale
    assert action.proposed_command == "STUB_PREVIEW"


def test_propose_package_download_rejects_when_cephadm(dashboard_client, monkeypatch):
    _set_cephadm(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/propose-package-download", data={"target_version": "19.2.0"}
    )

    assert response.status_code == 400


def test_propose_package_download_rejects_unknown_codename(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/propose-package-download", data={"target_version": "99.0.0"}
    )

    assert response.status_code == 400


def test_propose_package_download_rejects_malformed_version(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/propose-package-download", data={"target_version": "not-a-version"}
    )

    assert response.status_code == 400


def test_propose_package_download_requires_configured_nodes(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/propose-package-download", data={"target_version": "19.2.0"}
    )

    assert response.status_code == 400


def test_propose_package_local_creates_pending_action(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _stub_package_command_preview(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/propose-package-local",
        data={"package_dir": "/opt/ceph-packages"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(
            action_id=upgrade_route.PACKAGE_LOCAL_ACTION_ID
        ).one()

    assert action.status == ActionStatus.PENDING_APPROVAL.value
    assert json.loads(action.action_params) == {"package_dir": "/opt/ceph-packages"}
    assert "/opt/ceph-packages" in action.rationale
    assert action.proposed_command == "STUB_PREVIEW"


def test_propose_package_local_rejects_relative_path(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/propose-package-local", data={"package_dir": "relative/path"}
    )

    assert response.status_code == 400


def test_propose_package_local_rejects_when_cephadm(dashboard_client, monkeypatch):
    _set_cephadm(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/propose-package-local", data={"package_dir": "/opt/ceph-packages"}
    )

    assert response.status_code == 400


def test_propose_package_download_and_cephadm_share_the_same_duplicate_proposal_gate(
    dashboard_client, monkeypatch
):
    # A pending package-download proposal must block a NEW cephadm-style
    # proposal too (and vice versa) — is_cluster_upgrade_pending_or_approved
    # checks across all 3 action_ids, not just one.
    _set_package_deploy(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _stub_package_command_preview(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/upgrade/propose-package-download", data={"target_version": "19.2.0"})

    with db_module.SessionLocal() as session:
        assert upgrade_route.is_cluster_upgrade_pending_or_approved(session) is True

    second = dashboard_client.post(
        "/upgrade/propose-package-local", data={"package_dir": "/opt/ceph-packages"}
    )
    assert second.status_code == 409


def test_pending_package_download_action_shown_on_upgrade_page(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _stub_package_command_preview(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/upgrade/propose-package-download", data={"target_version": "19.2.0"})

    response = dashboard_client.get("/upgrade")

    assert response.status_code == 200
    assert "ceph-deploy — tải từ download.ceph.com" in response.text
    assert "19.2.0" in response.text


def test_approved_package_download_action_shows_per_host_progress(
    dashboard_client, monkeypatch
):
    _set_package_deploy(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)

    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code=upgrade_route.CLUSTER_UPGRADE_CEPH_CODE,
            status=IncidentStatus.APPROVED.value,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        session.add(
            Action(
                incident_id=incident.id,
                action_id="upgrade_ceph_cluster_package_download",
                classification="RISKY",
                status=ActionStatus.APPROVED.value,
                action_params=json.dumps({"target_version": "19.2.0"}),
                execution_progress=json.dumps(
                    [
                        {"host": "10.20.1.112", "status": "done"},
                        {"host": "10.20.1.95", "status": "running"},
                        {"host": "10.20.1.21", "status": "pending"},
                    ]
                ),
            )
        )
        session.commit()

    response = dashboard_client.get("/upgrade")

    assert response.status_code == 200
    assert "10.20.1.112" in response.text
    assert "Xong" in response.text
    assert "Đang chạy" in response.text
    assert "Đang chờ" in response.text
    # Auto-refresh while the action is still APPROVED (in-flight) — no other
    # way for an operator watching the page to see progress land without
    # manually reloading, since a real install can take minutes per host.
    assert 'http-equiv="refresh"' in response.text


# --- Nhật ký nâng cấp (Markdown step log) ------------------------------


def _resolved_package_action(session, status, host_results):
    """Creates an already-resolved (EXECUTED/FAILED) Cluster Upgrade Action
    with a rich execution_progress list — same shape
    worker/llm/router_client.py::_execute_approved_action now writes
    (command/started_at/finished_at/error alongside host/status)."""
    incident = Incident(
        ceph_code=upgrade_route.CLUSTER_UPGRADE_CEPH_CODE,
        status=IncidentStatus.RESOLVED.value if status == "EXECUTED" else IncidentStatus.FAILED.value,
        log_excerpt="Đề xuất nâng cấp cụm (ceph-deploy, tải từ download.ceph.com) lên 19.2.0 bởi admin",
        detected_at=datetime.utcnow(),
    )
    session.add(incident)
    session.flush()
    action = Action(
        incident_id=incident.id,
        action_id="upgrade_ceph_cluster_package_download",
        classification="RISKY",
        status=status,
        rationale="Quy trình mẫu — bước 1, bước 2.",
        action_params=json.dumps({"target_version": "19.2.0"}),
        execution_progress=json.dumps(host_results),
    )
    session.add(action)
    session.commit()
    return action


def test_upgrade_page_shows_markdown_log_after_resolved_action(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)

    with db_module.SessionLocal() as session:
        _resolved_package_action(
            session,
            "FAILED",
            [
                {
                    "host": "10.20.1.112",
                    "status": "done",
                    "command": "apt-get install -y ceph",
                    "started_at": "2026-07-27T09:24:18",
                    "finished_at": "2026-07-27T09:26:05",
                },
                {
                    "host": "10.20.1.95",
                    "status": "failed",
                    "command": "apt-get install -y ceph",
                    "started_at": "2026-07-27T09:26:05",
                    "finished_at": "2026-07-27T09:26:10",
                    "error": "10.20.1.95: command exited 1: E: Unable to locate package ceph",
                },
            ],
        )

    response = dashboard_client.get("/upgrade")

    assert response.status_code == 200
    assert "Nhật ký nâng cấp gần nhất" in response.text
    assert "10.20.1.112" in response.text
    assert "10.20.1.95" in response.text
    assert "E: Unable to locate package ceph" in response.text
    assert "/upgrade/log.md" in response.text


def test_upgrade_page_hides_markdown_log_while_pending(dashboard_client, monkeypatch):
    """The log card is only meaningful once the Action has actually
    resolved — while PENDING_APPROVAL/APPROVED, execution_progress is
    either empty or still changing, and the per-node list already visible
    above (test_approved_package_download_action_shows_per_host_progress)
    covers that case."""
    _set_package_deploy(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)

    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code=upgrade_route.CLUSTER_UPGRADE_CEPH_CODE,
            status=IncidentStatus.APPROVED.value,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        session.add(
            Action(
                incident_id=incident.id,
                action_id="upgrade_ceph_cluster_package_download",
                classification="RISKY",
                status=ActionStatus.APPROVED.value,
                action_params=json.dumps({"target_version": "19.2.0"}),
                execution_progress=json.dumps([{"host": "10.20.1.112", "status": "running"}]),
            )
        )
        session.commit()

    response = dashboard_client.get("/upgrade")

    assert response.status_code == 200
    assert "Nhật ký nâng cấp gần nhất" not in response.text


def test_download_upgrade_log_returns_markdown_file(dashboard_client, monkeypatch):
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)

    with db_module.SessionLocal() as session:
        _resolved_package_action(
            session,
            "EXECUTED",
            [
                {
                    "host": "10.20.1.112",
                    "status": "done",
                    "command": "apt-get install -y ceph",
                    "started_at": "2026-07-27T09:24:18",
                    "finished_at": "2026-07-27T09:26:05",
                }
            ],
        )

    response = dashboard_client.get("/upgrade/log.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert 'attachment; filename="nhat-ky-nang-cap-cum.md"' in response.headers["content-disposition"]
    assert "# Nhật ký nâng cấp cụm Ceph" in response.text
    assert "10.20.1.112" in response.text
    assert "apt-get install -y ceph" in response.text
    # 2026-07-27: post-upgrade summary section (operator request) — only
    # for a resolved EXECUTED action, live "ceph -s" snapshot + duration.
    assert "## Tóm tắt cụm sau nâng cấp" in response.text
    assert "Ceph 18.2.4" in response.text  # current_version from the stub
    assert "HEALTH_OK" in response.text
    assert "MON:** 3 node" in response.text
    assert "active `a`, standby: b" in response.text
    assert "3 osd, 3 up, 3 in" in response.text
    assert "0.9 GiB / 60.0 GiB" in response.text
    assert "phút" in response.text and "giây" in response.text


def test_upgrade_summary_omitted_for_non_executed_actions(dashboard_client, monkeypatch):
    """The post-upgrade summary only makes sense once the upgrade actually
    finished — a PENDING/APPROVED/FAILED action has no "after" state to
    summarize, and querying live cluster status for a FAILED upgrade could
    misleadingly look like a success report."""
    _set_package_deploy(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)

    with db_module.SessionLocal() as session:
        _resolved_package_action(session, "FAILED", [{"host": "10.20.1.112", "status": "failed"}])

    response = dashboard_client.get("/upgrade/log.md")

    assert response.status_code == 200
    assert "## Tóm tắt cụm sau nâng cấp" not in response.text


def test_download_upgrade_log_404_when_no_action_ever_proposed(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/upgrade/log.md")

    assert response.status_code == 404


def test_unauthenticated_download_upgrade_log_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/upgrade/log.md", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
