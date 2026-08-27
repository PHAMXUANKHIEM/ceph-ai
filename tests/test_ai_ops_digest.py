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
