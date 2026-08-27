import asyncio
import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.llm.router_client as router_client
from config.settings import settings
from shared import audit
from shared import db as db_module
from shared.db import Base
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    ActionPolicyOverride,
    AuditEntry,
    AutopilotLease,
    ChatMessage,
    Cluster,
    Incident,
    IncidentStatus,
    IncidentTimelineEvent,
    RemediationCase,
)

ENVELOPE = {
    "schema_version": "1.0",
    "incident_id": "will-be-set-per-test",
    "ceph_code": "MON_CLOCK_SKEW",
    "detected_at": "2026-07-16T10:00:00",
    "nodes": ["10.20.1.249"],
    "log_excerpt": "mon2 clock skew log",
    "cluster_snapshot": {"status": "HEALTH_WARN"},
}


@pytest.fixture()
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # Mirror shared/db.py::make_engine()'s PRAGMA foreign_keys=ON — without
    # this, FK-constrained inserts (e.g. AuditEntry via Story 3.3's
    # audit.record()) succeed here even when they'd raise IntegrityError in
    # production, hiding real bugs from this test file (Review Story 3.3).
    event.listen(engine, "connect", lambda dbapi_conn, _: dbapi_conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    with db_module.SessionLocal() as session:
        session.add(Cluster(
            id="test-default-cluster", name="test", is_default=True, is_active=True,
            ceph_mon_nodes="mon-a", ceph_container_name="ceph-mon", ssh_user="root",
            ssh_key_path="/tmp/test-key", ceph_exec_mode="none",
            autonomy_environment="lab", autopilot_enabled=True,
        ))
        session.commit()
    # Most tests in this legacy suite exercise the execution path itself.
    # Opt them into the pre-Pha-0 posture explicitly; dedicated tests below
    # cover the new fail-closed defaults and kill-switch behavior.
    monkeypatch.setattr(settings, "ai_preflight_enforcement_enabled", False)
    monkeypatch.setattr(settings, "autopilot_enabled", True)
    monkeypatch.setattr(settings, "autopilot_grace_period_seconds", 0)
    # Legacy execution-path fixtures predate model confidence. Tests dedicated
    # to the confidence contract below set a production-like threshold.
    monkeypatch.setattr(settings, "ai_min_diagnosis_confidence", 0.0)
    monkeypatch.setattr(
        router_client, "run_ceph_json_command_with",
        lambda *_args, **_kwargs: ("mon-a", {
            "health": {"status": "HEALTH_OK"}, "monmap": {"num_mons": 1},
            "quorum_names": ["mon-a"], "pgmap": {
                "pgs_by_state": [{"state_name": "active+clean", "count": 1}],
            },
        }),
    )
    yield engine


def _create_incident(incident_id: str) -> None:
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id=incident_id,
                ceph_code="MON_CLOCK_SKEW",
                status=IncidentStatus.DIAGNOSING.value,
                detected_at=datetime.utcnow(),
            )
        )
        session.commit()


def test_pool_too_many_pgs_extracts_only_decreasing_ceph_targets():
    envelope = {
        "cluster_snapshot": {"checks": {"POOL_TOO_MANY_PGS": {"detail": [
            {"message": "Pool 'images' has 128 placement groups, should have 32"},
            {"message": "Pool volumes has 64 placement groups, should have 16"},
            {"message": "Pool bad has 8 placement groups, should have 32"},
        ]}}},
    }

    assert router_client._pool_pg_adjustments_from_health_detail(
        envelope, check_code="POOL_TOO_MANY_PGS",
    ) == [
        {"pool_name": "images", "current_pg_num": 128, "pg_num": 32},
        {"pool_name": "volumes", "current_pg_num": 64, "pg_num": 16},
    ]


def test_large_omap_diagnosis_is_read_only_and_rgw_specific():
    envelope = {
        "cluster_snapshot": {"checks": {"LARGE_OMAP_OBJECTS": {"detail": [
            {"message": "2 large objects found in pool '.rgw.buckets.index'"},
        ]}}},
        "log_excerpt": "Large omap object found. Object: .dir.bucket-marker key count 250000",
    }

    diagnosis = router_client._large_omap_diagnosis(envelope)

    assert ".rgw.buckets.index" in diagnosis
    assert "rgw_dynamic_resharding" in diagnosis
    assert "không được xoá" in diagnosis


def test_large_omap_lab_evidence_calculates_safe_headroom_shards():
    envelope = {
        "log_excerpt": (
            "LARGE_OMAP_EVIDENCE bucket=test-large-omap "
            "object=.dir.instance.3.0 keys=10922 threshold=5000 shards=1 pg=6.5"
        )
    }

    params = router_client._large_omap_reshard_params(envelope)

    assert params["bucket_name"] == "test-large-omap"
    assert params["num_shards"] == 3
    assert params["pg_id"] == "6.5"


def test_large_omap_production_bucket_is_not_contextually_safe():
    envelope = {
        "log_excerpt": (
            "LARGE_OMAP_EVIDENCE bucket=customer-data "
            "object=.dir.instance.3.0 keys=250000 threshold=200000 shards=1 pg=6.a"
        )
    }

    assert router_client._large_omap_reshard_params(envelope) is None


def test_diagnose_incident_saves_diagnosis_text_on_valid_response(isolated_db, monkeypatch):
    redact_calls = []

    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "MON clock is skewed beyond threshold; likely NTP drift.",
            "action_id": "resync_ntp",
            "rationale": "clock skew directly maps to NTP resync.",
        }

    def spying_redact(payload):
        redact_calls.append(payload)
        return payload

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(router_client.default_redactor, "redact", spying_redact)
    # Story 3.2: resync_ntp is SAFE and now auto-executes — this test cares
    # about diagnosis_text/Action creation, not execution, so stub it out.
    monkeypatch.setattr(router_client, "execute_command", lambda host, command, **kwargs: "ok")

    _create_incident("incident-1")
    envelope = dict(ENVELOPE, incident_id="incident-1")

    asyncio.run(router_client.diagnose_incident("incident-1", envelope))

    assert len(redact_calls) == 1  # AC #1: exactly one redaction pass, after retrieval enrichment
    assert {key: value for key, value in redact_calls[0].items() if key != "verified_case_references"} == envelope
    assert redact_calls[0]["verified_case_references"] == []
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-1")
        assert incident.diagnosis_text == "MON clock is skewed beyond threshold; likely NTP drift."
        actions = session.query(Action).filter_by(incident_id="incident-1").all()
        assert len(actions) == 1
        assert actions[0].action_id == "resync_ntp"
        assert actions[0].classification == ActionClassification.SAFE.value
        assert actions[0].status == ActionStatus.AUTO_EXECUTED.value  # auto-executed, Story 3.2
        case = session.query(RemediationCase).filter_by(action_id=actions[0].id).one()
        assert case.shadow_decision == "HOLD"
        assert case.shadow_sample_count == 0
        assert case.shadow_recorded_at is not None
        assert case.outcome == "EXECUTED_PENDING_VERIFY"
        assert case.incident_id == "incident-1"


def test_diagnose_incident_called_twice_does_not_create_duplicate_action(isolated_db, monkeypatch):
    # Simulates a redelivered message (e.g. Story 2.1 retry, or an ack()
    # failure after a successful commit) causing diagnose_incident to run
    # twice for the same incident — must not end up with 2 (possibly
    # conflicting) Action rows.
    call_count = {"n": 0}

    async def fake_call_router(user_content):
        call_count["n"] += 1
        # Second call returns a DIFFERENT (risky) action_id — proving the
        # guard prevents a conflicting second Action, not just an identical one.
        if call_count["n"] == 1:
            return {"diagnosis_text": "first diagnosis", "action_id": "resync_ntp", "rationale": "r1"}
        return {"diagnosis_text": "second diagnosis", "action_id": "restart_osd_daemon", "rationale": "r2"}

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(router_client, "execute_command", lambda host, command, **kwargs: "ok")

    _create_incident("incident-1c")
    envelope = dict(ENVELOPE, incident_id="incident-1c")

    asyncio.run(router_client.diagnose_incident("incident-1c", envelope))
    asyncio.run(router_client.diagnose_incident("incident-1c", envelope))

    with db_module.SessionLocal() as session:
        actions = session.query(Action).filter_by(incident_id="incident-1c").all()
        assert len(actions) == 1
        assert actions[0].action_id == "resync_ntp"  # first classification wins, not overwritten


def test_diagnose_incident_creates_risky_action_for_risky_action_id(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "OSD daemon appears down, likely a transient crash.",
            "action_id": "restart_osd_daemon",
            "rationale": "restarting the daemon typically clears a transient crash.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-1b")
    envelope = dict(ENVELOPE, incident_id="incident-1b")

    asyncio.run(router_client.diagnose_incident("incident-1b", envelope))

    with db_module.SessionLocal() as session:
        actions = session.query(Action).filter_by(incident_id="incident-1b").all()
        assert len(actions) == 1
        assert actions[0].classification == ActionClassification.RISKY.value


def test_pool_too_few_pgs_uses_exact_health_detail_targets(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        # The model is allowed to diagnose the warning, but command parameters
        # must come from Ceph's structured health detail, not this response.
        return {
            "diagnosis_text": "Hai pool đang có ít PG hơn mức Ceph khuyến nghị.",
            "action_id": "resync_ntp",
            "rationale": "generic model recommendation",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    _create_incident("incident-too-few-pgs")
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-too-few-pgs")
        incident.ceph_code = "POOL_TOO_FEW_PGS"
        session.commit()

    envelope = dict(
        ENVELOPE,
        incident_id="incident-too-few-pgs",
        ceph_code="POOL_TOO_FEW_PGS",
        cluster_snapshot={
            "status": "HEALTH_WARN",
            "checks": {
                "POOL_TOO_FEW_PGS": {
                    "severity": "HEALTH_WARN",
                    "summary": {"message": "2 pools have too few placement groups"},
                    "detail": [
                        {"message": "Pool images has 16 placement groups, should have 32"},
                        {"message": "Pool volumes has 16 placement groups, should have 32"},
                    ],
                }
            },
        },
    )

    asyncio.run(router_client.diagnose_incident("incident-too-few-pgs", envelope))

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-too-few-pgs").one()
        assert action.action_id == "set_pool_pg_num"
        assert action.classification == ActionClassification.RISKY.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert json.loads(action.action_params) == {
            "adjustments": [
                {"pool_name": "images", "current_pg_num": 16, "pg_num": 32},
                {"pool_name": "volumes", "current_pg_num": 16, "pg_num": 32},
            ]
        }
        assert action.proposed_command == (
            "ceph osd pool set images pg_num 32 && "
            "ceph osd pool set volumes pg_num 32"
        )


def test_diagnose_incident_auto_rejects_risky_action_while_upgrade_in_flight(
    isolated_db, monkeypatch
):
    # 2026-07-24: a cluster upgrade restarting every daemon one host at a
    # time routinely trips a transient OSD_DOWN/MGR_DOWN incident — this
    # must not surface as a new "chờ duyệt" proposal the operator has to
    # reject just to let the upgrade they already approved keep running.
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "OSD daemon appears down, likely a transient crash.",
            "action_id": "restart_osd_daemon",
            "rationale": "restarting the daemon typically clears a transient crash.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    upgrade_incident = Incident(
        id="upgrade-incident-1",
        ceph_code="CLUSTER_UPGRADE",
        status=IncidentStatus.APPROVED.value,
        detected_at=datetime.utcnow(),
    )
    with db_module.SessionLocal() as session:
        session.add(upgrade_incident)
        session.flush()
        session.add(
            Action(
                incident_id=upgrade_incident.id,
                action_id="upgrade_ceph_cluster_package_download",
                classification=ActionClassification.RISKY.value,
                status=ActionStatus.APPROVED.value,
            )
        )
        session.commit()

    _create_incident("incident-suppressed")
    envelope = dict(ENVELOPE, incident_id="incident-suppressed")

    asyncio.run(router_client.diagnose_incident("incident-suppressed", envelope))

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-suppressed").one()
        assert action.status == ActionStatus.REJECTED.value
        incident = session.get(Incident, "incident-suppressed")
        assert incident.status == IncidentStatus.REJECTED.value
        entries = session.query(AuditEntry).filter_by(incident_id="incident-suppressed").all()
        assert (
            entries[-1].event_type
            == audit.EVENT_RISKY_ACTION_AUTO_REJECTED_CLUSTER_OPERATION_IN_PROGRESS
        )


def test_diagnose_incident_routes_risky_action_normally_once_upgrade_resolved(
    isolated_db, monkeypatch
):
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "OSD daemon appears down.",
            "action_id": "restart_osd_daemon",
            "rationale": "restart clears a transient crash.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(router_client.commands, "execute_command", lambda host, command, **kwargs: "")

    upgrade_incident = Incident(
        id="upgrade-incident-2",
        ceph_code="CLUSTER_UPGRADE",
        status=IncidentStatus.RESOLVED.value,
        detected_at=datetime.utcnow(),
    )
    with db_module.SessionLocal() as session:
        session.add(upgrade_incident)
        session.flush()
        session.add(
            Action(
                incident_id=upgrade_incident.id,
                action_id="upgrade_ceph_cluster_package_download",
                classification=ActionClassification.RISKY.value,
                status=ActionStatus.EXECUTED.value,
            )
        )
        session.commit()

    _create_incident("incident-normal-again")
    envelope = dict(ENVELOPE, incident_id="incident-normal-again")

    asyncio.run(router_client.diagnose_incident("incident-normal-again", envelope))

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-normal-again").one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value


def test_diagnose_incident_keeps_restart_osd_daemon_risky(isolated_db, monkeypatch):
    # 2026-07-23: the dashboard-controlled auto-approve-restart-osd override
    # (shared/auto_approve.py) was removed along with its dashboard toggle
    # and the "Chờ duyệt" approval card — restart_osd_daemon now always
    # classifies via the plain policy (action_policy.yaml's `risky:` list),
    # unconditionally, same as any other RISKY action_id.
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "OSD daemon appears down.",
            "action_id": "restart_osd_daemon",
            "rationale": "restart clears a transient crash.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command, **kwargs: pytest.fail("must not execute")
    )

    _create_incident("incident-auto-2")
    envelope = dict(ENVELOPE, incident_id="incident-auto-2")

    asyncio.run(router_client.diagnose_incident("incident-auto-2", envelope))

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-auto-2").one()
        assert action.classification == ActionClassification.RISKY.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value


