import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.backup.ai_analysis as ai_analysis
from shared import db as db_module
from shared.db import Base
from shared.models import BackupAnomaly, BackupJob, Cluster


@pytest.fixture()
def isolated_db(monkeypatch):
    test_engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(test_engine, "connect", lambda dbapi_conn, _: dbapi_conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(test_engine)
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    )
    yield test_engine


# --- Fake AsyncOpenAI client, same shape tests/test_router_client.py uses ---


class _FakeToolCall:
    def __init__(self, name: str, args: dict):
        self.function = SimpleNamespace(name=name, arguments=json.dumps(args))


class _FakeStream:
    def __init__(self, completion):
        self._completion = completion

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_final_completion(self):
        return self._completion


def _tool_completion(name: str, args: dict, finish_reason: str = "stop"):
    message = SimpleNamespace(tool_calls=[_FakeToolCall(name, args)], content=None)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def _text_completion(content: str, finish_reason: str = "stop"):
    message = SimpleNamespace(tool_calls=[], content=content)
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

    monkeypatch.setattr(ai_analysis, "_get_client", lambda: FakeClient())


def _make_job(pool, image, job_type, status, error_message=None, created_at=None, cluster_id=None) -> BackupJob:
    with db_module.SessionLocal() as session:
        job = BackupJob(
            run_id="run-1",
            pool=pool,
            image=image,
            job_type=job_type,
            status=status,
            cluster_id=cluster_id,
            error_message=error_message,
            created_at=created_at or datetime.utcnow(),
        )
        session.add(job)
        session.commit()
        job_id = job.id
    with db_module.SessionLocal() as session:
        return session.get(BackupJob, job_id)


def _make_cluster(name: str) -> str:
    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name=name,
            ceph_mon_nodes="10.20.1.112",
            ssh_user="root",
            ssh_key_path="/tmp/test-key",
        )
        session.add(cluster)
        session.commit()
        return cluster.id


def test_call_router_extracts_args_from_matching_tool_call(monkeypatch):
    completion = _tool_completion(
        ai_analysis.TOOL_NAME,
        {"root_cause_summary_vi": "hết dung lượng", "severity": "critical", "suggested_action_vi": "dọn ổ đĩa"},
    )
    _install_fake_client(monkeypatch, completion)

    result = asyncio.run(ai_analysis._call_router("some context"))

    assert result["severity"] == "critical"


def test_call_router_raises_on_truncated_response(monkeypatch):
    completion = _tool_completion(ai_analysis.TOOL_NAME, {}, finish_reason="length")
    _install_fake_client(monkeypatch, completion)

    with pytest.raises(ai_analysis.AIAnalysisError, match="truncated"):
        asyncio.run(ai_analysis._call_router("some context"))


def test_call_router_raises_when_no_matching_tool_call(monkeypatch):
    completion = _tool_completion("some_other_tool", {})
    _install_fake_client(monkeypatch, completion)

    with pytest.raises(ai_analysis.AIAnalysisError, match="no report_backup_analysis"):
        asyncio.run(ai_analysis._call_router("some context"))


def test_analyze_redacts_before_calling_router(monkeypatch):
    completion = _tool_completion(
        ai_analysis.TOOL_NAME,
        {"root_cause_summary_vi": "x", "severity": "warning", "suggested_action_vi": "y"},
    )
    _install_fake_client(monkeypatch, completion)

    redact_calls = []
    original_redact = ai_analysis.default_redactor.redact

    def spying_redact(payload):
        redact_calls.append(payload)
        return original_redact(payload)

    monkeypatch.setattr(ai_analysis.default_redactor, "redact", spying_redact)

    context = {"pool": "vms", "image": "web01", "job_type": "full", "error_message": "boom"}
    asyncio.run(ai_analysis.analyze(context))

    assert redact_calls == [context]


def test_analyze_raises_on_incomplete_result(monkeypatch):
    completion = _tool_completion(ai_analysis.TOOL_NAME, {"root_cause_summary_vi": "", "severity": "critical", "suggested_action_vi": "x"})
    _install_fake_client(monkeypatch, completion)

    with pytest.raises(ai_analysis.AIAnalysisError):
        asyncio.run(ai_analysis.analyze({}))


def test_analyze_backup_job_calls_ai_and_alerts_critical_for_failed_job(isolated_db, monkeypatch):
    job = _make_job("vms", "web01", "full", "FAILED", error_message="disk full")

    monkeypatch.setattr(
        ai_analysis,
        "analyze",
        lambda context: _async_return(
            {"root_cause_summary_vi": "Hết dung lượng đích", "severity": "critical", "suggested_action_vi": "Mở rộng ổ đĩa"}
        ),
    )
    alerts = []
    monkeypatch.setattr(ai_analysis.alerting, "send_alert", lambda severity, message, backup_job_id=None: alerts.append((severity, message, backup_job_id)))

    ai_analysis.analyze_backup_job(job)

    assert len(alerts) == 1
    assert alerts[0][0] == "critical"
    assert "Hết dung lượng đích" in alerts[0][1]
    assert "🤖 Phân tích AI" in alerts[0][1]
    assert "🔧 Giải pháp đề xuất" in alerts[0][1]
    assert alerts[0][2] == job.id


