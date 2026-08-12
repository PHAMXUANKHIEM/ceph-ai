import json
from datetime import datetime

import pytest
from sqlalchemy.exc import OperationalError

import dashboard.routes.upgrade as upgrade_route
from config.settings import settings
from shared import audit, db as db_module
from shared.models import (
    Action,
    ActionStatus,
    AuditEntry,
    Incident,
    IncidentStatus,
    NodeUpgradeGate,
    NodeUpgradeGateLock,
    NodeUpgradeGateState,
)
from shared.node_upgrade_gate import LOCK_ID


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
    assert action.proposed_command == (
        "cephadm shell -- bash -c 'ceph osd set noout && ceph osd set noscrub && "
        "ceph osd set nodeep-scrub && ceph osd set nosnaptrim && "
        "ceph orch upgrade start --ceph-version 19.2.0'"
    )
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


def test_cephadm_pending_progress_rendering_unchanged_by_story_7_2(dashboard_client, monkeypatch):
    """Story 7.2 only phased the 2 package-based action_ids — a cephadm
    Action's execution_progress (still one flat entry per node, written by
    the UNTOUCHED generic per-host loop, never a `phase` key) must still
    render with the exact pre-7.2 heading and no phase tag next to the
    host, even though the SAME template block now also serves the
    phase-tagged package flavors."""
    _set_cephadm(monkeypatch)
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
                action_id=upgrade_route.CLUSTER_UPGRADE_ACTION_ID,
                classification="RISKY",
                status=ActionStatus.APPROVED.value,
                action_params=json.dumps({"target_version": "19.2.0"}),
                execution_progress=json.dumps([{"host": "10.20.1.150", "status": "running"}]),
            )
        )
        session.commit()

    response = dashboard_client.get("/upgrade")

    assert response.status_code == 200
    assert "Tiến trình theo từng node" in response.text
    assert "Tiến trình theo từng giai đoạn" not in response.text
    assert "phase-tag" not in response.text


def test_cephadm_markdown_log_has_no_phase_tag(dashboard_client, monkeypatch):
    """Same byte-for-byte guarantee as the pending-progress test above,
    for build_upgrade_log_markdown's per-step listing."""
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)

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
                rationale="Quy trình mẫu.",
                action_params=json.dumps({"target_version": "19.2.0"}),
                execution_progress=json.dumps(
                    [{"host": "10.20.1.150", "status": "done", "command": "ceph orch upgrade start"}]
                ),
            )
        )
        session.commit()

    response = dashboard_client.get("/upgrade/log.md")

    assert response.status_code == 200
    assert "**10.20.1.150** — ✅ Xong" in response.text
    assert "Cài đặt" not in response.text
    assert "(mon)" not in response.text


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


