from datetime import datetime

from shared import db
from shared.models import CephCapacitySample, Cluster, Incident, IncidentStatus
from worker.ai_ops_digest import build_digest


def test_digest_aggregates_default_cluster_and_legacy_rows(dashboard_client):
    with db.SessionLocal() as session:
        cluster = session.query(Cluster).filter_by(is_default=True).first()
        session.add_all([
            Incident(cluster_id=None, ceph_code="HEALTH_WARN", status=IncidentStatus.NEW.value,
                     severity="WARNING", log_excerpt="x", detected_at=datetime(2026, 8, 27)),
            CephCapacitySample(cluster_id=cluster.id, entity_type="cluster", entity_name="cluster",
                               used_bytes=90, total_bytes=100, used_percent=90,
                               captured_at=datetime(2026, 8, 27)),
            # An older high-water mark must not leak into a seven-day digest.
            CephCapacitySample(cluster_id=cluster.id, entity_type="cluster", entity_name="cluster",
                               used_bytes=99, total_bytes=100, used_percent=99,
                               captured_at=datetime(2026, 8, 1)),
        ])
        session.commit()
    rows = build_digest(now=datetime(2026, 8, 27, 12, 0))
    assert rows and "7 ngày" in rows[0][1]
    assert "Incident: 1" in rows[0][1] and "90.0%" in rows[0][1]
    assert "99.0%" not in rows[0][1]


def _evidence(scored, cluster_key="cluster-a", decision="KEEP_SHADOW"):
    return {
        "execution_mode": "SHADOW_ONLY",
        "scopes": [
            {"cluster_key": cluster_key, "host": "h1", "metric": "cpu", "verified": 200, "scored": scored},
            {"cluster_key": "other-cluster", "host": "h9", "metric": "cpu", "verified": 999, "scored": 999},
        ],
        "verdict": {"decision": decision, "reasons": ["clusters covered 1 < 2"]},
    }


def test_learning_lines_count_only_this_clusters_scopes_and_the_weekly_change():
    from types import SimpleNamespace

    from worker.ai_ops_digest import learning_lines

    cluster = SimpleNamespace(id="cluster-a", is_default=False)

    lines = learning_lines(cluster, _evidence(12), previous=_evidence(2))

    assert lines[0] == "Online learning (SHADOW_ONLY): 200 kết quả đã xác minh · 12 đã học (+10 so với tuần trước)"
    assert lines[1] == "Đánh giá nâng cấp mô hình: KEEP_SHADOW — clusters covered 1 < 2"
    assert learning_lines(cluster, None) == []
    assert "so với tuần trước" not in learning_lines(cluster, _evidence(12))[0]


def test_default_cluster_also_counts_legacy_default_scopes():
    from types import SimpleNamespace

    from worker.ai_ops_digest import learning_lines

    cluster = SimpleNamespace(id="cluster-a", is_default=True)

    assert "200 kết quả" in learning_lines(cluster, _evidence(3, cluster_key="__default__"))[0]


def test_weekly_evidence_is_saved_per_iso_week_and_read_back_next_week(tmp_path):
    from worker.ai_ops_digest import previous_learning_evidence, write_learning_evidence

    monday = datetime(2026, 10, 5, 8, 0)
    path = write_learning_evidence(_evidence(4), str(tmp_path), monday)

    assert path.name == "river-v2-evidence-2026-W41.json"
    assert previous_learning_evidence(str(tmp_path), datetime(2026, 10, 12, 8, 0))["scopes"][0]["scored"] == 4
    assert previous_learning_evidence(str(tmp_path), monday) is None
    assert write_learning_evidence(None, str(tmp_path), monday) is None
    assert write_learning_evidence(_evidence(1), "", monday) is None


def test_run_digest_sends_the_learning_lines_and_saves_the_evidence(dashboard_client, monkeypatch, tmp_path):
    from worker import ai_ops_digest

    with db.SessionLocal() as session:
        cluster = session.query(Cluster).filter_by(is_default=True).first()
        cluster_id = cluster.id
    sent = []
    monkeypatch.setattr(ai_ops_digest, "send_ai_ops_digest_alert", lambda text, **kw: sent.append(text))
    monkeypatch.setattr(ai_ops_digest, "_learning_evidence", lambda now: _evidence(7, cluster_key=cluster_id))
    monkeypatch.setattr(ai_ops_digest.settings, "learning_evidence_report_dir", str(tmp_path))
    monkeypatch.setattr(ai_ops_digest.settings, "ai_ops_weekly_digest_enabled", True)

    ai_ops_digest.run_digest()

    assert sent and "200 kết quả đã xác minh · 7 đã học" in sent[0]
    assert len(list(tmp_path.glob("river-v2-evidence-*.json"))) == 1


def test_digest_survives_an_evidence_failure(dashboard_client, monkeypatch):
    from worker import ai_ops_digest

    def boom(*_args, **_kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(ai_ops_digest.river_v2_evidence, "build_report", boom)

    assert ai_ops_digest._learning_evidence(datetime(2026, 10, 5)) is None
