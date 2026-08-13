import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config.settings import settings
from shared import db
from shared.db import Base
from shared.models import VitastorAnomalyEvent, VitastorEntityMetricSample
from vitastor.anomaly import detect_and_record, extract_entities, is_anomaly, robust_baseline


@pytest.fixture()
def isolated(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine); factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(settings, "vitastor_anomaly_min_samples", 5)
    return factory


def test_robust_baseline_ignores_single_large_outlier():
    median, sigma = robust_baseline([10, 10, 11, 9, 10, 500])
    assert median == 10
    assert sigma < 2


def test_zero_idle_baseline_does_not_flag_first_iops_burst():
    anomalous, _, _ = is_anomaly("read_iops", 5000, [0] * 20)
    assert anomalous is False


def test_extract_entities_covers_cluster_osd_pool_and_image_without_credentials():
    datasets = {"osds": [{"type": "osd", "name": "1", "op_stats": {}}], "pools": [{"name": "p1", "read_iops": 2}], "images": [{"pool_name": "p1", "name": "v1", "read_iops": 3}]}
    entities = extract_entities(datasets, {"io": {}})
    assert {(row["type"], row["name"]) for row in entities} == {("cluster", "cluster"), ("osd", "1"), ("pool", "p1"), ("image", "p1/v1")}
    assert "ssh" not in json.dumps(entities).lower()


def test_anomaly_opens_once_and_resolves_when_metric_recovers(isolated):
    now = datetime(2026, 8, 13, 15, 0)
    with isolated() as session:
        for index, value in enumerate([1.0, 1.1, .9, 1.0, 1.05]):
            session.add(VitastorEntityMetricSample(cluster_id="c1", entity_type="osd", entity_name="1", metrics_json=json.dumps({"read_latency_ms": value}), collected_at=now-timedelta(minutes=index+1)))
        session.commit()
    high = [{"type": "osd", "name": "1", "metrics": {"read_latency_ms": 12.0}}]
    first = detect_and_record("c1", high, now); second = detect_and_record("c1", high, now+timedelta(minutes=1))
    assert len(first["opened"]) == 1 and second["opened"] == []
    recovered = detect_and_record("c1", [{"type": "osd", "name": "1", "metrics": {"read_latency_ms": 1.0}}], now+timedelta(minutes=2))
    assert len(recovered["resolved"]) == 1
    with isolated() as session:
        event = session.query(VitastorAnomalyEvent).one()
        assert event.status == "RESOLVED" and event.resolved_at is not None