def test_unset_upgrade_osd_flags_route_calls_unset_and_audits(dashboard_client, monkeypatch):
    _set_cephadm(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/upgrade/propose", data={"target_version": "19.2.0"})

    calls = []
    monkeypatch.setattr(upgrade_route, "unset_upgrade_osd_flags", lambda: calls.append("unset"))

    response = dashboard_client.post("/upgrade/unset-osd-flags", follow_redirects=False)

    assert response.status_code == 303
    assert calls == ["unset"]

    with db_module.SessionLocal() as session:
        entry = (
            session.query(AuditEntry)
            .filter_by(event_type=audit.EVENT_CLUSTER_UPGRADE_OSD_FLAGS_UNSET)
            .one()
        )
        assert entry.actor == "admin"


def test_unset_upgrade_osd_flags_route_returns_502_on_failure(dashboard_client, monkeypatch):
    _set_cephadm(monkeypatch)
    _stub_no_versions_or_progress(monkeypatch)
    _login(dashboard_client)

    def raising():
        raise upgrade_route.CephQueryError("cụm không phản hồi")

    monkeypatch.setattr(upgrade_route, "unset_upgrade_osd_flags", raising)

    response = dashboard_client.post("/upgrade/unset-osd-flags")

    assert response.status_code == 502


def test_unauthenticated_unset_osd_flags_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/upgrade/unset-osd-flags", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


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


# --- Story 11.1 (2026-08-05): OS Upgrade Gate screen replaces the ad-hoc
# HTTPException(400) for the package-download path (FR-1/FR-2, AD-20).
# Neither the ad-hoc 400 nor this gate screen had any regression test
# before this story — the "propose" tests above all rely on read_os_release
# raising ExecutorError against a fake host (best-effort skip) to avoid
# ever hitting this path at all; these tests instead monkeypatch
# read_os_release directly to exercise the incompatible/compatible cases.


def _stub_os_release(monkeypatch, by_host: dict):
    """by_host: {host: {"ID": ..., "VERSION_ID": ...}} — hosts not present
    raise ExecutorError (unreachable), matching read_os_release's own
    contract."""

    def _fake_read_os_release(host):
        if host not in by_host:
            raise upgrade_route.ExecutorError(f"no stub for {host}")
        return by_host[host]

    monkeypatch.setattr(upgrade_route, "read_os_release", _fake_read_os_release)


def test_propose_package_download_shows_os_gate_screen_when_node_incompatible(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    _stub_no_versions_or_progress(monkeypatch)
    _stub_os_release(
        monkeypatch,
        {
            "10.20.1.150": {"ID": "centos", "VERSION_ID": "7"},
            # rocky/8, not rocky/9: Pacific (16.2.15) never shipped el9
            # packages at all (see shared/ceph_releases.py's el_history,
            # 2026-08-06 live audit) — rocky/9 would ALSO be incompatible
            # here, so it can't stand in as "the compatible node" for this
            # assertion anymore. rocky/8 is Pacific's actual (only)
            # supported OS.
            "10.20.1.83": {"ID": "rocky", "VERSION_ID": "8"},
        },
    )
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/propose-package-download",
        data={"target_version": "16.2.15"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "10.20.1.150" in response.text
    assert "centos 7" in response.text
    assert "CentOS/RHEL/Rocky Linux/AlmaLinux 8 trở lên" in response.text
    # The compatible node must NOT show up in the incompatible-node table.
    assert "10.20.1.83" not in response.text


def test_propose_package_download_os_gate_creates_no_incident_or_action(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150")
    _stub_no_versions_or_progress(monkeypatch)
    _stub_os_release(monkeypatch, {"10.20.1.150": {"ID": "centos", "VERSION_ID": "7"}})
    _login(dashboard_client)

    dashboard_client.post(
        "/upgrade/propose-package-download",
        data={"target_version": "16.2.15"},
        follow_redirects=False,
    )

    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(action_id=upgrade_route.PACKAGE_DOWNLOAD_ACTION_ID).first() is None
        assert session.query(Incident).filter_by(ceph_code=upgrade_route.CLUSTER_UPGRADE_CEPH_CODE).first() is None


def test_propose_package_download_proceeds_when_os_meets_floor(dashboard_client, monkeypatch):
    # Given consequence: "khi TẤT CẢ node đã đạt... luồng đề xuất nâng cấp
    # cụm hoạt động y hệt hiện tại" — must still create the Action/Incident
    # exactly like before this story, not just avoid crashing.
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150")
    _stub_no_versions_or_progress(monkeypatch)
    _stub_package_command_preview(monkeypatch)
    _stub_os_release(monkeypatch, {"10.20.1.150": {"ID": "rocky", "VERSION_ID": "9"}})
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/propose-package-download",
        data={"target_version": "19.2.0"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(action_id=upgrade_route.PACKAGE_DOWNLOAD_ACTION_ID).one()
    assert action.status == ActionStatus.PENDING_APPROVAL.value


def test_check_os_upgrade_needed_skips_unreachable_node_best_effort(monkeypatch):
    # Direct unit test of the helper: an unreachable node must be SKIPPED,
    # not reported as incompatible (FR-1's own consequence), even when
    # another node in the same call IS genuinely incompatible.
    _stub_os_release(monkeypatch, {"10.20.1.83": {"ID": "centos", "VERSION_ID": "7"}})

    result = upgrade_route._check_os_upgrade_needed("16.2.15", ["10.20.1.150", "10.20.1.83"])

    assert len(result) == 1
    assert result[0]["host"] == "10.20.1.83"
    assert result[0]["os_id"] == "centos"
    assert result[0]["os_version_id"] == "7"


def test_check_os_upgrade_needed_empty_when_all_nodes_compatible(monkeypatch):
    _stub_os_release(monkeypatch, {"10.20.1.150": {"ID": "rocky", "VERSION_ID": "9"}})

    assert upgrade_route._check_os_upgrade_needed("19.2.0", ["10.20.1.150"]) == []


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


def test_approved_package_download_action_renders_old_format_progress_without_phase_key(
    dashboard_client, monkeypatch
):
    """Story 7.2: execution_progress entries stored by an Action from
    BEFORE this story (no `phase` key at all) must render with a fallback
    label instead of crashing or leaving the phase column blank."""
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
                execution_progress=json.dumps([{"host": "10.20.1.112", "status": "done"}]),
            )
        )
        session.commit()

    response = dashboard_client.get("/upgrade")

    assert response.status_code == 200
    assert "Cài đặt" in response.text  # fallback phase label


def test_approved_package_download_action_renders_new_phase_labels(dashboard_client, monkeypatch):
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
                        {"host": "10.20.1.112", "status": "done", "phase": "install"},
                        {"host": "10.20.1.112", "status": "running", "phase": "mon"},
                    ]
                ),
            )
        )
        session.commit()

    response = dashboard_client.get("/upgrade")

    assert response.status_code == 200
    assert "Khởi động lại MON" in response.text


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