def test_safe_override_only_restarts_exact_osd_for_verified_osd_down(isolated_db, monkeypatch):
    async def fake_call_router(_user_content):
        return {"diagnosis_text": "osd.0 down", "action_id": "restart_osd_daemon", "rationale": "restart exact daemon"}

    calls = []
    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(router_client.commands, "_discover_ceph_units", lambda _host: {
        "osd": ["ceph-fsid@osd.0.service", "ceph-fsid@osd.1.service"],
        "mon": [], "mgr": [], "mds": [], "rgw": [],
    })
    monkeypatch.setattr(
        router_client, "execute_command",
        lambda host, command, **_kwargs: calls.append((host, command)) or "ok",
    )
    _create_incident("incident-exact-osd")
    with db_module.SessionLocal() as session:
        session.get(Incident, "incident-exact-osd").ceph_code = "OSD_DOWN"
        session.add(ActionPolicyOverride(
            action_id="restart_osd_daemon", classification="SAFE",
            updated_by="admin", reason="Exact OSD self healing",
        ))
        session.commit()
    envelope = dict(
        ENVELOPE, incident_id="incident-exact-osd", ceph_code="OSD_DOWN",
        nodes=["10.20.1.83"], osd_hosts={"0": "10.20.1.83"},
    )
    asyncio.run(router_client.diagnose_incident("incident-exact-osd", envelope))

    assert calls == [("10.20.1.83", "systemctl restart ceph-fsid@osd.0.service")]
    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-exact-osd").one()
        assert action.status == ActionStatus.AUTO_EXECUTED.value
        assert json.loads(action.action_params) == {"osd_ids_by_host": {"10.20.1.83": [0]}}


def test_safe_override_keeps_vague_pg_restart_risky(isolated_db, monkeypatch):
    async def fake_call_router(_user_content):
        return {"diagnosis_text": "PG degraded", "action_id": "restart_osd_daemon", "rationale": "vague restart"}

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    _create_incident("incident-vague-pg")
    with db_module.SessionLocal() as session:
        session.get(Incident, "incident-vague-pg").ceph_code = "PG_DEGRADED"
        session.add(ActionPolicyOverride(
            action_id="restart_osd_daemon", classification="SAFE",
            updated_by="admin", reason="Exact OSD self healing",
        ))
        session.commit()
    envelope = dict(
        ENVELOPE, incident_id="incident-vague-pg", ceph_code="PG_DEGRADED",
        nodes=["10.20.1.83", "10.20.1.84"], osd_hosts={},
    )
    asyncio.run(router_client.diagnose_incident("incident-vague-pg", envelope))
    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-vague-pg").one()
        assert action.classification == ActionClassification.RISKY.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value


def test_verified_bluestore_slow_osd_restart_is_contextually_safe(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_risky)
    monkeypatch.setattr(
        router_client.commands, "_discover_ceph_units", lambda _host: {
            "osd": ["ceph-fsid@osd.4.service"],
            "mon": [], "mgr": [], "mds": [], "rgw": [],
        },
    )
    monkeypatch.setattr(
        router_client, "execute_command",
        lambda host, command, **kwargs: calls.append((host, command)) or "ok",
    )
    _create_incident("incident-blue-safe")
    with db_module.SessionLocal() as session:
        session.get(Incident, "incident-blue-safe").ceph_code = "BLUESTORE_SLOW_OP_ALERT"
        session.commit()
    envelope = dict(
        ENVELOPE,
        incident_id="incident-blue-safe",
        ceph_code="BLUESTORE_SLOW_OP_ALERT",
        nodes=["10.20.1.83"],
        osd_hosts={"4": "10.20.1.83"},
    )

    asyncio.run(router_client.diagnose_incident("incident-blue-safe", envelope))

    assert calls == [("10.20.1.83", "systemctl restart ceph-fsid@osd.4.service")]
    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-blue-safe").one()
        assert action.classification == ActionClassification.SAFE.value
        assert action.status == ActionStatus.AUTO_EXECUTED.value
        assert json.loads(action.action_params) == {
            "osd_ids_by_host": {"10.20.1.83": [4]}
        }


def test_osd_upgrade_finished_always_proposes_release_command(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "Nâng cấp OSD đã hoàn tất nhưng compatibility floor còn thấp.",
            "action_id": "restart_osd_daemon",
            "rationale": "LLM output is deliberately overridden for this health code.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    _create_incident("incident-osd-upgrade-finished")
    envelope = dict(
        ENVELOPE,
        incident_id="incident-osd-upgrade-finished",
        ceph_code="OSD_UPGRADE_FINISHED",
        nodes=["10.20.1.83", "10.20.1.84"],
        log_excerpt=(
            "all OSDs are running pacific or later but require_osd_release < pacific"
        ),
    )

    asyncio.run(router_client.diagnose_incident("incident-osd-upgrade-finished", envelope))

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-osd-upgrade-finished").one()
        assert action.action_id == "finalize_osd_release"
        assert action.classification == ActionClassification.RISKY.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert json.loads(action.action_params) == {"release": "pacific"}
        assert json.loads(action.target_nodes) == ["10.20.1.83"]
        assert action.proposed_command == "ceph osd require-osd-release pacific"
        assert "ceph osd require-osd-release pacific" in action.rationale


def test_osd_upgrade_finished_does_not_guess_unknown_release(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "OSD upgrade warning.",
            "action_id": "restart_osd_daemon",
            "rationale": "Needs inspection.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    _create_incident("incident-osd-upgrade-unknown")
    envelope = dict(
        ENVELOPE,
        incident_id="incident-osd-upgrade-unknown",
        ceph_code="OSD_UPGRADE_FINISHED",
        log_excerpt="all OSDs are running futureceph or later",
    )

    with pytest.raises(router_client.RouterDiagnosisError, match="không chứa release Ceph đã biết"):
        asyncio.run(router_client.diagnose_incident("incident-osd-upgrade-unknown", envelope))


def test_diagnose_incident_raises_when_no_tool_use_block(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        raise router_client.RouterDiagnosisError("no report_diagnosis tool call")

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-2")
    envelope = dict(ENVELOPE, incident_id="incident-2")

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client.diagnose_incident("incident-2", envelope))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-2")
        assert incident.diagnosis_text is None
        assert session.query(Action).filter_by(incident_id="incident-2").count() == 0


def test_diagnose_incident_requires_confidence_in_production(isolated_db, monkeypatch):
    async def fake_call_router(_user_content):
        return {"diagnosis_text": "clock skew", "action_id": "resync_ntp", "rationale": "NTP drift"}

    monkeypatch.setattr(settings, "ai_min_diagnosis_confidence", 0.6)
    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    _create_incident("incident-missing-confidence")

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client.diagnose_incident("incident-missing-confidence", dict(
            ENVELOPE, incident_id="incident-missing-confidence",
        )))

    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(incident_id="incident-missing-confidence").count() == 0


def test_diagnose_incident_low_confidence_never_creates_action(isolated_db, monkeypatch):
    async def fake_call_router(_user_content):
        return {
            "diagnosis_text": "Có thể do NTP drift.", "action_id": "resync_ntp",
            "rationale": "Evidence chưa đủ.", "diagnosis_confidence": 0.59,
        }

    monkeypatch.setattr(settings, "ai_min_diagnosis_confidence", 0.6)
    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(router_client, "send_ai_incident_alert", lambda *_args, **_kwargs: None)
    _create_incident("incident-low-confidence")
    asyncio.run(router_client.diagnose_incident(
        "incident-low-confidence", dict(ENVELOPE, incident_id="incident-low-confidence"),
    ))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-low-confidence")
        assert incident.status == IncidentStatus.FAILED.value
        assert "0.59" in incident.diagnosis_text and "0.60" in incident.diagnosis_text
        assert session.query(Action).filter_by(incident_id=incident.id).count() == 0
        assert session.query(AuditEntry).filter_by(
            incident_id=incident.id,
            event_type=audit.EVENT_PROPOSAL_BLOCKED_BY_LOW_CONFIDENCE,
        ).count() == 1
        events = session.query(IncidentTimelineEvent).filter_by(
            incident_id=incident.id,
        ).order_by(IncidentTimelineEvent.created_at).all()
        completed = next(event for event in events if event.event_type == "diagnosis_completed")
        blocked = next(
            event for event in events
            if event.event_type == audit.EVENT_PROPOSAL_BLOCKED_BY_LOW_CONFIDENCE
        )
        completed_evidence = json.loads(completed.evidence_json)
        blocked_evidence = json.loads(blocked.evidence_json)
        assert completed_evidence["diagnosis_confidence"] == 0.59
        assert completed_evidence["minimum_confidence"] == 0.6
        assert completed_evidence["proposed_action_id"] == "resync_ntp"
        assert blocked_evidence == {
            "diagnosis_confidence": 0.59,
            "minimum_confidence": 0.6,
            "model_provider": settings.router_provider,
            "proposed_action_id": "resync_ntp",
        }


def test_diagnose_incident_raises_when_action_id_outside_enum(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "some diagnosis",
            "action_id": "delete_entire_cluster",  # not in VALID_ACTION_IDS
            "rationale": "nonsense",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-3")
    envelope = dict(ENVELOPE, incident_id="incident-3")

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client.diagnose_incident("incident-3", envelope))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-3")
        assert incident.diagnosis_text is None
        assert session.query(Action).filter_by(incident_id="incident-3").count() == 0


def test_diagnose_incident_raises_when_diagnosis_text_missing(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {"diagnosis_text": "", "action_id": "resync_ntp", "rationale": "x"}

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-4")
    envelope = dict(ENVELOPE, incident_id="incident-4")

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client.diagnose_incident("incident-4", envelope))


def test_diagnose_incident_raises_when_diagnosis_text_whitespace_only(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {"diagnosis_text": "   ", "action_id": "resync_ntp", "rationale": "x"}

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-4b")
    envelope = dict(ENVELOPE, incident_id="incident-4b")

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client.diagnose_incident("incident-4b", envelope))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-4b")
        assert incident.diagnosis_text is None


def test_diagnose_incident_raises_when_rationale_missing(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {"diagnosis_text": "some diagnosis", "action_id": "resync_ntp", "rationale": ""}

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-4c")
    envelope = dict(ENVELOPE, incident_id="incident-4c")

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client.diagnose_incident("incident-4c", envelope))


def test_diagnose_incident_handles_null_nodes_without_crashing(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {"diagnosis_text": "diagnosis", "action_id": "resync_ntp", "rationale": "x"}

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-4d")
    envelope = dict(ENVELOPE, incident_id="incident-4d", nodes=None)

    # Must not raise TypeError from ', '.join(None) — this used to crash
    # identically on every retry attempt.
    asyncio.run(router_client.diagnose_incident("incident-4d", envelope))


def test_diagnose_incident_unknown_incident_id_logs_and_returns_without_raising(
    isolated_db, monkeypatch
):
    async def fake_call_router(user_content):
        return {"diagnosis_text": "diagnosis", "action_id": "resync_ntp", "rationale": "x"}

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    envelope = dict(ENVELOPE, incident_id="does-not-exist")

    # Must not raise — matches worker/main.py::_set_incident_status's pattern
    # for a row that's disappeared.
    asyncio.run(router_client.diagnose_incident("does-not-exist", envelope))


class _FakeToolCall:
    def __init__(self, name: str, args: dict):
        self.function = SimpleNamespace(name=name, arguments=json.dumps(args))


class _FakeStream:
    """Mimics the openai SDK's `client.chat.completions.stream(...)` async
    context manager just enough for _call_router: `async with ... as
    stream:` then `await stream.get_final_completion()`."""

    def __init__(self, completion):
        self._completion = completion

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_final_completion(self):
        return self._completion


def _completion(*calls: tuple[str, dict], finish_reason: str = "stop"):
    """`calls` is (name, args) pairs — mirrors tests/test_chat_client.py's
    helper of the same shape. Builds a minimal fake mirroring the real
    openai ChatCompletion shape (`.choices[0].finish_reason`,
    `.choices[0].message.tool_calls[i].function.{name,arguments}`)."""
    tool_calls = [_FakeToolCall(name, args) for name, args in calls]
    message = SimpleNamespace(tool_calls=tool_calls, content=None)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def _install_fake_client(monkeypatch, completion):
    class FakeCompletions:
        def stream(self, **kwargs):
            return _FakeStream(completion)

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(router_client, "_get_client", lambda: FakeClient())


def test_get_client_uses_configured_api_key(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "router_api_key", "sk-test-whatever")
    monkeypatch.setattr(settings, "router_base_url", "http://localhost:20128")

    client = router_client._get_client()

    assert client.api_key == "sk-test-whatever"


def test_call_router_extracts_args_from_matching_tool_call(monkeypatch):
    _install_fake_client(
        monkeypatch,
        _completion(
            (router_client.TOOL_NAME, {"diagnosis_text": "d", "action_id": "resync_ntp", "rationale": "r"})
        ),
    )

    result = asyncio.run(router_client._call_router("some content"))

    assert result == {"diagnosis_text": "d", "action_id": "resync_ntp", "rationale": "r"}


def test_call_router_uses_codex_when_enabled(monkeypatch):
    monkeypatch.setattr(router_client.settings, "codex_chat_enabled", True)
    monkeypatch.setattr(router_client.settings, "claude_chat_enabled", False)

    async def fake_run_turn(prompt, tools, handler, timeout):
        await handler(
            router_client.TOOL_NAME,
            {"diagnosis_text": "d", "action_id": "resync_ntp", "rationale": "r"},
        )
        return {}

    monkeypatch.setattr(router_client.codex_app_server, "run_turn", fake_run_turn)
    monkeypatch.setattr(router_client, "_get_client", lambda: pytest.fail("router must not be used"))

    result = asyncio.run(router_client._call_router("some content"))

    assert result == {"diagnosis_text": "d", "action_id": "resync_ntp", "rationale": "r"}


def test_call_router_uses_claude_when_enabled(monkeypatch):
    monkeypatch.setattr(router_client.settings, "codex_chat_enabled", False)
    monkeypatch.setattr(router_client.settings, "claude_chat_enabled", True)

    async def fake_prompt(prompt, timeout):
        return '```json\n{"diagnosis_text":"d","action_id":"resync_ntp","rationale":"r"}\n```'

    monkeypatch.setattr(router_client, "run_claude_prompt", fake_prompt)
    monkeypatch.setattr(router_client, "_get_client", lambda: pytest.fail("router must not be used"))

    result = asyncio.run(router_client._call_router("some content"))

    assert result == {"diagnosis_text": "d", "action_id": "resync_ntp", "rationale": "r"}


def test_call_router_ignores_tool_call_with_wrong_name(monkeypatch):
    _install_fake_client(monkeypatch, _completion(("some_other_tool", {"x": 1})))

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client._call_router("some content"))


def test_call_router_raises_when_there_is_no_tool_call(monkeypatch):
    _install_fake_client(monkeypatch, _completion())

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client._call_router("some content"))


