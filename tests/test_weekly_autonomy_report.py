import json
from datetime import datetime, timedelta

from shared import db, weekly_autonomy_report as war
from shared.models import (
    Action, AutonomyDecision, Cluster, Incident, IncidentEvidence, IncidentTimelineEvent, PlaybookStat,
    RemediationCase,
)
from worker import ai_ops_digest

NOW = datetime(2026, 9, 28, 8, 0)


def _seed(cluster):
    with db.SessionLocal() as session:
        codes = ["NODE_UNREACHABLE:10.0.0.9", "NODE_UNREACHABLE:10.0.0.8", "OSD_LATENCY_HIGH:3"]
        incidents = []
        for index, code in enumerate(codes):
            incident = Incident(cluster_id=cluster.id, ceph_code=code, status="RESOLVED", severity="HEALTH_WARN",
                                detected_at=NOW - timedelta(days=1), created_at=NOW - timedelta(days=1, minutes=index))
            session.add(incident)
            session.flush()
            incidents.append(incident)
        action = Action(incident_id=incidents[0].id, action_id="resync_ntp", classification="SAFE", status="EXECUTED")
        session.add(action)
        session.flush()
        case = RemediationCase(incident_id=incidents[0].id, action_id=action.id, cluster_id=cluster.id,
                               fault_family="NODE_UNREACHABLE", evidence_fingerprint="a" * 64, prompt_version="v1",
                               classification="SAFE", autonomy_decision="AUTO_EXECUTE", outcome="PROPOSED",
                               operator_verdict="CORRECT", operator_verdict_at=NOW - timedelta(days=2))
        session.add(case)
        session.flush()
        session.add(AutonomyDecision(case_id=case.id, incident_id=incidents[0].id, cluster_id=cluster.id,
                                     fault_family="NODE_UNREACHABLE", context_json="{}", candidates_json="[]",
                                     chosen_action="resync_ntp", chosen_by="rules", propensity=1.0,
                                     policy_version="v1", created_at=NOW - timedelta(days=1),
                                     shadow_recommendation="escalate",
                                     shadow_reasons_json=json.dumps(["không có rollback đã đăng ký"])))
        session.add(IncidentEvidence(incident_id=incidents[0].id, runbook="NODE_UNREACHABLE", collector_id="mon_ping",
                                     target="mon", status="ok", created_at=NOW - timedelta(days=1)))
        for incident, conclusion in ((incidents[0], "TRANSIENT"), (incidents[2], "UNKNOWN")):
            session.add(IncidentTimelineEvent(incident_id=incident.id, event_type="triage_concluded", actor="watcher",
                                              evidence_json=json.dumps({"conclusion": conclusion}),
                                              created_at=NOW - timedelta(days=1)))
        scope = f"cluster={cluster.id}|ceph_major=19|deployment=cephadm"
        session.add_all([
            PlaybookStat(playbook_id="resync_ntp", playbook_version="v1", scope_key=scope, verified_count=12,
                         trust_score=0.7, maturity_level="L2"),
            PlaybookStat(playbook_id="restart_osd_daemon", playbook_version="v1", scope_key=scope, verified_count=25,
                         trust_score=0.9, maturity_level="L2"),
            PlaybookStat(playbook_id="pg_repair_force", playbook_version="v1", scope_key=scope, verified_count=30,
                         trust_score=0.95, maturity_level="L1", auto_disabled_reason="operator marked a case unsafe"),
            PlaybookStat(playbook_id="other_cluster", playbook_version="v1",
                         scope_key="cluster=someone-else|ceph_major=19|deployment=cephadm", verified_count=30,
                         trust_score=0.99, maturity_level="L2"),
        ])
        session.commit()


def _default_cluster():
    with db.SessionLocal() as session:
        cluster = session.query(Cluster).filter_by(is_default=True).first()
        session.expunge(cluster)
        return cluster


def test_weekly_report_sections(dashboard_client):
    cluster = _default_cluster()
    _seed(cluster)
    with db.SessionLocal() as session:
        report = war.build(session, cluster, now=NOW)
    assert report["errors"] == {}
    assert report["noise"]["incidents"] == 3
    assert tuple(report["noise"]["top_families"][0]) == ("NODE_UNREACHABLE", 2)
    assert report["verdicts"] == {"new": {"CORRECT": 1}, "new_total": 1, "labelled_total": 1, "label_target": 200}
    assert report["evidence"]["incidents_investigated"] == 1
    assert (report["evidence"]["triaged"], report["evidence"]["rule_concluded"]) == (2, 1)
    assert report["decisions"]["by_source"] == {"rules": 1}
    assert report["decisions"]["shadow"] == {"escalate": 1}
    assert [p["playbook"] for p in report["playbooks"]["ready"]] == ["restart_osd_daemon"]
    assert [p["playbook"] for p in report["playbooks"]["closest"]] == ["resync_ntp"]
    text = "\n".join(war.format_lines(report))
    assert "Verdict mới: 1 (CORRECT 1); tổng 1/200" in text
    assert "luật kết luận 1/2 (50.0%)" in text
    assert "Shadow (không hành động): execute 0 · escalate 1 — lý do chính: không có rollback đã đăng ký (1)" in text
    assert "Đủ ngưỡng Trust Engine (chờ operator duyệt nâng quyền): restart_osd_daemon" in text
    assert "resync_ntp 12/20 mẫu, trust 0.70" in text
    assert "pg_repair_force" not in text and "other_cluster" not in text
    json.dumps(report, default=str)


def test_a_failing_section_is_reported_and_the_rest_survive(dashboard_client, monkeypatch):
    cluster = _default_cluster()

    def boom(*args, **kwargs):
        raise RuntimeError("kpi down")

    monkeypatch.setattr(war.autonomy_kpi, "collect", boom)
    with db.SessionLocal() as session:
        report = war.build(session, cluster, now=NOW)
    assert report["noise"] is None and report["errors"] == {"noise": "RuntimeError"}
    assert report["verdicts"] is not None
    assert "Thiếu mục: noise" in "\n".join(war.format_lines(report))


def test_digest_appends_the_autonomy_section_and_writes_json(dashboard_client, tmp_path):
    cluster = _default_cluster()
    _seed(cluster)
    reports = []
    rows = ai_ops_digest.build_digest(now=NOW, reports=reports)
    text = dict(rows)[cluster.name]
    assert "🤖 Tự vận hành (autonomy):" in text and "Nguồn quyết định: rules 1" in text
    assert text.rstrip().endswith("không tự thực thi thao tác.")
    path = ai_ops_digest.write_reports(reports, str(tmp_path), NOW)
    assert path.name == "weekly-autonomy-2026-W40.json"
    assert json.loads(path.read_text(encoding="utf-8"))[0]["schema"] == war.SCHEMA
    assert ai_ops_digest.write_reports(reports, "", NOW) is None


def test_digest_still_sends_when_the_autonomy_report_fails(dashboard_client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(ai_ops_digest.weekly_autonomy_report, "build", boom)
    rows = ai_ops_digest.build_digest(now=NOW)
    assert rows and "🤖" not in rows[0][1] and "Incident:" in rows[0][1]


def test_report_constants_match_the_evidence_module():
    from shared import incident_evidence

    assert war.EVENT_TRIAGED == incident_evidence.EVENT_TRIAGED
    assert war.SKIPPED_FLAPPING == incident_evidence.SKIPPED_FLAPPING