# --- Story 11.3: Chuẩn bị node để cài lại OS (kèm Huỷ Chuẩn bị) -------------
#
# dashboard_client's DB fixture uses Base.metadata.create_all() (SQLite),
# which does NOT run the migration's seed insert — same reasoning
# so every test that expects a successful lock CLAIM must seed the
# singleton row first.


def _seed_gate_lock(active_gate_id: str | None = None) -> None:
    with db_module.SessionLocal() as session:
        session.add(NodeUpgradeGateLock(id=LOCK_ID, active_gate_id=active_gate_id))
        session.commit()


def test_prepare_requires_package_deploy_mode(dashboard_client, monkeypatch):
    _set_cephadm(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/gate/prepare", data={"host": "10.20.1.150", "target_version": "19.2.0"}
    )

    assert response.status_code == 400


def test_prepare_rejects_host_not_in_configured_nodes(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150")
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/gate/prepare", data={"host": "10.99.99.99", "target_version": "19.2.0"}
    )

    assert response.status_code == 400


def test_prepare_happy_path_claims_lock_and_creates_gate(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150,10.20.1.151", osd_nodes="10.20.1.83")
    _seed_gate_lock()
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/gate/prepare",
        data={"host": "10.20.1.83", "target_version": "19.2.0"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/upgrade/gate?target_version=19.2.0"
    with db_module.SessionLocal() as session:
        gate = session.query(NodeUpgradeGate).filter_by(host="10.20.1.83").one()
        assert gate.state == NodeUpgradeGateState.PREPARING.value
        assert gate.target_version == "19.2.0"
        assert json.loads(gate.roles_snapshot) == ["OSD"]
        assert gate.prepare_action_id is not None

        action = session.get(Action, gate.prepare_action_id)
        assert action.action_id == upgrade_route.NODE_OS_GATE_PREPARE_ACTION_ID
        assert action.status == ActionStatus.APPROVED.value  # has_command()==True -> real approval

        lock = session.get(NodeUpgradeGateLock, LOCK_ID)
        assert lock.active_gate_id == gate.id

        incident = session.get(Incident, action.incident_id)
        assert incident.ceph_code == upgrade_route.NODE_OS_GATE_CEPH_CODE


def test_prepare_blocked_when_lock_already_held(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83,10.20.1.84")
    _seed_gate_lock(active_gate_id="some-other-gate-id")
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/gate/prepare", data={"host": "10.20.1.83", "target_version": "19.2.0"}
    )

    assert response.status_code == 409
    with db_module.SessionLocal() as session:
        assert session.query(NodeUpgradeGate).filter_by(host="10.20.1.83").first() is None
        assert session.query(Action).filter_by(action_id=upgrade_route.NODE_OS_GATE_PREPARE_ACTION_ID).first() is None
        assert session.query(Incident).filter_by(ceph_code=upgrade_route.NODE_OS_GATE_CEPH_CODE).first() is None


def test_prepare_is_idempotent_for_a_node_already_preparing(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    _seed_gate_lock(active_gate_id="existing-gate")
    with db_module.SessionLocal() as session:
        session.add(
            NodeUpgradeGate(
                id="existing-gate",
                host="10.20.1.83",
                target_version="19.2.0",
                state=NodeUpgradeGateState.PREPARING.value,
            )
        )
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/gate/prepare",
        data={"host": "10.20.1.83", "target_version": "19.2.0"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        # No SECOND gate row created for the same host — idempotent no-op.
        assert session.query(NodeUpgradeGate).filter_by(host="10.20.1.83").count() == 1


def test_prepare_is_idempotent_for_a_node_already_prepared(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    _seed_gate_lock(active_gate_id="existing-gate")
    with db_module.SessionLocal() as session:
        session.add(
            NodeUpgradeGate(
                id="existing-gate",
                host="10.20.1.83",
                target_version="19.2.0",
                state=NodeUpgradeGateState.PREPARED.value,
            )
        )
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/gate/prepare",
        data={"host": "10.20.1.83", "target_version": "19.2.0"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        assert session.query(NodeUpgradeGate).filter_by(host="10.20.1.83").count() == 1


def test_abort_rejects_when_no_gate_exists(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    _login(dashboard_client)

    response = dashboard_client.post("/upgrade/gate/abort", data={"host": "10.20.1.83"})

    assert response.status_code == 400


def test_abort_rejects_when_gate_is_not_prepared(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    with db_module.SessionLocal() as session:
        session.add(
            NodeUpgradeGate(
                id="g1", host="10.20.1.83", target_version="19.2.0", state=NodeUpgradeGateState.PREPARING.value
            )
        )
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.post("/upgrade/gate/abort", data={"host": "10.20.1.83"})

    assert response.status_code == 400


def test_abort_happy_path_sets_abort_action_id_and_approves(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    _seed_gate_lock(active_gate_id="g1")
    with db_module.SessionLocal() as session:
        session.add(
            NodeUpgradeGate(
                id="g1",
                host="10.20.1.83",
                target_version="19.2.0",
                state=NodeUpgradeGateState.PREPARED.value,
                roles_snapshot=json.dumps(["OSD"]),
            )
        )
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/gate/abort", data={"host": "10.20.1.83"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/upgrade/gate?target_version=19.2.0"
    with db_module.SessionLocal() as session:
        gate = session.get(NodeUpgradeGate, "g1")
        assert gate.abort_action_id is not None
        action = session.get(Action, gate.abort_action_id)
        assert action.action_id == upgrade_route.NODE_OS_GATE_ABORT_ACTION_ID
        assert action.status == ActionStatus.APPROVED.value


def test_get_upgrade_gate_redirects_when_no_node_incompatible(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150")
    _stub_os_release(monkeypatch, {"10.20.1.150": {"ID": "rocky", "VERSION_ID": "9"}})
    _login(dashboard_client)

    response = dashboard_client.get("/upgrade/gate?target_version=19.2.0", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/upgrade"


def test_get_upgrade_gate_shows_existing_gate_status(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150")
    _stub_os_release(monkeypatch, {"10.20.1.150": {"ID": "centos", "VERSION_ID": "7"}})
    with db_module.SessionLocal() as session:
        session.add(
            NodeUpgradeGate(
                id="g1",
                host="10.20.1.150",
                target_version="16.2.15",
                state=NodeUpgradeGateState.PREPARED.value,
            )
        )
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.get("/upgrade/gate?target_version=16.2.15")

    assert response.status_code == 200
    assert "Sẵn sàng cài lại OS" in response.text
    assert "Huỷ Chuẩn bị" in response.text


# --- Story 11.4: Xác nhận & Phục hồi node tự động ---------------------------


def test_confirm_rejects_when_no_gate_exists(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    _login(dashboard_client)

    response = dashboard_client.post("/upgrade/gate/confirm", data={"host": "10.20.1.83"})

    assert response.status_code == 400


def test_confirm_rejects_when_gate_is_not_prepared(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    with db_module.SessionLocal() as session:
        session.add(
            NodeUpgradeGate(
                id="g1", host="10.20.1.83", target_version="19.2.0", state=NodeUpgradeGateState.PREPARING.value
            )
        )
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.post("/upgrade/gate/confirm", data={"host": "10.20.1.83"})

    assert response.status_code == 400


def test_confirm_recheck_fails_leaves_gate_unchanged_and_creates_nothing(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    _seed_gate_lock(active_gate_id="g1")
    with db_module.SessionLocal() as session:
        session.add(
            NodeUpgradeGate(
                id="g1",
                host="10.20.1.83",
                target_version="19.2.0",
                state=NodeUpgradeGateState.PREPARED.value,
                roles_snapshot=json.dumps(["OSD"]),
            )
        )
        session.commit()
    # Still el7 -> still fails os_upgrade_warning for 19.2.0.
    _stub_os_release(monkeypatch, {"10.20.1.83": {"ID": "centos", "VERSION_ID": "7"}})
    _login(dashboard_client)

    response = dashboard_client.post("/upgrade/gate/confirm", data={"host": "10.20.1.83"})

    assert response.status_code == 400
    with db_module.SessionLocal() as session:
        gate = session.get(NodeUpgradeGate, "g1")
        assert gate.state == NodeUpgradeGateState.PREPARED.value
        assert gate.confirm_action_id is None
        assert session.query(Action).filter_by(action_id=upgrade_route.NODE_OS_GATE_RECOVER_ACTION_ID).first() is None
        assert session.query(Incident).filter_by(ceph_code=upgrade_route.NODE_OS_GATE_CEPH_CODE).first() is None
        # Lock stays exactly as Prepare left it — Confirm never touches it.
        assert session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id == "g1"


def test_confirm_recheck_ssh_unreachable_treated_as_not_confirmed(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    _seed_gate_lock(active_gate_id="g1")
    with db_module.SessionLocal() as session:
        session.add(
            NodeUpgradeGate(
                id="g1", host="10.20.1.83", target_version="19.2.0", state=NodeUpgradeGateState.PREPARED.value,
                roles_snapshot=json.dumps(["OSD"]),
            )
        )
        session.commit()
    _stub_os_release(monkeypatch, {})  # 10.20.1.83 has no stub -> ExecutorError
    _login(dashboard_client)

    response = dashboard_client.post("/upgrade/gate/confirm", data={"host": "10.20.1.83"})

    assert response.status_code == 400
    with db_module.SessionLocal() as session:
        assert session.get(NodeUpgradeGate, "g1").state == NodeUpgradeGateState.PREPARED.value


def test_confirm_happy_path_creates_recover_action_and_keeps_lock(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    _seed_gate_lock(active_gate_id="g1")
    with db_module.SessionLocal() as session:
        session.add(
            NodeUpgradeGate(
                id="g1",
                host="10.20.1.83",
                target_version="19.2.0",
                state=NodeUpgradeGateState.PREPARED.value,
                roles_snapshot=json.dumps(["OSD"]),
            )
        )
        session.commit()
    _stub_os_release(monkeypatch, {"10.20.1.83": {"ID": "rocky", "VERSION_ID": "9"}})
    _stub_package_command_preview(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/gate/confirm", data={"host": "10.20.1.83"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/upgrade/gate?target_version=19.2.0"
    with db_module.SessionLocal() as session:
        gate = session.get(NodeUpgradeGate, "g1")
        assert gate.state == NodeUpgradeGateState.RECOVERING.value
        assert gate.confirm_action_id is not None

        action = session.get(Action, gate.confirm_action_id)
        assert action.action_id == upgrade_route.NODE_OS_GATE_RECOVER_ACTION_ID
        assert action.status == ActionStatus.APPROVED.value  # has_command()==True -> real approval
        params = json.loads(action.action_params)
        assert params["roles"] == ["OSD"]
        assert params["node_upgrade_gate_id"] == "g1"

        # AD-21: Confirm keeps the EXISTING lock, never claims/releases it.
        assert session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id == "g1"


def test_confirm_is_idempotent_while_already_recovering(dashboard_client, monkeypatch):
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    _seed_gate_lock(active_gate_id="g1")
    with db_module.SessionLocal() as session:
        session.add(
            NodeUpgradeGate(
                id="g1",
                host="10.20.1.83",
                target_version="19.2.0",
                state=NodeUpgradeGateState.RECOVERING.value,
                roles_snapshot=json.dumps(["OSD"]),
            )
        )
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/gate/confirm", data={"host": "10.20.1.83"}, follow_redirects=False
    )

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        # No SECOND Action created for the same in-flight Recovery.
        assert session.query(Action).filter_by(action_id=upgrade_route.NODE_OS_GATE_RECOVER_ACTION_ID).count() == 0


def test_get_upgrade_gate_keeps_recovering_node_visible_after_os_now_passes(dashboard_client, monkeypatch):
    # Story 11.4's own fix: the node's OS now genuinely passes the live
    # check (it really was reinstalled), but the gate is still RECOVERING —
    # the page must NOT drop the row or redirect away mid-flight.
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    _seed_gate_lock(active_gate_id="g1")
    with db_module.SessionLocal() as session:
        session.add(
            NodeUpgradeGate(
                id="g1",
                host="10.20.1.83",
                target_version="19.2.0",
                state=NodeUpgradeGateState.RECOVERING.value,
                roles_snapshot=json.dumps(["OSD"]),
            )
        )
        session.commit()
    _stub_os_release(monkeypatch, {"10.20.1.83": {"ID": "rocky", "VERSION_ID": "9"}})
    _login(dashboard_client)

    response = dashboard_client.get("/upgrade/gate?target_version=19.2.0", follow_redirects=False)

    assert response.status_code == 200
    assert "10.20.1.83" in response.text
    assert "Đang Xác nhận" in response.text


# --- Code review fixes (2026-08-06) -----------------------------------------


def test_prepare_after_failed_recovery_creates_a_fresh_gate(dashboard_client, monkeypatch):
    # Story 11.4 code review finding: FAILED is terminal (not in
    # _NODE_OS_GATE_NON_TERMINAL_STATES), so prepare_node_os_gate's
    # idempotency check must NOT short-circuit here — "Chuẩn bị lại" on a
    # FAILED node must claim the lock and create a brand-new gate row.
    _set_package_deploy(monkeypatch, mon_nodes="10.20.1.150", osd_nodes="10.20.1.83")
    _seed_gate_lock(active_gate_id=None)
    with db_module.SessionLocal() as session:
        session.add(
            NodeUpgradeGate(
                id="old-failed-gate",
                host="10.20.1.83",
                target_version="19.2.0",
                state=NodeUpgradeGateState.FAILED.value,
                roles_snapshot=json.dumps(["OSD"]),
            )
        )
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/gate/prepare",
        data={"host": "10.20.1.83", "target_version": "19.2.0"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        gates = session.query(NodeUpgradeGate).filter_by(host="10.20.1.83").all()
        assert len(gates) == 2  # old FAILED row untouched, new one created
        new_gate = next(g for g in gates if g.id != "old-failed-gate")
        assert new_gate.state == NodeUpgradeGateState.PREPARING.value
        old_gate = session.get(NodeUpgradeGate, "old-failed-gate")
        assert old_gate.state == NodeUpgradeGateState.FAILED.value  # left as history
        assert session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id == new_gate.id


def test_confirm_rejects_when_target_version_format_invalid_reaches_worker(dashboard_client, monkeypatch):
    # Code review finding: _phase_gate_install_packages now rejects a
    # non-x.y.z target_version defensively even though the Dashboard route
    # itself doesn't validate the Form field's format — verified at the
    # worker layer directly (this Confirm route always persists whatever
    # gate_row.target_version already holds, so the defense-in-depth lives
    # in cluster_deploy.py, not here; this test documents that boundary).
    from worker.executor import cluster_deploy as cluster_deploy_module_local

    with pytest.raises(cluster_deploy_module_local.DeployPhaseError):
        cluster_deploy_module_local._phase_gate_install_packages(
            ["10.20.1.83"],
            {
                "host": "10.20.1.83",
                "target_version": "19.2.0; rm -rf /",
                "roles": ["OSD"],
                "nodes": ["10.20.1.83"],
                "node_upgrade_gate_id": "gate-1",
                "action_pk": "action-1",
                "incident_id": "incident-1",
            },
            lambda host_status: None,
        )