def test_call_router_returns_first_matching_call_when_multiple_tool_calls(monkeypatch):
    _install_fake_client(
        monkeypatch,
        _completion(
            (router_client.TOOL_NAME, {"first": True}),
            (router_client.TOOL_NAME, {"first": False}),
        ),
    )

    result = asyncio.run(router_client._call_router("some content"))

    assert result == {"first": True}


def test_call_router_raises_when_response_truncated_at_max_tokens(monkeypatch):
    _install_fake_client(
        monkeypatch,
        _completion(
            (router_client.TOOL_NAME, {"diagnosis_text": "cut off"}),
            finish_reason="length",
        ),
    )

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client._call_router("some content"))


def test_warn_if_missing_api_key_logs_warning_when_empty(caplog):
    with caplog.at_level("WARNING"):
        router_client._warn_if_missing_api_key("")
    assert "ROUTER_API_KEY is not configured" in caplog.text


def test_warn_if_missing_api_key_silent_when_set(caplog):
    with caplog.at_level("WARNING"):
        router_client._warn_if_missing_api_key("sk-fake-key")
    assert "ROUTER_API_KEY is not configured" not in caplog.text


@pytest.mark.parametrize("provider", ["codex_enabled", "claude_enabled"])
def test_warn_if_missing_api_key_silent_when_cli_provider_enabled(caplog, provider):
    with caplog.at_level("WARNING"):
        router_client._warn_if_missing_api_key("", **{provider: True})
    assert "ROUTER_API_KEY is not configured" not in caplog.text


def test_valid_action_ids_loaded_from_policy_yaml_non_empty():
    assert len(router_client.VALID_ACTION_IDS) > 0
    assert "resync_ntp" in router_client.VALID_ACTION_IDS
    assert "restart_osd_daemon" in router_client.VALID_ACTION_IDS
    assert "pg_repair_force" in router_client.VALID_ACTION_IDS
    assert "investigate_manually" in router_client.VALID_ACTION_IDS
    assert "crash_archive_all" in router_client.VALID_ACTION_IDS


def test_ai_schema_only_offers_executable_actions():
    action_enum = router_client._tool_schema()["function"]["parameters"]["properties"]["action_id"]["enum"]
    assert "investigate_manually" not in action_enum
    assert "pg_repair_force" not in action_enum
    assert "enable_pool_application" in action_enum
    assert all(router_client.commands.has_command(action_id) for action_id in action_enum)


# --- Story 3.2: Safe Action execution ---------------------------------------


def _safe_response(nodes_note=""):
    return {
        "diagnosis_text": f"MON clock skewed{nodes_note}",
        "action_id": "resync_ntp",
        "rationale": "clock skew maps to NTP resync",
    }


async def _fake_call_router_safe(user_content):
    return _safe_response()


async def _fake_call_router_risky(user_content):
    return {
        "diagnosis_text": "OSD daemon appears down",
        "action_id": "restart_osd_daemon",
        "rationale": "restart clears transient crash",
    }


def test_diagnose_incident_executes_safe_action_and_marks_auto_fixed(isolated_db, monkeypatch):
    execute_calls = []

    def fake_execute(host, command, **kwargs):
        execute_calls.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(router_client, "execute_command", fake_execute)

    _create_incident("incident-5a")
    envelope = dict(ENVELOPE, incident_id="incident-5a", nodes=["10.20.1.249", "10.20.1.253"])

    asyncio.run(router_client.diagnose_incident("incident-5a", envelope))

    assert [host for host, _cmd in execute_calls] == ["10.20.1.249", "10.20.1.253"]
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5a")
        assert incident.status == IncidentStatus.VERIFYING.value
        assert incident.verify_after is not None
        action = session.query(Action).filter_by(incident_id="incident-5a").one()
        assert action.status == ActionStatus.AUTO_EXECUTED.value
        assert action.executed_at is not None
        assert "chronyc" in action.proposed_command
        entry = session.query(AuditEntry).filter_by(incident_id="incident-5a").one()
        assert entry.action_id == action.id
        assert entry.event_type == audit.EVENT_SAFE_ACTION_EXECUTED
        assert entry.actor == audit.ACTOR_SYSTEM


def test_diagnose_incident_safe_action_uses_envelopes_own_cluster_creds_not_default(isolated_db, monkeypatch):
    """2026-08-10 (multi-tenant remediation Phase 1) regression guard: the
    envelope's own ssh_user/ssh_key_path (a NON-default cluster's, in this
    test) must reach execute_command() exactly as given — never silently
    falling back to the default cluster's settings.ssh_user/ssh_key_path.
    This is the single most important test in this file given the
    credential-mixup risk the whole feature exists to avoid."""
    execute_calls = []

    def fake_execute(host, command, user=None, key_path=None):
        execute_calls.append((host, user, key_path))
        return "ok"

    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(router_client, "execute_command", fake_execute)

    _create_incident("incident-5z")
    envelope = dict(
        ENVELOPE,
        incident_id="incident-5z",
        nodes=["10.30.1.20"],
        cluster_id="other-cluster-id",
        ssh_user="other-cluster-user",
        ssh_key_path="/root/.ssh/other-cluster-key",
    )

    asyncio.run(router_client.diagnose_incident("incident-5z", envelope))

    assert execute_calls == [("10.30.1.20", "other-cluster-user", "/root/.ssh/other-cluster-key")]


def test_diagnose_incident_marks_failed_when_any_node_execution_fails(isolated_db, monkeypatch):
    def fake_execute(host, command, **kwargs):
        if host == "10.20.1.253":
            raise router_client.ExecutorError(f"{host}: command exited 1")
        return "ok"

    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(router_client, "execute_command", fake_execute)

    _create_incident("incident-5b")
    envelope = dict(ENVELOPE, incident_id="incident-5b", nodes=["10.20.1.249", "10.20.1.253"])

    # Must not raise — an execution failure is terminal (AC #3), not retried
    # via worker/main.py's router-failure retry/DLX mechanism.
    asyncio.run(router_client.diagnose_incident("incident-5b", envelope))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5b")
        assert incident.status == IncidentStatus.FAILED.value
        action = session.query(Action).filter_by(incident_id="incident-5b").one()
        assert action.status == ActionStatus.FAILED.value
        assert action.executed_at is None
        assert action.proposed_command is not None  # recorded even on failure
        entry = session.query(AuditEntry).filter_by(incident_id="incident-5b").one()
        assert entry.event_type == audit.EVENT_SAFE_ACTION_FAILED


def test_record_execution_result_missing_action_row_does_not_crash_and_still_updates_incident(
    isolated_db,
):
    _create_incident("incident-missing-action")

    router_client._record_execution_result(
        "incident-missing-action", "nonexistent-action-pk", command="echo ok", succeeded=True
    )

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-missing-action")
        assert incident.status == IncidentStatus.VERIFYING.value
        assert incident.verify_after is not None
        entry = session.query(AuditEntry).filter_by(incident_id="incident-missing-action").one()
        assert entry.action_id is None
        assert entry.event_type == audit.EVENT_SAFE_ACTION_EXECUTED


def test_record_execution_result_missing_incident_row_does_not_crash_and_skips_audit(
    isolated_db,
):
    # Neither Incident nor Action exists — nothing valid to update or audit.
    router_client._record_execution_result(
        "nonexistent-incident", "nonexistent-action-pk", command="echo ok", succeeded=True
    )

    with db_module.SessionLocal() as session:
        assert session.query(AuditEntry).count() == 0


def test_diagnose_incident_recovers_pending_action_left_by_a_crashed_prior_attempt(
    isolated_db, monkeypatch
):
    # Simulates: a prior diagnose_incident() call created the Action (status
    # PENDING) and committed, then the process died before execution ran.
    # A redelivery must retry execution, not strand it forever.
    _create_incident("incident-5g")
    with db_module.SessionLocal() as session:
        session.add(
            Action(
                incident_id="incident-5g",
                action_id="resync_ntp",
                classification=ActionClassification.SAFE.value,
                status=ActionStatus.PENDING.value,
            )
        )
        session.commit()

    execute_calls = []
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command, **kwargs: execute_calls.append(host) or "ok"
    )

    envelope = dict(ENVELOPE, incident_id="incident-5g")
    asyncio.run(router_client.diagnose_incident("incident-5g", envelope))

    assert execute_calls == ["10.20.1.249"]  # execution actually retried
    with db_module.SessionLocal() as session:
        actions = session.query(Action).filter_by(incident_id="incident-5g").all()
        assert len(actions) == 1  # no duplicate row created
        assert actions[0].status == ActionStatus.AUTO_EXECUTED.value
        incident = session.get(Incident, "incident-5g")
        assert incident.status == IncidentStatus.VERIFYING.value
        assert incident.verify_after is not None


# --- Story 4.2: RISKY -> PENDING_APPROVAL ----------------------------------


def test_diagnose_incident_risky_action_records_pending_approval_audit_entry(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_risky)
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command, **kwargs: pytest.fail("must not execute")
    )

    _create_incident("incident-6a")
    envelope = dict(ENVELOPE, incident_id="incident-6a", nodes=["10.20.1.83"])
    asyncio.run(router_client.diagnose_incident("incident-6a", envelope))

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-6a").one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.target_nodes == '["10.20.1.83"]'
        entries = session.query(AuditEntry).filter_by(incident_id="incident-6a").all()
        assert len(entries) == 1
        assert entries[0].event_type == audit.EVENT_RISKY_ACTION_PENDING_APPROVAL
        assert entries[0].action_id == action.id


def test_diagnose_incident_rejects_action_with_no_executable_command(
    isolated_db, monkeypatch
):
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "PG stuck inconsistent",
            "action_id": "pg_repair_force",
            "rationale": "matches pg repair criteria",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-6b")
    envelope = dict(ENVELOPE, incident_id="incident-6b")
    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client.diagnose_incident("incident-6b", envelope))

    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(incident_id="incident-6b").count() == 0


# --- Story 4.3: execute an operator-approved RISKY action ------------------


def _approved_action(session, incident_id: str, action_id: str = "restart_osd_daemon", nodes=None) -> Action:
    import json as _json

    action = Action(
        incident_id=incident_id,
        action_id=action_id,
        classification=ActionClassification.RISKY.value,
        status=ActionStatus.APPROVED.value,
        target_nodes=_json.dumps(nodes if nodes is not None else ["10.20.1.83"]),
    )
    session.add(action)
    session.commit()
    return action


def test_execute_approved_action_success_marks_executed_and_resolved(isolated_db, monkeypatch):
    execute_calls = []

    def fake_execute(host, command, **kwargs):
        if command == "systemctl --all | grep ceph || true":
            return "  ceph-fsid@osd.3.service   loaded active running   x\n"
        execute_calls.append(host)
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-7a")
    with db_module.SessionLocal() as session:
        action = _approved_action(session, "incident-7a")
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert execute_calls == ["10.20.1.83"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        assert action.executed_at is not None
        assert action.proposed_command == "systemctl restart ceph-fsid@osd.3.service"
        incident = session.get(Incident, "incident-7a")
        # 2026-08-20: lệnh chạy xong exit 0 KHÔNG còn tự động là RESOLVED —
        # Incident dừng ở VERIFYING cho tới khi watcher/verify.py hỏi lại cụm
        # và xác nhận ceph_code đã biến mất khỏi `ceph health detail`.
        assert incident.status == IncidentStatus.VERIFYING.value
        entries = session.query(AuditEntry).filter_by(incident_id="incident-7a").all()
        assert entries[-1].event_type == audit.EVENT_RISKY_ACTION_EXECUTED


def test_execute_approved_rbd_action_fails_when_post_check_disagrees(isolated_db, monkeypatch):
    monkeypatch.setattr(
        router_client, "execute_command",
        lambda *args, **kwargs: json.dumps({"name": "vm-01", "size": 9 * 1024 * 1024}),
    )
    _create_incident("incident-rbd-reconcile")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session, "incident-rbd-reconcile", action_id="rbd_resize_volume"
        )
        action.action_params = json.dumps({"pool_name": "vms", "image": "vm-01", "size_mib": 10})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        incident = session.get(Incident, "incident-rbd-reconcile")
        progress = json.loads(action.execution_progress)
        assert action.status == ActionStatus.FAILED.value
        assert incident.status == IncidentStatus.FAILED.value
        assert progress[0]["status"] == "failed"
        assert "size mismatch" in progress[0]["error"]


def test_stuck_rbd_scanner_marks_matching_live_state_executed(isolated_db, monkeypatch):
    now = datetime.utcnow()
    monkeypatch.setattr(
        router_client, "execute_command",
        lambda *args, **kwargs: json.dumps({"name": "vm-01", "size": 10 * 1024 * 1024}),
    )
    _create_incident("incident-rbd-stuck-success")
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-rbd-stuck-success")
        incident.status = IncidentStatus.EXECUTING.value
        action = _approved_action(session, incident.id, action_id="rbd_resize_volume")
        action.action_params = json.dumps({"pool_name": "vms", "image": "vm-01", "size_mib": 10})
        action.updated_at = now - timedelta(minutes=20)
        session.commit()
        action_pk = action.id

    resolved = router_client._reconcile_stuck_rbd_actions_once(now=now)

    assert resolved == [action_pk]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)
        assert progress[0]["phase"] == "reconciliation"
        assert "resize" not in progress[0]["command"]


