from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config.settings import settings
from shared import db
from shared.models import Base, Cluster, Incident, IncidentStatus
from watcher import performance_rca_monitor


def test_check_and_alert_is_deduplicated_and_resolves(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(settings, "telegram_incident_bot_token", "token")
    monkeypatch.setattr(settings, "telegram_incident_chat_id", "chat")
    monkeypatch.setattr(settings, "telegram_incident_enabled", True)
    monkeypatch.setattr(settings, "telegram_performance_rca_enabled", True)

    cluster = Cluster(
        id="c1", name="cluster-1", ceph_mon_nodes="", ssh_user="test", ssh_key_path="test",
    )
    with factory() as session:
        session.add(cluster)
        session.commit()

    analysis = {
        "pool": "rbd",
        "image": "vm-a",
        "hypothesis": "host_resource_candidate",
        "confidence": 0.7,
        "current_latency_ms": 30,
        "baseline_latency_ms": 10,
        "explanation": "host disk signal",
        "host_evidence": [],
        "topology": None,
    }
    monkeypatch.setattr(
        performance_rca_monitor,
        "report",
        lambda _cluster, **_kwargs: {"analyses": [analysis]},
    )
    sent = []
    monkeypatch.setattr(
        performance_rca_monitor.telegram_alerts,
        "send_performance_rca_alert",
        lambda candidate, **_kwargs: sent.append(candidate) or True,
    )

    assert performance_rca_monitor.check_and_alert("c1", cluster) == 1
    assert performance_rca_monitor.check_and_alert("c1", cluster) == 0
    assert len(sent) == 1

    with factory() as session:
        incident = session.query(Incident).one()
        assert incident.telegram_reminded_at is not None
        assert incident.status == IncidentStatus.NEW.value

    monkeypatch.setattr(performance_rca_monitor, "report", lambda _cluster, **_kwargs: {"analyses": []})
    assert performance_rca_monitor.check_and_alert("c1", cluster) == 0
    with factory() as session:
        assert session.query(Incident).one().status == IncidentStatus.RESOLVED.value

    # A transient evidence gap must not create/send a new alert immediately
    # when the same candidate returns on the next scan.
    monkeypatch.setattr(
        performance_rca_monitor,
        "report",
        lambda _cluster, **_kwargs: {"analyses": [analysis]},
    )
    assert performance_rca_monitor.check_and_alert("c1", cluster) == 0
    assert len(sent) == 1
    with factory() as session:
        assert session.query(Incident).count() == 1

    monkeypatch.setattr(settings, "telegram_performance_rca_enabled", False)
    monkeypatch.setattr(performance_rca_monitor, "report", lambda _cluster, **_kwargs: {"analyses": [analysis]})
    assert performance_rca_monitor.check_and_alert("c1", cluster) == 0
    assert len(sent) == 1

    Base.metadata.drop_all(engine)