def test_failed_job_classified_warning_still_sends_ai_solution(isolated_db, monkeypatch):
    job = _make_job(None, None, "metadata", "FAILED", error_message="python encoding error")
    monkeypatch.setattr(
        ai_analysis,
        "analyze",
        lambda context: _async_return(
            {
                "root_cause_summary_vi": "Ceph CLI Python 2 không giải mã được Unicode",
                "severity": "warning",
                "suggested_action_vi": "Ép locale C.UTF-8 hoặc loại ký tự Unicode khỏi output",
            }
        ),
    )
    alerts = []
    monkeypatch.setattr(
        ai_analysis.alerting,
        "send_alert",
        lambda severity, message, backup_job_id=None: alerts.append((severity, message)),
    )

    ai_analysis.analyze_backup_job(job)

    assert alerts == [
        (
            "warning",
            "🤖 Phân tích AI: Ceph CLI Python 2 không giải mã được Unicode\n"
            "🔧 Giải pháp đề xuất: Ép locale C.UTF-8 hoặc loại ký tự Unicode khỏi output",
        )
    ]


def test_analyze_backup_job_skips_ai_for_ordinary_success(isolated_db, monkeypatch):
    """AC #8: no anomaly, job succeeded — must NOT call the AI at all."""
    job = _make_job("vms", "web01", "full", "SUCCESS")

    calls = []
    monkeypatch.setattr(ai_analysis, "analyze", lambda context: calls.append(context) or _async_return({}))

    ai_analysis.analyze_backup_job(job, anomaly=None)

    assert calls == []


def test_analyze_backup_job_calls_ai_for_success_with_anomaly(isolated_db, monkeypatch):
    job = _make_job("vms", "web01", "full", "SUCCESS")
    monkeypatch.setattr(
        ai_analysis,
        "analyze",
        lambda context: _async_return(
            {"root_cause_summary_vi": "Thời gian chạy bất thường", "severity": "warning", "suggested_action_vi": "Theo dõi thêm"}
        ),
    )
    alerts = []
    monkeypatch.setattr(ai_analysis.alerting, "send_alert", lambda *a, **kw: alerts.append((a, kw)))

    ai_analysis.analyze_backup_job(job, anomaly={"kind": "duration", "details": "lệch 5 stddev"})

    with db_module.SessionLocal() as session:
        rows = session.query(BackupAnomaly).filter(BackupAnomaly.backup_job_id == job.id).all()
    assert len(rows) == 1
    assert rows[0].kind == "duration"
    assert rows[0].severity == "warning"
    assert rows[0].ai_summary == "Thời gian chạy bất thường"
    # warning severity: NOT sent as an immediate alert (AC #4 — accumulated for digest)
    assert alerts == []


def test_analyze_backup_job_downgrades_recovered_failure_to_warning_alert(isolated_db, monkeypatch):
    """AC #3: a later SUCCESS for the same (pool, image, job_type) already
    exists — the AI said "critical" but this must be downgraded, and no
    critical alert sent."""
    old_time = datetime.utcnow() - timedelta(hours=2)
    job = _make_job("vms", "web01", "full", "FAILED", error_message="transient network blip", created_at=old_time)
    # a later success for the SAME (pool, image, job_type)
    with db_module.SessionLocal() as session:
        session.add(
            BackupJob(
                run_id="run-2", pool="vms", image="web01", job_type="full", status="SUCCESS",
                created_at=datetime.utcnow(),
            )
        )
        session.commit()

    monkeypatch.setattr(
        ai_analysis,
        "analyze",
        lambda context: _async_return(
            {"root_cause_summary_vi": "Mất kết nối tạm thời", "severity": "critical", "suggested_action_vi": "Không cần làm gì thêm"}
        ),
    )
    alerts = []
    monkeypatch.setattr(ai_analysis.alerting, "send_alert", lambda severity, message, backup_job_id=None: alerts.append(severity))

    ai_analysis.analyze_backup_job(job)

    assert alerts == ["warning"]


def test_failure_is_not_recovered_by_success_from_another_cluster(isolated_db):
    failed_cluster_id = _make_cluster("failed-cluster")
    healthy_cluster_id = _make_cluster("healthy-cluster")
    failed = _make_job(
        "vms", "web01", "full", "FAILED",
        created_at=datetime.utcnow() - timedelta(hours=2),
        cluster_id=failed_cluster_id,
    )
    _make_job(
        "vms", "web01", "full", "SUCCESS",
        created_at=datetime.utcnow(),
        cluster_id=healthy_cluster_id,
    )

    assert ai_analysis._has_since_recovered(failed) is False


def test_analyze_backup_job_falls_back_when_ai_call_fails(isolated_db, monkeypatch):
    job = _make_job("vms", "web01", "full", "FAILED", error_message="disk full trên đích")

    def _boom(context):
        raise ai_analysis.AIAnalysisError("router unreachable")

    monkeypatch.setattr(ai_analysis, "analyze", lambda context: _async_raise(ai_analysis.AIAnalysisError("router unreachable")))
    alerts = []
    monkeypatch.setattr(ai_analysis.alerting, "send_alert", lambda severity, message, backup_job_id=None: alerts.append((severity, message)))

    ai_analysis.analyze_backup_job(job)  # must not raise

    assert len(alerts) == 1
    assert alerts[0][0] == "critical"
    assert "disk full trên đích" in alerts[0][1]


async def _async_return(value):
    return value


async def _async_raise(exc):
    raise exc