def test_stuck_rbd_scanner_keeps_action_for_retry_when_ceph_unreachable(isolated_db, monkeypatch):
    from worker.executor.ssh_executor import ExecutorError

    now = datetime.utcnow()
    monkeypatch.setattr(
        router_client, "execute_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(ExecutorError("MON unreachable")),
    )
    _create_incident("incident-rbd-stuck-retry")
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-rbd-stuck-retry")
        incident.status = IncidentStatus.EXECUTING.value
        action = _approved_action(session, incident.id, action_id="rbd_trash_move_volume")
        action.action_params = json.dumps({"pool_name": "vms", "image": "vm-01"})
        action.updated_at = now - timedelta(minutes=20)
        session.commit()
        action_pk = action.id

    resolved = router_client._reconcile_stuck_rbd_actions_once(now=now)

    assert resolved == []
    with db_module.SessionLocal() as session:
        assert session.get(Action, action_pk).status == ActionStatus.APPROVED.value
        assert session.get(Incident, "incident-rbd-stuck-retry").status == IncidentStatus.EXECUTING.value


def test_execute_node_command_posts_stdout_back_to_chat(isolated_db, monkeypatch):
    monkeypatch.setattr(router_client, "execute_command", lambda *args, **kwargs: "RAM used: 98%\nOOM killed pid 42")
    _create_incident("incident-node-command-result")
    with db_module.SessionLocal() as session:
        session.add(ChatMessage(
            session_id="chat-session",
            role="assistant",
            content="Nhập OK để chạy",
            actor="admin",
            proposed_action_id="execute_node_command",
            proposed_target_nodes=json.dumps(["10.20.1.83"]),
            proposed_action_params=json.dumps({"command": "free -h"}),
            proposed_status="CONFIRMED",
            proposed_incident_id="incident-node-command-result",
        ))
        action = _approved_action(
            session,
            "incident-node-command-result",
            action_id="execute_node_command",
            nodes=["10.20.1.83"],
        )
        action.action_params = json.dumps({"command": "free -h"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        progress = json.loads(action.execution_progress)
        result = session.query(ChatMessage).filter_by(proposed_status="RESULT").one()
        assert progress[0]["output"] == "RAM used: 98%\nOOM killed pid 42"
        assert result.session_id == "chat-session"
        assert "Kết quả trên 10.20.1.83" in result.content
        assert "OOM killed pid 42" in result.content


def test_execute_approved_action_skips_when_another_worker_claimed_incident(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        router_client, "execute_command", lambda *args, **kwargs: calls.append(args) or "ok"
    )
    _create_incident("incident-already-claimed")
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-already-claimed")
        incident.status = IncidentStatus.EXECUTING.value
        action = _approved_action(session, incident.id)
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert calls == []
    with db_module.SessionLocal() as session:
        assert session.get(Action, action_pk).status == ActionStatus.APPROVED.value


def test_execute_approved_action_uses_incidents_own_cluster_creds_not_default(isolated_db, monkeypatch):
    """2026-08-10 (multi-tenant remediation Phase 1) regression guard: by the
    time an operator approves a RISKY action, the original RabbitMQ envelope
    is long gone — Incident.cluster_id -> Cluster is the only place left to
    resolve creds from. Must be that cluster's own ssh_user/ssh_key_path,
    never settings.ssh_user/settings.ssh_key_path (the default cluster's)."""
    execute_calls = []

    def fake_execute(host, command, user=None, key_path=None):
        if command == "systemctl --all | grep ceph || true":
            return "  ceph-fsid@osd.9.service   loaded active running   x\n"
        execute_calls.append((host, user, key_path))
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="cluster-b",
            ceph_mon_nodes="10.30.1.10",
            ssh_user="other-cluster-user",
            ssh_key_path="/root/.ssh/other-cluster-key",
            ceph_exec_mode="docker",
            is_default=False,
            is_active=True,
        )
        session.add(cluster)
        session.commit()
        session.refresh(cluster)
        cluster_id = cluster.id

    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id="incident-cross-cluster",
                ceph_code="OSD_DOWN",
                status=IncidentStatus.DIAGNOSING.value,
                detected_at=datetime.utcnow(),
                cluster_id=cluster_id,
            )
        )
        session.commit()
        action = _approved_action(session, "incident-cross-cluster", nodes=["10.30.1.20"])
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert execute_calls == [("10.30.1.20", "other-cluster-user", "/root/.ssh/other-cluster-key")]


def test_package_upgrade_uses_incidents_cluster_nodes_roles_and_credentials(isolated_db, monkeypatch):
    """A selected non-default cluster must never inherit the old default
    cluster's MON/role list during phased package execution."""
    monkeypatch.setattr(router_client.settings, "ceph_mon_nodes", "10.3.55.153")
    monkeypatch.setattr(router_client.settings, "ceph_mgr_nodes", "10.3.55.153")
    monkeypatch.setattr(router_client.settings, "ceph_osd_nodes", "10.3.55.153")
    monkeypatch.setattr(router_client.settings, "ceph_exec_mode", "none")
    calls = []

    def fake_execute(host, command, user=None, key_path=None):
        calls.append((host, command, user, key_path))
        if command == "systemctl --all | grep ceph || true":
            return f"  ceph-mon@{host}.service   loaded active running x\n"
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="new-cluster",
            ceph_mon_nodes="10.3.53.136",
            ceph_mgr_nodes="10.3.55.91",
            ceph_osd_nodes="10.3.52.26",
            ssh_user="new-user",
            ssh_key_path="/keys/new-cluster",
            ceph_exec_mode="none",
            is_default=False,
            is_active=True,
        )
        session.add(cluster)
        session.flush()
        incident = Incident(
            id="incident-package-new-cluster",
            cluster_id=cluster.id,
            ceph_code="CLUSTER_UPGRADE",
            status=IncidentStatus.APPROVED.value,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = _approved_action(
            session,
            incident.id,
            action_id="upgrade_ceph_cluster_package_download",
            nodes=["10.3.53.136", "10.3.55.91", "10.3.52.26"],
        )
        action.action_params = json.dumps(
            {"target_version": "15.2.17", "_cluster_exec_mode": "none"}
        )
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert calls
    assert all(host != "10.3.55.153" for host, *_rest in calls)
    assert all((user, key) == ("new-user", "/keys/new-cluster") for _h, _c, user, key in calls)
    with db_module.SessionLocal() as session:
        progress = json.loads(session.get(Action, action_pk).execution_progress)
    assert all(step["host"] != "10.3.55.153" for step in progress)


def test_execute_approved_action_restart_osd_daemon_discovers_via_systemctl_and_restarts(
    isolated_db, monkeypatch
):
    systemctl_outputs = {
        "10.20.1.112": "  ceph-fsid@osd.0.service   loaded active running   x\n",
        "10.20.1.95": "  ceph-fsid@osd.1.service   loaded active running   x\n",
    }
    executed = []

    def fake_execute(host, command, **kwargs):
        if command == "systemctl --all | grep ceph || true":
            return systemctl_outputs[host]
        executed.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    # Discovery (worker/executor/commands.py::_discover_ceph_units) calls
    # execute_command via ITS OWN module-level import binding, a separate
    # reference from router_client's — both must be patched.
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-7z")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session, "incident-7z", nodes=["10.20.1.112", "10.20.1.95"]
        )
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert executed == [
        ("10.20.1.112", "systemctl restart ceph-fsid@osd.0.service"),
        ("10.20.1.95", "systemctl restart ceph-fsid@osd.1.service"),
    ]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        assert action.proposed_command == "systemctl restart ceph-fsid@osd.1.service"
        incident = session.get(Incident, "incident-7z")
        # 2026-08-20: lệnh chạy xong exit 0 KHÔNG còn tự động là RESOLVED —
        # Incident dừng ở VERIFYING cho tới khi watcher/verify.py hỏi lại cụm
        # và xác nhận ceph_code đã biến mất khỏi `ceph health detail`.
        assert incident.status == IncidentStatus.VERIFYING.value


def test_execute_approved_action_persists_execution_progress_per_host(
    isolated_db, monkeypatch
):
    systemctl_outputs = {
        "10.20.1.112": "  ceph-fsid@osd.0.service   loaded active running   x\n",
        "10.20.1.95": "  ceph-fsid@osd.1.service   loaded active running   x\n",
    }

    def fake_execute(host, command, **kwargs):
        if command == "systemctl --all | grep ceph || true":
            return systemctl_outputs[host]
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-progress-db")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session, "incident-progress-db", nodes=["10.20.1.112", "10.20.1.95"]
        )
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        progress = json.loads(action.execution_progress)

    # 2026-07-27: progress entries also carry command/started_at/finished_at
    # (used to render a per-step Markdown log on the Upgrade page) — check
    # the fields this test actually cares about rather than exact equality,
    # so it doesn't have to be updated every time a new field is added.
    assert [{"host": p["host"], "status": p["status"]} for p in progress] == [
        {"host": "10.20.1.112", "status": "done"},
        {"host": "10.20.1.95", "status": "done"},
    ]
    for p in progress:
        assert p["command"]
        assert p["started_at"]
        assert p["finished_at"]


# --- Package upgrade "require-osd-release" finalization (2026-07-28) -------
# ceph-deploy/package upgrades (unlike cephadm's `ceph orch upgrade`, which
# does this itself) never bump require_osd_release on their own — left
# alone, the cluster sits in permanent HEALTH_WARN (OSD_UPGRADE_FINISHED)
# even though every node's packages/daemons genuinely finished upgrading.
# See worker/llm/router_client.py's _PACKAGE_UPGRADE_ACTION_IDS comment.


def test_execute_approved_action_package_upgrade_runs_require_osd_release_finalize(
    isolated_db, monkeypatch
):
    executed = []

    def fake_execute(host, command, **kwargs):
        executed.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.112,10.20.1.95")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-finalize")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-finalize",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=["10.20.1.112"],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert ("10.20.1.112", "ceph osd require-osd-release octopus") in executed

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    finalize_step = progress[-1]
    assert finalize_step["command"] == "ceph osd require-osd-release octopus"
    assert finalize_step["status"] == "done"
    assert finalize_step["started_at"]
    assert finalize_step["finished_at"]


def test_execute_approved_action_package_upgrade_finalize_failure_does_not_fail_action(
    isolated_db, monkeypatch
):
    """The per-node installs are the real work — a hiccup running the
    finalize command afterwards must not retroactively turn an otherwise-
    successful multi-node upgrade into FAILED."""
    from worker.executor.ssh_executor import ExecutorError

    def fake_execute(host, command, **kwargs):
        if command.startswith("ceph osd require-osd-release"):
            raise ExecutorError("mon unreachable")
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.112")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-finalize-fail")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-finalize-fail",
            action_id="upgrade_ceph_cluster_package_local",
            nodes=["10.20.1.112"],
        )
        action.action_params = json.dumps(
            {"target_version": "15.2.17", "package_dir": "/opt/ceph-packages"}
        )
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    finalize_step = progress[-1]
    assert finalize_step["status"] == "failed"
    assert finalize_step["error"] == "mon unreachable"


def test_execute_approved_action_non_package_action_skips_finalize(isolated_db, monkeypatch):
    """restart_osd_daemon (and every other non-upgrade action_id) must not
    trigger the require-osd-release finalization step."""
    executed = []

    def fake_execute(host, command, **kwargs):
        if command == "systemctl --all | grep ceph || true":
            return "  ceph-fsid@osd.3.service   loaded active running   x\n"
        executed.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-no-finalize")
    with db_module.SessionLocal() as session:
        action = _approved_action(session, "incident-no-finalize", nodes=["10.20.1.112"])
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert not any("require-osd-release" in cmd for _host, cmd in executed)


# --- noout/noscrub/nodeep-scrub/nosnaptrim around package upgrades (2026-08-04) -
# Ceph's own cephadm orchestrator does NOT set/unset these during `ceph orch
# upgrade start` either (verified against src/pybind/mgr/cephadm/upgrade.py —
# it only manages `noautoscale`), and the package-based path has no
# orchestrator behind it at all, so nothing else protects against
# scrub/backfill churn while OSDs bounce one host at a time.


def test_execute_approved_action_package_upgrade_sets_flags_before_hosts_and_unsets_after(
    isolated_db, monkeypatch
):
    executed = []

    def fake_execute(host, command, **kwargs):
        executed.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.112,10.20.1.95")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-flags")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-flags",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=["10.20.1.83"],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    set_command = "ceph osd set noout && ceph osd set noscrub && ceph osd set nodeep-scrub && ceph osd set nosnaptrim"
    unset_command = "ceph osd unset noout; ceph osd unset noscrub; ceph osd unset nodeep-scrub; ceph osd unset nosnaptrim"
    assert ("10.20.1.112", set_command) in executed
    assert ("10.20.1.112", unset_command) in executed
    # Runs on the first configured MON node, not the (unrelated) host the
    # actual package install ran on.
    assert not any(cmd == set_command and host != "10.20.1.112" for host, cmd in executed)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        progress = json.loads(action.execution_progress)

    # Both appended (not inserted at the front — see _set_upgrade_osd_flags'
    # own comment on why that would corrupt the per-host loop's positional
    # writes into `progress`) — set-flags right after the per-host
    # placeholder(s), unset-flags right before the require-osd-release
    # finalize step at the very end. Story 7.2: some phase-scoped entries
    # (e.g. an OSD-role host with no ACTUAL osd unit discovered) never get
    # a "command" key at all — use .get() so this lookup doesn't choke on
    # those on its way to the flags steps, which always have one.
    set_step = next(p for p in progress if p.get("command") == set_command)
    unset_step = next(p for p in progress if p.get("command") == unset_command)
    assert set_step["status"] == "done"
    assert unset_step["status"] == "done"
    assert progress[-1]["command"] == "ceph osd require-osd-release octopus"
    assert progress.index(set_step) < progress.index(unset_step) < len(progress) - 1


def test_execute_approved_action_package_upgrade_proceeds_when_set_flags_fails(
    isolated_db, monkeypatch
):
    """A failure suppressing scrub/backfill must not block the actual
    (approved, expected) package install — same best-effort posture as
    the require-osd-release finalize step."""
    from worker.executor.ssh_executor import ExecutorError

    def fake_execute(host, command, **kwargs):
        if command.startswith("ceph osd set"):
            raise ExecutorError("mon busy")
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.112")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-set-fail")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-set-fail",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=["10.20.1.83"],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        # The install itself still succeeded — a setup-step hiccup doesn't
        # retroactively fail the whole upgrade.
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    # Story 7.2: .get() guard, same reasoning as the sibling test above —
    # a phase-scoped "nothing to restart here" entry has no "command" key.
    set_step = next(p for p in progress if (p.get("command") or "").startswith("ceph osd set"))
    assert set_step["status"] == "failed"
    assert set_step["error"] == "mon busy"


def test_execute_approved_action_package_upgrade_unset_failure_does_not_fail_action(
    isolated_db, monkeypatch
):
    from worker.executor.ssh_executor import ExecutorError

    def fake_execute(host, command, **kwargs):
        if command.startswith("ceph osd unset"):
            raise ExecutorError("mon unreachable")
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.112")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-unset-fail")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-unset-fail",
            action_id="upgrade_ceph_cluster_package_local",
            nodes=["10.20.1.83"],
        )
        action.action_params = json.dumps(
            {"target_version": "15.2.17", "package_dir": "/opt/ceph-packages"}
        )
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    unset_step = progress[-2]
    assert unset_step["status"] == "failed"
    assert unset_step["error"] == "mon unreachable"


def test_execute_approved_action_package_upgrade_skips_flags_when_no_mon_configured(
    isolated_db, monkeypatch, caplog
):
    executed = []

    def fake_execute(host, command, **kwargs):
        executed.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-no-mon")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-no-mon",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=["10.20.1.83"],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    with caplog.at_level("WARNING"):
        router_client._execute_approved_action(action_pk)

    # The install itself still ran on its own target node — only the
    # cluster-wide flags step (which needs a MON) was skipped.
    assert not any("ceph osd set noout" in cmd for _host, cmd in executed)
    assert "skipping" in caplog.text.lower()


# --- Story 7.2 (2026-08-04): phased MON->MGR->OSD->MDS/RGW restart ---------


def _classify_mutating_call(command: str) -> str | None:
    """Buckets a real (non-discovery) executed command into "install" or
    "restart" for the phase-order assertions below — None for anything
    else (the noout/noscrub flags bracket, require-osd-release finalize),
    which those tests don't care about ordering-wise."""
    if command == "systemctl --all | grep ceph || true":
        return None
    if "systemctl restart" in command:
        return "restart"
    if "apt-get install" in command or "dnf install" in command:
        return "install"
    return None


def test_execute_approved_action_package_upgrade_runs_install_then_mon_then_osd_in_order(
    isolated_db, monkeypatch
):
    """I/O matrix row: 3 MON + 2 separate OSD hosts — install runs on all 5
    hosts, then MON restarts on the 3 MON hosts, then OSD restarts on the 2
    OSD hosts, in that order (call-order assertion, not just final state)."""
    from config.settings import settings

    mon_hosts = ["10.20.1.1", "10.20.1.2", "10.20.1.3"]
    osd_hosts = ["10.20.1.4", "10.20.1.5"]
    nodes = mon_hosts + osd_hosts

    discovery_output = {h: f"  ceph-mon@{h}.service   loaded active running   x\n" for h in mon_hosts}
    discovery_output.update(
        {h: f"  ceph-osd@{h}.service   loaded active running   x\n" for h in osd_hosts}
    )

    calls = []

    def fake_execute(host, command, **kwargs):
        calls.append((host, command))
        if command == "systemctl --all | grep ceph || true":
            return discovery_output.get(host, "")
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", ",".join(mon_hosts))
    monkeypatch.setattr(settings, "ceph_osd_nodes", ",".join(osd_hosts))
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-phase-order")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-phase-order",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=nodes,
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    sequence = [(host, kind) for host, cmd in calls if (kind := _classify_mutating_call(cmd))]
    expected = (
        [(h, "install") for h in nodes]
        + [(h, "restart") for h in mon_hosts]
        + [(h, "restart") for h in osd_hosts]
    )
    assert sequence == expected

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    # 5 install steps + 3 mon + 2 osd = 10 phase-tagged steps (plus the
    # flags/finalize steps, not phase-tagged — see _set_upgrade_osd_flags).
    phase_counts = {}
    for step in progress:
        phase_counts[step.get("phase")] = phase_counts.get(step.get("phase"), 0) + 1
    assert phase_counts.get("install") == 5
    assert phase_counts.get("mon") == 3
    assert phase_counts.get("osd") == 2
    assert all(step["status"] == "done" for step in progress if step.get("phase"))


def test_execute_approved_action_package_upgrade_colocated_host_not_double_restarted(
    isolated_db, monkeypatch
):
    """I/O matrix row: a MON+OSD colocated host installs once, and its MON
    unit restarts in the MON phase while its OSD unit restarts in the OSD
    phase — two separate progress entries, never a double-restart of the
    same systemd unit."""
    from config.settings import settings

    host = "10.20.1.9"
    discovery_output = "  ceph-mon@a.service   loaded active running   x\n  ceph-osd@0.service   loaded active running   x\n"

    calls = []

    def fake_execute(h, command, **kwargs):
        calls.append((h, command))
        if command == "systemctl --all | grep ceph || true":
            return discovery_output
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", host)
    monkeypatch.setattr(settings, "ceph_osd_nodes", host)
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-colocated")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-colocated",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=[host],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    install_calls = [c for h, c in calls if "apt-get install" in c or "dnf install" in c]
    mon_restart_calls = [c for h, c in calls if "systemctl restart ceph-mon@a.service" in c]
    osd_restart_calls = [c for h, c in calls if "systemctl restart ceph-osd@0.service" in c]
    assert len(install_calls) == 1  # installed exactly once, not once per role
    assert len(mon_restart_calls) == 1
    assert len(osd_restart_calls) == 1

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    phase_tagged = [p for p in progress if p.get("phase") in ("install", "mon", "osd")]
    assert [p["phase"] for p in phase_tagged] == ["install", "mon", "osd"]
    assert all(p["host"] == host for p in phase_tagged)


def test_execute_approved_action_package_upgrade_restarts_leftover_rgw_host_in_final_phase(
    isolated_db, monkeypatch
):
    """I/O matrix row: a dedicated RGW box (no MON/MGR/OSD role at all) is
    installed in phase 0 and restarted in the final MDS/RGW phase."""
    from config.settings import settings

    rgw_host = "10.20.1.13"

    def fake_execute(h, command, **kwargs):
        if command == "systemctl --all | grep ceph || true":
            return "  ceph-radosgw@rgw.a.service   loaded active running   x\n"
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", rgw_host)
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-rgw-final-phase")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-rgw-final-phase",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=[rgw_host],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    install_step = next(p for p in progress if p.get("phase") == "install")
    assert install_step["status"] == "done"
    rgw_step = next(p for p in progress if p.get("phase") == "mds_rgw")
    assert rgw_step["status"] == "done"
    assert rgw_step["host"] == rgw_host
    assert "ceph-radosgw@rgw.a.service" in rgw_step["command"]
    # No MON/MGR/OSD phase entries at all — this host has none of those roles.
    assert not any(p.get("phase") in ("mon", "mgr", "osd") for p in progress)


def test_execute_approved_action_package_upgrade_host_with_no_leftover_units_gets_no_mds_rgw_step(
    isolated_db, monkeypatch
):
    """A host with nothing left to restart in the final phase gets no
    progress entry for it at all — same silent no-op posture the pre-7.2
    single command already had when _restart_discovered_units_snippet
    found nothing."""
    from config.settings import settings

    host = "10.20.1.14"

    def fake_execute(h, command, **kwargs):
        if command == "systemctl --all | grep ceph || true":
            return ""  # nothing discovered at all
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", host)
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-no-leftover-units")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-no-leftover-units",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=[host],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    assert not any(p.get("phase") == "mds_rgw" for p in progress)


# --- Code review fixes (2026-08-04) on top of Story 7.2's phased executor --


def test_execute_approved_action_package_upgrade_skips_restart_for_host_with_failed_install(
    isolated_db, monkeypatch
):
    """Fix 1: a host whose install step failed must not have its later
    MON/MGR/OSD restart command issued — restarting a daemon against a
    possibly broken/partial package install is worse than leaving it
    alone. The other (successful) host's restart must still proceed
    normally."""
    from worker.executor.ssh_executor import ExecutorError
    from config.settings import settings

    good_host = "10.20.1.30"
    bad_host = "10.20.1.31"
    nodes = [good_host, bad_host]

    executed = []

    def fake_execute(host, command, **kwargs):
        executed.append((host, command))
        if command == "systemctl --all | grep ceph || true":
            return "  ceph-mon@a.service   loaded active running   x\n"
        if host == bad_host and ("apt-get install" in command or "dnf install" in command):
            raise ExecutorError("package conflict")
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", ",".join(nodes))
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-install-fail-gates-restart")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-install-fail-gates-restart",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=nodes,
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    restart_calls = [(h, c) for h, c in executed if "systemctl restart" in c]
    assert not any(h == bad_host for h, _c in restart_calls)
    # Fail-safe rollback: once any install fails, no daemon on any host is
    # restarted and no later phase continues.
    assert not restart_calls

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        # bad_host's failed install means overall progress is not a clean
        # success — this is expected and orthogonal to the fix being tested.
        assert action.status == ActionStatus.FAILED.value
        progress = json.loads(action.execution_progress)

    bad_mon_step = next(p for p in progress if p.get("phase") == "mon" and p["host"] == bad_host)
    assert bad_mon_step["status"] == "skipped"
    assert "bảo vệ cụm" in bad_mon_step["error"]

    good_mon_step = next(p for p in progress if p.get("phase") == "mon" and p["host"] == good_host)
    assert good_mon_step["status"] == "skipped"
    assert any(p.get("phase") == "rollback" and p["status"] == "done" for p in progress)


def test_execute_approved_action_package_upgrade_unexpected_exception_still_unsets_flags(
    isolated_db, monkeypatch
):
    """Fix 4: an exception OTHER than ExecutorError propagating out of a
    phase (e.g. an unwrapped network/OS error) must still guarantee
    _unset_upgrade_osd_flags runs — otherwise noout/noscrub/nodeep-scrub/
    nosnaptrim are left set on the live cluster indefinitely."""
    from config.settings import settings

    mon_host = "10.20.1.60"
    executed = []

    def fake_execute(host, command, **kwargs):
        executed.append((host, command))
        if command == "systemctl --all | grep ceph || true":
            raise RuntimeError("ssh transport blew up")
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", mon_host)
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-unexpected-exc")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-unexpected-exc",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=[mon_host],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    with pytest.raises(RuntimeError):
        router_client._execute_approved_action(action_pk)

    unset_command = (
        "ceph osd unset noout; ceph osd unset noscrub; ceph osd unset nodeep-scrub; "
        "ceph osd unset nosnaptrim"
    )
    assert (mon_host, unset_command) in executed


def test_execute_approved_action_cephadm_upgrade_skips_router_clients_own_flags_handling(
    isolated_db, monkeypatch
):
    """cephadm's own `upgrade_ceph_cluster` action_id (ceph orch upgrade
    start) is a DIFFERENT action_id from the 2 package-based ones — its
    noout/noscrub/nodeep-scrub/nosnaptrim handling is baked directly into
    worker/executor/commands.py::_upgrade_ceph_cluster_command's own single
    command string instead (see that function's docstring for why: this
    app's own set/unset step-pair, scoped to _PACKAGE_UPGRADE_ACTION_IDS
    only, would be meaningless here — a SEPARATE `ceph osd set` SSH round
    trip achieves nothing `bash -c '... && ceph orch upgrade start ...'`
    doesn't already do in one, and there's no unset counterpart at all on
    this path — see dashboard/routes/upgrade.py's manual "Bỏ noout/
    noscrub..." button instead). This only asserts THIS app's own
    _set_upgrade_osd_flags/_unset_upgrade_osd_flags never ran as a
    SEPARATE progress step — the single upgrade command itself legitimately
    contains "ceph osd set noout" as a substring within its own chain."""
    executed = []

    def fake_execute(host, command, **kwargs):
        executed.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.112")
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")

    _create_incident("incident-cephadm-upgrade-no-flags")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-cephadm-upgrade-no-flags",
            action_id="upgrade_ceph_cluster",
            nodes=["10.20.1.112"],
        )
        action.action_params = json.dumps({"target_version": "16.2.15"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    # Exactly one SSH round trip — the single chained upgrade command.
    # A dedicated router_client.py flags step would show up as an
    # ADDITIONAL, separate command here.
    assert len(executed) == 1
    assert executed[0][1].startswith("cephadm shell -- bash -c")
    assert "ceph orch upgrade start" in executed[0][1]
    assert not any("ceph osd unset" in cmd for _host, cmd in executed)


def test_cephadm_upgrade_failure_stops_upgrade_rolls_back_flags_and_notifies(
    isolated_db, monkeypatch
):
    from worker.executor.ssh_executor import ExecutorError

    executed = []
    alerts = []

    def fake_execute(host, command, **kwargs):
        executed.append((host, command))
        if "ceph orch upgrade start" in command:
            raise ExecutorError("image pull failed")
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client, "send_update_failure_alert", lambda *a, **kw: alerts.append((a, kw)))
    monkeypatch.setattr(router_client.settings, "ceph_exec_mode", "cephadm")
    _create_incident("incident-cephadm-fail")
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-cephadm-fail")
        incident.diagnosis_text = "Không tải được image Ceph mục tiêu."
        action = _approved_action(
            session, incident.id, action_id="upgrade_ceph_cluster", nodes=["10.20.1.112"]
        )
        action.action_params = json.dumps({"target_version": "16.2.15"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert any("ceph orch upgrade stop" in command for _host, command in executed)
    assert any("ceph osd unset noout" in command for _host, command in executed)
    assert alerts and "image pull failed" in alerts[0][0][2]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.FAILED.value
        progress = json.loads(action.execution_progress)
        assert any(step.get("phase") == "rollback" and step["status"] == "done" for step in progress)


def test_execute_approved_action_marks_failed_host_in_progress(isolated_db, monkeypatch):
    from worker.executor.ssh_executor import ExecutorError

    def fake_execute(host, command, **kwargs):
        if command == "systemctl --all | grep ceph || true":
            return "  ceph-fsid@osd.0.service   loaded active running   x\n"
        raise ExecutorError(f"{host}: boom")

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-progress-fail")
    with db_module.SessionLocal() as session:
        action = _approved_action(session, "incident-progress-fail", nodes=["10.20.1.83"])
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        progress = json.loads(action.execution_progress)

    assert [{"host": p["host"], "status": p["status"]} for p in progress] == [
        {"host": "10.20.1.83", "status": "failed"}
    ]
    assert progress[0]["error"] == "10.20.1.83: boom"
    assert progress[0]["started_at"]
    assert progress[0]["finished_at"]


def test_execute_approved_action_logs_start_and_completion_per_host(
    isolated_db, monkeypatch, caplog
):
    # 2026-07-24: an operator tailing worker.log during a real (multi-minute)
    # upgrade run had no way to tell which host it was currently on vs.
    # already done — this pins the per-host progress log lines added for
    # exactly that.
    systemctl_outputs = {
        "10.20.1.112": "  ceph-fsid@osd.0.service   loaded active running   x\n",
        "10.20.1.95": "  ceph-fsid@osd.1.service   loaded active running   x\n",
    }

    def fake_execute(host, command, **kwargs):
        if command == "systemctl --all | grep ceph || true":
            return systemctl_outputs[host]
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-progress")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session, "incident-progress", nodes=["10.20.1.112", "10.20.1.95"]
        )
        action_pk = action.id

    with caplog.at_level(logging.INFO, logger="worker.llm.router_client"):
        router_client._execute_approved_action(action_pk)

    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "bắt đầu" in m and "10.20.1.112" in m and "(1/2)" in m for m in messages
    )
    assert any(
        "hoàn tất" in m and "10.20.1.112" in m and "(1/2)" in m for m in messages
    )
    assert any(
        "bắt đầu" in m and "10.20.1.95" in m and "(2/2)" in m for m in messages
    )
    assert any(
        "hoàn tất" in m and "10.20.1.95" in m and "(2/2)" in m for m in messages
    )


def test_execute_approved_action_ssh_failure_marks_failed(isolated_db, monkeypatch):
    from worker.executor.ssh_executor import ExecutorError

    def fake_execute(host, command, **kwargs):
        raise ExecutorError("boom")

    monkeypatch.setattr(router_client, "execute_command", fake_execute)

    _create_incident("incident-7b")
    with db_module.SessionLocal() as session:
        action = _approved_action(session, "incident-7b")
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.FAILED.value
        incident = session.get(Incident, "incident-7b")
        assert incident.status == IncidentStatus.FAILED.value
        entries = session.query(AuditEntry).filter_by(incident_id="incident-7b").all()
        assert entries[-1].event_type == audit.EVENT_RISKY_ACTION_FAILED


def test_execute_approved_action_no_command_defined_marks_failed(isolated_db, monkeypatch):
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command, **kwargs: pytest.fail("must not execute")
    )

    _create_incident("incident-7c")
    with db_module.SessionLocal() as session:
        action = _approved_action(session, "incident-7c", action_id="investigate_manually")
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.FAILED.value
        incident = session.get(Incident, "incident-7c")
        assert incident.status == IncidentStatus.FAILED.value


def test_execute_approved_action_malformed_target_nodes_marks_failed(isolated_db, monkeypatch):
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command, **kwargs: pytest.fail("must not execute")
    )

    _create_incident("incident-7d")
    with db_module.SessionLocal() as session:
        action = Action(
            incident_id="incident-7d",
            action_id="restart_osd_daemon",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.APPROVED.value,
            target_nodes=None,
        )
        session.add(action)
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        assert session.get(Action, action_pk).status == ActionStatus.FAILED.value


def test_execute_approved_action_skips_non_approved_action(isolated_db, monkeypatch):
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command, **kwargs: pytest.fail("must not execute")
    )

    _create_incident("incident-7g")
    with db_module.SessionLocal() as session:
        action = Action(
            incident_id="incident-7g",
            action_id="restart_osd_daemon",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.EXECUTED.value,  # already resolved by a previous tick
            target_nodes='["10.20.1.83"]',
        )
        session.add(action)
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)  # must be a no-op, not re-execute

    with db_module.SessionLocal() as session:
        assert session.get(Action, action_pk).status == ActionStatus.EXECUTED.value


# --- Story 8.1: cluster-deploy action_ids delegate to cluster_deploy.run() ---
# (cluster_deploy.run()'s own phase logic is covered exhaustively in
# tests/test_cluster_deploy.py — these tests only cover the dispatch/
# status-recording wiring in _execute_approved_action itself.)


def _approved_cluster_deploy_action(session, incident_id: str, action_params: dict | None) -> Action:
    import json as _json

    action = Action(
        incident_id=incident_id,
        action_id="deploy_cluster_cephadm",
        classification=ActionClassification.RISKY.value,
        status=ActionStatus.APPROVED.value,
        target_nodes=_json.dumps(["10.20.1.112"]),
        action_params=_json.dumps(action_params) if action_params is not None else None,
    )
    session.add(action)
    session.commit()
    return action


def test_execute_approved_action_delegates_to_cluster_deploy_for_deploy_action_ids(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, cmd: pytest.fail("must not use the generic loop")
    )
    run_calls = []

    def fake_run(action_pk, action_id, action_params, incident_id, write_progress):
        run_calls.append((action_pk, action_id, action_params, incident_id))
        return True

    monkeypatch.setattr(router_client.cluster_deploy, "run", fake_run)

    _create_incident("incident-8a")
    with db_module.SessionLocal() as session:
        action = _approved_cluster_deploy_action(session, "incident-8a", {"version": "18.2.8", "nodes": []})
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert len(run_calls) == 1
    assert run_calls[0][0] == action_pk
    assert run_calls[0][1] == "deploy_cluster_cephadm"
    assert run_calls[0][3] == "incident-8a"
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        assert action.executed_at is not None
        incident = session.get(Incident, "incident-8a")
        # 2026-08-20: lệnh chạy xong exit 0 KHÔNG còn tự động là RESOLVED —
        # Incident dừng ở VERIFYING cho tới khi watcher/verify.py hỏi lại cụm
        # và xác nhận ceph_code đã biến mất khỏi `ceph health detail`.
        assert incident.status == IncidentStatus.VERIFYING.value
        entries = session.query(AuditEntry).filter_by(incident_id="incident-8a").all()
        assert entries[-1].event_type == audit.EVENT_RISKY_ACTION_EXECUTED


def test_execute_approved_action_cluster_deploy_failure_marks_failed(isolated_db, monkeypatch):
    monkeypatch.setattr(router_client.cluster_deploy, "run", lambda *a, **kw: False)

    _create_incident("incident-8b")
    with db_module.SessionLocal() as session:
        action = _approved_cluster_deploy_action(session, "incident-8b", {"version": "18.2.8", "nodes": []})
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.FAILED.value
        incident = session.get(Incident, "incident-8b")
        assert incident.status == IncidentStatus.FAILED.value


def test_execute_approved_action_cluster_deploy_malformed_action_params_marks_failed(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(
        router_client.cluster_deploy, "run", lambda *a, **kw: pytest.fail("must not be called")
    )

    _create_incident("incident-8c")
    with db_module.SessionLocal() as session:
        action = _approved_cluster_deploy_action(session, "incident-8c", action_params=None)
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        assert session.get(Action, action_pk).status == ActionStatus.FAILED.value


# --- Volumes "Đo hiệu năng tối đa" delegates to volume_perf.run() --------
# (volume_perf.run()'s own sweep/knee-detection logic is covered
# exhaustively in tests/test_volume_perf.py — these tests only cover the
# dispatch/status-recording wiring in _execute_approved_action itself,
# same split as the cluster-deploy family above.)


def _approved_volume_perf_action(
    session, incident_id: str, action_params: dict | None, action_id: str = "volume_perf_sweep"
) -> Action:
    import json as _json

    action = Action(
        incident_id=incident_id,
        action_id=action_id,
        classification=ActionClassification.RISKY.value,
        status=ActionStatus.APPROVED.value,
        target_nodes=_json.dumps(["10.20.1.112"]),
        action_params=_json.dumps(action_params) if action_params is not None else None,
    )
    session.add(action)
    session.commit()
    return action


def test_execute_approved_action_delegates_to_volume_perf_for_volume_perf_sweep(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, cmd: pytest.fail("must not use the generic loop")
    )
    run_calls = []

    def fake_run(action_pk, action_params, incident_id, write_progress):
        run_calls.append((action_pk, action_params, incident_id))
        return True

    monkeypatch.setattr(router_client.volume_perf, "run", fake_run)

    _create_incident("incident-9a")
    with db_module.SessionLocal() as session:
        action = _approved_volume_perf_action(session, "incident-9a", {"pool": "vms", "mon_ip": "10.20.1.112"})
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert len(run_calls) == 1
    assert run_calls[0][0] == action_pk
    assert run_calls[0][1] == {"pool": "vms", "mon_ip": "10.20.1.112"}
    assert run_calls[0][2] == "incident-9a"
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        incident = session.get(Incident, "incident-9a")
        # 2026-08-20: lệnh chạy xong exit 0 KHÔNG còn tự động là RESOLVED —
        # Incident dừng ở VERIFYING cho tới khi watcher/verify.py hỏi lại cụm
        # và xác nhận ceph_code đã biến mất khỏi `ceph health detail`.
        assert incident.status == IncidentStatus.VERIFYING.value


def test_execute_approved_action_volume_perf_failure_marks_failed(isolated_db, monkeypatch):
    monkeypatch.setattr(router_client.volume_perf, "run", lambda *a, **kw: False)

    _create_incident("incident-9b")
    with db_module.SessionLocal() as session:
        action = _approved_volume_perf_action(session, "incident-9b", {"pool": "vms", "mon_ip": "10.20.1.112"})
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.FAILED.value
        incident = session.get(Incident, "incident-9b")
        assert incident.status == IncidentStatus.FAILED.value


def test_execute_approved_action_delegates_vm_benchmark_to_vm_executor(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        router_client.vm_perf,
        "run",
        lambda action_pk, params, incident_id, write_progress, cluster: calls.append(
            (action_pk, params, incident_id, cluster)
        ) or True,
    )
    monkeypatch.setattr(
        router_client.volume_perf,
        "run",
        lambda *args: pytest.fail("VM benchmark must not use Ceph-side executor"),
    )
    _create_incident("incident-vm-perf")
    params = {
        "vm_ip": "10.20.1.50",
        "ssh_user": "ubuntu",
        "ssh_key_path": "/key",
        "device": "/dev/vdb",
    }
    with db_module.SessionLocal() as session:
        action = _approved_volume_perf_action(
            session, "incident-vm-perf", params, action_id="vm_perf_benchmark"
        )
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert calls == [(action_pk, params, "incident-vm-perf", None)]
    with db_module.SessionLocal() as session:
        assert session.get(Action, action_pk).status == ActionStatus.EXECUTED.value


def test_execute_approved_action_volume_perf_malformed_action_params_marks_failed(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(
        router_client.volume_perf, "run", lambda *a, **kw: pytest.fail("must not be called")
    )

    _create_incident("incident-9c")
    with db_module.SessionLocal() as session:
        action = _approved_volume_perf_action(session, "incident-9c", action_params=None)
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        assert session.get(Action, action_pk).status == ActionStatus.FAILED.value


def test_poll_approved_actions_processes_pending_approved_rows_then_stops(isolated_db, monkeypatch):
    execute_calls = []

    def fake_execute(host, command, **kwargs):
        if command == "systemctl --all | grep ceph || true":
            return "  ceph-fsid@osd.4.service   loaded active running   x\n"
        execute_calls.append(host)
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-8a")
    _create_incident("incident-8b")
    with db_module.SessionLocal() as session:
        _approved_action(session, "incident-8a", nodes=["10.20.1.83"])
        _approved_action(session, "incident-8b", nodes=["10.20.1.78"])

    asyncio.run(router_client.poll_approved_actions(poll_interval=0, max_iterations=1))

    assert sorted(execute_calls) == ["10.20.1.78", "10.20.1.83"]
    with db_module.SessionLocal() as session:
        statuses = {a.incident_id: a.status for a in session.query(Action).all()}
        assert statuses["incident-8a"] == ActionStatus.EXECUTED.value
        assert statuses["incident-8b"] == ActionStatus.EXECUTED.value


def test_diagnose_incident_redelivery_of_already_resolved_action_restores_incident_status(
    isolated_db, monkeypatch
):
    # Simulates: incident already fully AUTO_FIXED on a prior attempt, then
    # the message gets redelivered. worker/main.py::_handle_message would
    # have already reset Incident.status to DIAGNOSING before this call.
    _create_incident("incident-5h")
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5h")
        incident.status = IncidentStatus.DIAGNOSING.value  # as _handle_message would set it
        session.add(
            Action(
                incident_id="incident-5h",
                action_id="resync_ntp",
                classification=ActionClassification.SAFE.value,
                status=ActionStatus.AUTO_EXECUTED.value,
            )
        )
        session.commit()

    execute_calls = []
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command, **kwargs: execute_calls.append(host)
    )

    envelope = dict(ENVELOPE, incident_id="incident-5h")
    asyncio.run(router_client.diagnose_incident("incident-5h", envelope))

    assert execute_calls == []  # never re-executed
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5h")
        assert incident.status == IncidentStatus.AUTO_FIXED.value  # restored, not stuck DIAGNOSING
        assert session.query(Action).filter_by(incident_id="incident-5h").count() == 1


def test_redelivery_keeps_auto_executed_case_in_verification(isolated_db, monkeypatch):
    _create_incident("incident-5h-pending-verify")
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5h-pending-verify")
        incident.status = IncidentStatus.DIAGNOSING.value
        action = Action(
            incident_id=incident.id, action_id="restart_osd_daemon",
            classification=ActionClassification.SAFE.value,
            status=ActionStatus.AUTO_EXECUTED.value,
        )
        session.add(action)
        session.flush()
        session.add(RemediationCase(
            incident_id=incident.id, action_id=action.id,
            cluster_id=incident.cluster_id, fault_family="OSD_DOWN",
            evidence_fingerprint="a" * 64, prompt_version="test-v1",
            classification="SAFE", autonomy_decision="AUTO_EXECUTE",
            playbook_version="1", outcome="EXECUTED_PENDING_VERIFY",
        ))
        session.commit()

    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(settings, "incident_verify_delay_seconds", 30)
    execute_calls = []
    monkeypatch.setattr(
        router_client, "execute_command", lambda *args, **kwargs: execute_calls.append(args)
    )
    before = datetime.utcnow()
    asyncio.run(router_client.diagnose_incident(
        "incident-5h-pending-verify",
        dict(ENVELOPE, incident_id="incident-5h-pending-verify"),
    ))

    assert execute_calls == []
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5h-pending-verify")
        assert incident.status == IncidentStatus.VERIFYING.value
        assert incident.verify_after >= before + timedelta(seconds=29)


def test_msgr2_retry_reexecutes_instead_of_only_resetting_verify_timer(isolated_db, monkeypatch):
    incident_id = "incident-msgr2-retry"
    _create_incident(incident_id)
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        incident.ceph_code = "MON_MSGR2_NOT_ENABLED"
        incident.status = IncidentStatus.DIAGNOSING.value
        action = Action(
            incident_id=incident.id, action_id="enable_mon_msgr2",
            classification=ActionClassification.SAFE.value,
            status=ActionStatus.AUTO_EXECUTED.value,
            target_nodes='["mon-a"]',
        )
        session.add(action)
        session.flush()
        session.add(RemediationCase(
            incident_id=incident.id, action_id=action.id,
            cluster_id=incident.cluster_id, fault_family="MON_MSGR2_NOT_ENABLED",
            evidence_fingerprint="b" * 64, prompt_version="test-v1",
            classification="SAFE", autonomy_decision="AUTO_EXECUTE",
            playbook_version="3", outcome="EXECUTED_PENDING_VERIFY",
        ))
        session.commit()

    async def msgr2_response(_user_content):
        return {
            "diagnosis_text": "MON lacks a v2 endpoint",
            "action_id": "enable_mon_msgr2",
            "rationale": "enable the idempotent msgr2 listener",
        }

    execute_calls = []
    monkeypatch.setattr(router_client, "_call_router", msgr2_response)
    monkeypatch.setattr(
        router_client, "execute_command",
        lambda host, command, **kwargs: execute_calls.append((host, command)) or "ok",
    )

    asyncio.run(router_client.diagnose_incident(
        incident_id,
        dict(ENVELOPE, incident_id=incident_id,
             ceph_code="MON_MSGR2_NOT_ENABLED", nodes=["mon-a"]),
    ))

    assert len(execute_calls) == 1
    assert execute_calls[0][0] == "mon-a"
    with db_module.SessionLocal() as session:
        actions = session.query(Action).filter_by(incident_id=incident_id).all()
        assert len(actions) == 1
        assert actions[0].status == ActionStatus.AUTO_EXECUTED.value
        incident = session.get(Incident, incident_id)
        assert incident.status == IncidentStatus.VERIFYING.value
        assert incident.verify_after is not None


def test_diagnose_incident_malformed_nodes_field_marks_failed_instead_of_guessing(
    isolated_db, monkeypatch
):
    execute_calls = []
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command, **kwargs: execute_calls.append(host)
    )

    _create_incident("incident-5i")
    # nodes is a bare string, not a list — must not be iterated character by character.
    envelope = dict(ENVELOPE, incident_id="incident-5i", nodes="10.20.1.249")

    asyncio.run(router_client.diagnose_incident("incident-5i", envelope))

    assert execute_calls == []
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5i")
        assert incident.status == IncidentStatus.FAILED.value


def test_every_safe_action_id_has_a_command_defined():
    from worker.backup.engine import BACKUP_ACTION_IDS
    from worker.executor.commands import _MANAGEMENT_COMMAND_BUILDERS, COMMANDS
    from worker.policy.gate import SAFE_ACTION_IDS

    # restart_osd_daemon is get_command()'s other special-cased id (handled
    # via _restart_osd_daemon_command, not the COMMANDS dict) — not
    # currently in `safe:`, but included here so this guard keeps working if
    # that ever changes, same spirit as covering the management builders.
    #
    # 2026-07-30 (Epic 9, Story 9.1): rbd_backup_run/retention_sweep_delete/
    # restore_drill_execute are the FIRST Safe action_ids dispatched via a
    # bespoke orchestrator branch (worker/backup/engine.py) instead of the
    # generic per-host commands.py lookup this guard checks — same
    # "own multi-step orchestrator, not a single Command" reasoning
    # cluster_deploy_action_ids/volume_perf_sweep already have, but those
    # two families happen to be entirely Risky (never auto-executed), so
    # this guard never had to account for a bespoke-dispatched SAFE id
    # before now.
    defined = COMMANDS.keys() | _MANAGEMENT_COMMAND_BUILDERS.keys() | {"restart_osd_daemon"} | BACKUP_ACTION_IDS
    missing = SAFE_ACTION_IDS - defined
    assert not missing, f"SAFE action_ids with no Command defined: {missing}"


def test_warn_if_missing_worker_ssh_key_logs_warning_when_path_does_not_exist(caplog, tmp_path):
    missing_path = str(tmp_path / "does-not-exist")
    with caplog.at_level("WARNING"):
        router_client._warn_if_missing_worker_ssh_key(missing_path)
    assert "does not exist" in caplog.text


def test_warn_if_missing_worker_ssh_key_silent_when_path_exists(caplog, tmp_path):
    real_path = tmp_path / "fake-key"
    real_path.write_text("not a real key")
    with caplog.at_level("WARNING"):
        router_client._warn_if_missing_worker_ssh_key(str(real_path))
    assert "does not exist" not in caplog.text


# --- AI roadmap Pha 0.3: preflight validator integration --------------------


def test_diagnose_incident_creates_action_when_preflight_blocks_but_enforcement_disabled(
    isolated_db, monkeypatch
):
    """The explicit compatibility escape hatch still works when an operator
    temporarily disables enforcement during a controlled migration."""
    from worker.preflight import PreflightResult

    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "MON clock is skewed beyond threshold; likely NTP drift.",
            "action_id": "resync_ntp",
            "rationale": "clock skew directly maps to NTP resync.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(router_client, "execute_command", lambda host, command, **kwargs: "ok")
    monkeypatch.setattr(
        router_client,
        "run_preflight",
        lambda session, *, cluster_id, action_id: PreflightResult(False, reason="no matrix entry"),
    )
    monkeypatch.setattr(settings, "ai_preflight_enforcement_enabled", False)

    _create_incident("incident-preflight-disabled")
    envelope = dict(ENVELOPE, incident_id="incident-preflight-disabled")

    asyncio.run(router_client.diagnose_incident("incident-preflight-disabled", envelope))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-preflight-disabled")
        assert incident.status != IncidentStatus.FAILED.value
        actions = session.query(Action).filter_by(incident_id="incident-preflight-disabled").all()
        assert len(actions) == 1


def test_diagnose_incident_blocks_action_when_preflight_fails_and_enforcement_enabled(
    isolated_db, monkeypatch
):
    from worker.preflight import PreflightResult

    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "MON clock is skewed beyond threshold; likely NTP drift.",
            "action_id": "resync_ntp",
            "rationale": "clock skew directly maps to NTP resync.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(router_client, "execute_command", lambda host, command, **kwargs: "ok")
    monkeypatch.setattr(settings, "ai_preflight_enforcement_enabled", True)
    monkeypatch.setattr(
        router_client,
        "run_preflight",
        lambda session, *, cluster_id, action_id: PreflightResult(
            False, reason="capability matrix has no entry for resync_ntp",
            capability_status="UNKNOWN",
        ),
    )

    _create_incident("incident-preflight-enabled")
    envelope = dict(ENVELOPE, incident_id="incident-preflight-enabled")

    asyncio.run(router_client.diagnose_incident("incident-preflight-enabled", envelope))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-preflight-enabled")
        assert incident.status == IncidentStatus.FAILED.value
        assert "capability matrix has no entry for resync_ntp" in incident.diagnosis_text
        assert session.query(Action).filter_by(incident_id="incident-preflight-enabled").count() == 0
        audit_entries = session.query(AuditEntry).filter_by(
            incident_id="incident-preflight-enabled"
        ).all()
        assert any(
            e.event_type == audit.EVENT_PROPOSAL_BLOCKED_BY_PREFLIGHT for e in audit_entries
        )


def test_diagnose_incident_creates_action_when_preflight_passes_and_enforcement_enabled(
    isolated_db, monkeypatch
):
    from worker.preflight import PreflightResult

    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "MON clock is skewed beyond threshold; likely NTP drift.",
            "action_id": "resync_ntp",
            "rationale": "clock skew directly maps to NTP resync.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(router_client, "execute_command", lambda host, command, **kwargs: "ok")
    monkeypatch.setattr(settings, "ai_preflight_enforcement_enabled", True)
    monkeypatch.setattr(
        router_client,
        "run_preflight",
        lambda session, *, cluster_id, action_id: PreflightResult(True, capability_status="SUPPORTED"),
    )

    _create_incident("incident-preflight-allowed")
    envelope = dict(ENVELOPE, incident_id="incident-preflight-allowed")

    asyncio.run(router_client.diagnose_incident("incident-preflight-allowed", envelope))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-preflight-allowed")
        assert incident.status != IncidentStatus.FAILED.value
        assert session.query(Action).filter_by(incident_id="incident-preflight-allowed").count() == 1


def test_phase0_defaults_are_fail_closed():
    from config.settings import Settings

    assert Settings.model_fields["ai_preflight_enforcement_enabled"].default is True
    assert Settings.model_fields["autopilot_enabled"].default is False
    assert Settings.model_fields["autopilot_activation_unlocked"].default is False


def test_global_autopilot_kill_switch_parks_safe_action_for_approval(isolated_db, monkeypatch):
    from worker.preflight import PreflightResult

    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(settings, "ai_preflight_enforcement_enabled", True)
    monkeypatch.setattr(settings, "autopilot_enabled", False)
    monkeypatch.setattr(
        router_client, "run_preflight",
        lambda *_args, **_kwargs: PreflightResult(True, capability_status="SUPPORTED"),
    )
    monkeypatch.setattr(
        router_client, "execute_command",
        lambda *_args, **_kwargs: pytest.fail("kill switch must block SSH execution"),
    )
    _create_incident("incident-autopilot-off")

    asyncio.run(router_client.diagnose_incident(
        "incident-autopilot-off", dict(ENVELOPE, incident_id="incident-autopilot-off")
    ))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-autopilot-off")
        action = session.query(Action).filter_by(incident_id=incident.id).one()
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.classification == ActionClassification.SAFE.value
        assert session.query(AuditEntry).filter_by(
            incident_id=incident.id,
            event_type=audit.EVENT_AUTOPILOT_KILL_SWITCH_BLOCKED,
        ).count() == 1


def test_per_cluster_kill_switch_blocks_even_when_global_switch_is_on(isolated_db, monkeypatch):
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(settings, "autopilot_enabled", True)
    monkeypatch.setattr(
        router_client, "execute_command",
        lambda *_args, **_kwargs: pytest.fail("production cluster gate must block SSH"),
    )
    with db_module.SessionLocal() as session:
        cluster = session.get(Cluster, "test-default-cluster")
        cluster.autopilot_enabled = False
        session.commit()
    _create_incident("incident-cluster-gate")

    asyncio.run(router_client.diagnose_incident(
        "incident-cluster-gate", dict(ENVELOPE, incident_id="incident-cluster-gate")
    ))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-cluster-gate")
        action = session.query(Action).filter_by(incident_id=incident.id).one()
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert session.query(AuditEntry).filter_by(
            incident_id=incident.id,
            event_type=audit.EVENT_AUTOPILOT_CLUSTER_GATE_BLOCKED,
        ).count() == 1


def test_lab_action_enters_grace_without_ssh_and_due_tick_rechecks_cluster_gate(
    isolated_db, monkeypatch,
):
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(settings, "autopilot_enabled", True)
    monkeypatch.setattr(settings, "autopilot_grace_period_seconds", 300)
    monkeypatch.setattr(
        router_client, "execute_command",
        lambda *_args, **_kwargs: pytest.fail("grace period must not dispatch SSH"),
    )
    _create_incident("incident-grace")
    asyncio.run(router_client.diagnose_incident(
        "incident-grace", dict(ENVELOPE, incident_id="incident-grace")
    ))
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-grace")
        action = session.query(Action).filter_by(incident_id=incident.id).one()
        assert incident.status == IncidentStatus.GRACE_PENDING.value
        assert action.status == ActionStatus.GRACE_PENDING.value
        assert action.grace_until is not None
        cluster = session.get(Cluster, "test-default-cluster")
        cluster.autopilot_enabled = False
        action.grace_until = datetime.utcnow() - timedelta(seconds=1)
        action_id = action.id
        session.commit()

    assert router_client._process_due_grace_actions_once() == 1
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert session.get(Incident, action.incident_id).status == IncidentStatus.PENDING_APPROVAL.value


def test_safe_execution_rechecks_preflight_immediately_before_ssh(isolated_db, monkeypatch):
    from worker.preflight import PreflightResult

    verdicts = iter([
        PreflightResult(True, capability_status="SUPPORTED"),
        PreflightResult(False, reason="cluster entered recovery", capability_status="SUPPORTED"),
    ])
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(settings, "ai_preflight_enforcement_enabled", True)
    monkeypatch.setattr(settings, "autopilot_enabled", True)
    monkeypatch.setattr(router_client, "run_preflight", lambda *_args, **_kwargs: next(verdicts))
    monkeypatch.setattr(
        router_client, "execute_command",
        lambda *_args, **_kwargs: pytest.fail("fresh preflight must block SSH"),
    )
    _create_incident("incident-preflight-race")

    asyncio.run(router_client.diagnose_incident(
        "incident-preflight-race", dict(ENVELOPE, incident_id="incident-preflight-race")
    ))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-preflight-race")
        action = session.query(Action).filter_by(incident_id=incident.id).one()
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert "cluster entered recovery" in incident.diagnosis_text


def test_operational_gate_blocks_health_err_before_ssh(isolated_db, monkeypatch):
    from worker.preflight import PreflightResult

    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(settings, "ai_preflight_enforcement_enabled", True)
    monkeypatch.setattr(settings, "autopilot_enabled", True)
    monkeypatch.setattr(
        router_client, "run_preflight",
        lambda *_args, **_kwargs: PreflightResult(True, capability_status="SUPPORTED"),
    )
    monkeypatch.setattr(
        router_client, "run_ceph_json_command_with",
        lambda *_args, **_kwargs: ("mon-a", {"health": {"status": "HEALTH_ERR"}}),
    )
    monkeypatch.setattr(
        router_client, "execute_command",
        lambda *_args, **_kwargs: pytest.fail("operational gate must block SSH"),
    )
    _create_incident("incident-health-err")

    asyncio.run(router_client.diagnose_incident(
        "incident-health-err", dict(ENVELOPE, incident_id="incident-health-err")
    ))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-health-err")
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        assert "HEALTH_ERR" in incident.diagnosis_text
        assert session.query(AuditEntry).filter_by(
            incident_id=incident.id,
            event_type=audit.EVENT_AUTOPILOT_OPERATIONAL_GATE_BLOCKED,
        ).count() == 1


def test_operational_status_retries_transient_mon_election(monkeypatch):
    calls = []

    def query(*_args, **_kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("monitor election in progress")
        return "mon-a", {"health": {"status": "HEALTH_OK"}}

    monkeypatch.setattr(router_client, "run_ceph_json_command_with", query)
    monkeypatch.setattr(router_client.time, "sleep", lambda _seconds: None)

    host, status = router_client._read_operational_status(
        (["mon-a"], "ceph-mon", "root", "/tmp/key", "none")
    )

    assert host == "mon-a"
    assert status["health"]["status"] == "HEALTH_OK"
    assert len(calls) == 3


def test_autopilot_runtime_rate_limit_and_cluster_lease(isolated_db):
    from worker.autonomy_runtime import acquire_lease, check_limits, release_lease

    now = datetime.utcnow()
    with db_module.SessionLocal() as session:
        old_incident = Incident(
            cluster_id="test-default-cluster", ceph_code="OLD", status="RESOLVED", detected_at=now,
        )
        session.add(old_incident); session.flush()
        session.add(Action(
            incident_id=old_incident.id, action_id="resync_ntp", target_nodes='["n1"]',
            classification="SAFE", status="AUTO_EXECUTED", executed_at=now - timedelta(minutes=5),
        ))
        lease_incident = Incident(
            cluster_id="test-default-cluster", ceph_code="LEASE", status="NEW", detected_at=now,
        )
        session.add(lease_incident); session.flush()
        actions = [Action(
            incident_id=lease_incident.id, action_id=f"lease-{i}", classification="SAFE", status="PENDING",
        ) for i in range(2)]
        session.add_all(actions); session.commit()

        limited = check_limits(
            session, cluster_id="test-default-cluster", action_id="resync_ntp",
            target_nodes='["n1"]', now=now, max_hour=2, max_day=5, cooldown_seconds=1800,
        )
        assert not limited.allowed and "cooldown" in limited.reason
        assert acquire_lease(
            session, cluster_id="test-default-cluster", action_id=actions[0].id,
            now=now, ttl_seconds=60,
        ).allowed
        assert not acquire_lease(
            session, cluster_id="test-default-cluster", action_id=actions[1].id,
            now=now, ttl_seconds=60,
        ).allowed
        release_lease(session, action_id=actions[0].id)
        assert acquire_lease(
            session, cluster_id="test-default-cluster", action_id=actions[1].id,
            now=now, ttl_seconds=60,
        ).allowed


def test_verified_osd_restart_is_not_suppressed_by_rate_limit_or_cooldown(isolated_db):
    from worker.autonomy_runtime import check_limits

    now = datetime.utcnow()
    with db_module.SessionLocal() as session:
        for index in range(6):
            incident = Incident(
                cluster_id="test-default-cluster", ceph_code=f"OLD-{index}",
                status="RESOLVED", detected_at=now,
            )
            session.add(incident); session.flush()
            session.add(Action(
                incident_id=incident.id, action_id="restart_osd_daemon",
                target_nodes='["n1"]', classification="SAFE",
                status="AUTO_EXECUTED", executed_at=now - timedelta(minutes=5),
            ))
        session.commit()

        result = check_limits(
            session, cluster_id="test-default-cluster",
            action_id="restart_osd_daemon", target_nodes='["n1"]', now=now,
            max_hour=2, max_day=5, cooldown_seconds=1800,
        )
        assert result.allowed is True


def test_expired_cluster_lease_is_recovered_after_worker_crash(isolated_db):
    from worker.autonomy_runtime import acquire_lease

    now = datetime.utcnow()
    with db_module.SessionLocal() as session:
        incident = Incident(
            cluster_id="test-default-cluster", ceph_code="CRASH", status="NEW", detected_at=now,
        )
        session.add(incident); session.flush()
        old_action = Action(incident_id=incident.id, action_id="old", classification="SAFE", status="EXECUTING")
        new_action = Action(incident_id=incident.id, action_id="new", classification="SAFE", status="PENDING")
        session.add_all([old_action, new_action]); session.flush()
        session.add(AutopilotLease(
            cluster_id="test-default-cluster", action_id=old_action.id,
            acquired_at=now - timedelta(minutes=20), expires_at=now - timedelta(seconds=1),
        ))
        session.commit()
        result = acquire_lease(
            session, cluster_id="test-default-cluster", action_id=new_action.id,
            now=now, ttl_seconds=60,
        )
        assert result.allowed
        assert session.query(AutopilotLease).one().action_id == new_action.id


def test_enable_mon_msgr2_is_not_suppressed_by_target_cooldown(isolated_db):
    from worker.autonomy_runtime import check_limits

    now = datetime.utcnow()
    with db_module.SessionLocal() as session:
        incident = Incident(
            cluster_id="test-default-cluster", ceph_code="MON_MSGR2_NOT_ENABLED",
            status=IncidentStatus.RESOLVED.value, detected_at=now,
        )
        session.add(incident)
        session.flush()
        session.add(Action(
            incident_id=incident.id, action_id="enable_mon_msgr2",
            target_nodes='["mon-a"]', classification="SAFE",
            status=ActionStatus.AUTO_EXECUTED.value,
            executed_at=now - timedelta(minutes=2),
        ))
        session.commit()

        result = check_limits(
            session, cluster_id="test-default-cluster",
            action_id="enable_mon_msgr2", target_nodes='["mon-a"]',
            now=now, max_hour=50, max_day=100, cooldown_seconds=1800,
        )

        assert result.allowed


def test_reconciler_keeps_live_execution_lease_untouched(isolated_db):
    from worker.autonomy_runtime import reconcile_expired_executions

    now = datetime.utcnow()
    with db_module.SessionLocal() as session:
        incident = Incident(
            cluster_id="test-default-cluster", ceph_code="LIVE", status="EXECUTING", detected_at=now,
        )
        session.add(incident); session.flush()
        action = Action(
            incident_id=incident.id, action_id="resync_ntp", classification="SAFE", status="EXECUTING",
        )
        session.add(action); session.flush()
        session.add(AutopilotLease(
            cluster_id="test-default-cluster", action_id=action.id,
            acquired_at=now, expires_at=now + timedelta(seconds=60),
        ))
        session.commit()

        assert reconcile_expired_executions(session, now=now) == 0
        assert session.get(Action, action.id).status == ActionStatus.EXECUTING.value
        assert session.query(AutopilotLease).filter_by(action_id=action.id).count() == 1
        assert session.query(AuditEntry).count() == 0


def test_db_commit_failure_after_ssh_becomes_inconclusive_without_retry(isolated_db, monkeypatch):
    """A lost DB acknowledgement after SSH must never cause the SSH command to run twice."""
    from worker.autonomy_runtime import reconcile_expired_executions

    execute_calls = []
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(
        router_client, "execute_command",
        lambda host, command, **kwargs: execute_calls.append((host, command)) or "ok",
    )
    # Both the normal outcome commit and diagnose_incident's best-effort
    # fallback fail, modelling a DB outage that begins after SSH returns.
    monkeypatch.setattr(
        router_client, "_record_execution_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    _create_incident("incident-db-commit-loss")
    envelope = dict(ENVELOPE, incident_id="incident-db-commit-loss")
    asyncio.run(router_client.diagnose_incident("incident-db-commit-loss", envelope))

    assert len(execute_calls) == 1
    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-db-commit-loss").one()
        assert action.status == ActionStatus.EXECUTING.value
        lease = session.query(AutopilotLease).filter_by(action_id=action.id).one()
        lease.expires_at = datetime.utcnow() - timedelta(seconds=1)
        session.commit()

        assert reconcile_expired_executions(session, now=datetime.utcnow()) == 1
        assert session.get(Action, action.id).status == ActionStatus.INCONCLUSIVE.value
        assert session.get(Incident, action.incident_id).status == IncidentStatus.FAILED.value
        assert session.query(AutopilotLease).filter_by(action_id=action.id).count() == 0
        audit_entry = session.query(AuditEntry).filter_by(action_id=action.id).one()
        assert audit_entry.event_type == audit.EVENT_AUTOPILOT_EXECUTION_INCONCLUSIVE
        timeline = session.query(IncidentTimelineEvent).filter_by(
            action_id=action.id, event_type="autopilot_execution_recovery",
        ).one()
        assert json.loads(timeline.evidence_json) == {
            "auto_retry": False,
            "outcome": "INCONCLUSIVE",
            "reason": "cluster execution lease expired",
        }

    # RabbitMQ redelivery sees a terminal Action and restores Incident state;
    # it must not dispatch SSH again.
    asyncio.run(router_client.diagnose_incident("incident-db-commit-loss", envelope))
    assert len(execute_calls) == 1


# --- AI roadmap Pha 0.4: safety policy hardening ----------------------------


def test_diagnose_incident_sets_expiry_and_idempotency_key(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "MON clock is skewed beyond threshold; likely NTP drift.",
            "action_id": "resync_ntp",
            "rationale": "clock skew directly maps to NTP resync.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(router_client, "execute_command", lambda host, command, **kwargs: "ok")

    _create_incident("incident-expiry")
    envelope = dict(ENVELOPE, incident_id="incident-expiry")

    before = datetime.utcnow()
    asyncio.run(router_client.diagnose_incident("incident-expiry", envelope))

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-expiry").one()
        assert action.idempotency_key is not None
        assert len(action.idempotency_key) == 64  # sha256 hex digest
        assert action.expires_at is not None
        expected = before + timedelta(hours=settings.action_approval_expiry_hours)
        assert abs((action.expires_at - expected).total_seconds()) < 5


def test_diagnose_incident_skips_duplicate_when_idempotency_key_collides(isolated_db, monkeypatch):
    # Two DIFFERENT incidents both diagnosing to the identical command
    # against the identical target while the first is still in flight
    # (PENDING) — the uq_actions_idempotency_key_inflight index must
    # refuse the second, not create a duplicate in-flight Action.
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "diagnosis",
            "action_id": "restart_osd_daemon",
            "rationale": "r",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-collide-1")
    _create_incident("incident-collide-2")
    envelope1 = dict(ENVELOPE, incident_id="incident-collide-1", nodes=["10.20.1.249"])
    envelope2 = dict(ENVELOPE, incident_id="incident-collide-2", nodes=["10.20.1.249"])

    asyncio.run(router_client.diagnose_incident("incident-collide-1", envelope1))
    asyncio.run(router_client.diagnose_incident("incident-collide-2", envelope2))

    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(incident_id="incident-collide-1").count() == 1
        assert session.query(Action).filter_by(incident_id="incident-collide-2").count() == 0


def test_diagnose_incident_allows_same_action_after_first_terminates(isolated_db, monkeypatch):
    # Once the first Action reaches a terminal status, the partial unique
    # index no longer applies — a genuinely new, later incident proposing
    # the same command must not be permanently blocked.
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "diagnosis",
            "action_id": "restart_osd_daemon",
            "rationale": "r",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-seq-1")
    envelope1 = dict(ENVELOPE, incident_id="incident-seq-1", nodes=["10.20.1.249"])
    asyncio.run(router_client.diagnose_incident("incident-seq-1", envelope1))

    with db_module.SessionLocal() as session:
        first_action = session.query(Action).filter_by(incident_id="incident-seq-1").one()
        first_action.status = ActionStatus.EXECUTED.value
        session.commit()

    _create_incident("incident-seq-2")
    envelope2 = dict(ENVELOPE, incident_id="incident-seq-2", nodes=["10.20.1.249"])
    asyncio.run(router_client.diagnose_incident("incident-seq-2", envelope2))

    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(incident_id="incident-seq-2").count() == 1


def test_maybe_execute_safe_action_refuses_destructive_hard_guard(isolated_db, monkeypatch):
    # Defense-in-depth: even if _maybe_execute_safe_action were somehow
    # called with a DESTRUCTIVE action_id, it must refuse to execute
    # rather than trust its caller's gating.
    monkeypatch.setattr(router_client.gate, "classify_action", lambda action_id: ActionClassification.DESTRUCTIVE)
    monkeypatch.setattr(
        router_client, "execute_command",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must never execute")),
    )

    _create_incident("incident-destructive-guard")
    with db_module.SessionLocal() as session:
        action = Action(
            incident_id="incident-destructive-guard",
            action_id="pg_repair_force",
            classification=ActionClassification.DESTRUCTIVE.value,
            status=ActionStatus.PENDING.value,
        )
        session.add(action)
        session.commit()
        action_pk = action.id

    router_client._maybe_execute_safe_action(
        "incident-destructive-guard", action_pk, "pg_repair_force",
        dict(ENVELOPE, incident_id="incident-destructive-guard"),
    )

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.FAILED.value


def test_playbook_contract_ceiling_routes_safe_candidate_to_approval_with_audit(isolated_db):
    _create_incident("incident-contract-block")
    with db_module.SessionLocal() as session:
        action = Action(
            incident_id="incident-contract-block",
            action_id="finalize_osd_release",
            classification=ActionClassification.SAFE.value,
            status=ActionStatus.PENDING.value,
        )
        session.add(action)
        session.commit()
        action_pk = action.id

    router_client._maybe_execute_safe_action(
        "incident-contract-block", action_pk, "finalize_osd_release",
        dict(ENVELOPE, incident_id="incident-contract-block"),
    )

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        incident = session.get(Incident, "incident-contract-block")
        events = session.query(AuditEntry.event_type).filter_by(action_id=action_pk).all()
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        assert events == [(audit.EVENT_AUTOPILOT_PLAYBOOK_CONTRACT_BLOCKED,)]
