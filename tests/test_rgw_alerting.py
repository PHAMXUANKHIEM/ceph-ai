from uuid import uuid4
from types import SimpleNamespace
from datetime import timedelta

from shared import db
from shared.models import Cluster, Incident, IncidentStatus, RgwMetricSnapshot
from shared.time import utc_now
from watcher.rgw_alerting import (
    collect_bucket_quota_stats,
    evaluate_rgw_alerts,
    persist_rgw_metric_snapshot,
    snapshot_from_audit_rows,
    sync_rgw_alerts,
)


def _snapshot(*, request_count=100, status_counts=None, top_buckets=None, **extra):
    return {
        "metrics": {
            "request_count": request_count,
            "status_counts": status_counts or {"200": request_count},
            "top_buckets": top_buckets or {},
        },
        **extra,
    }


def test_rgw_alert_rules_cover_5xx_denied_hot_bucket_and_quota():
    result = evaluate_rgw_alerts(_snapshot(
        request_count=100,
        status_counts={"200": 35, "403": 15, "500": 50},
        top_buckets={"hot": 80, "other": 20},
        bucket_stats=[{
            "bucket": "nearly-full", "quota_enabled": True,
            "size_bytes": 95, "quota_max_size_bytes": 100,
        }],
    ))

    by_code = {alert["code"]: alert for alert in result["alerts"]}
    assert by_code["RGW_ALERT_5XX_SPIKE"]["severity"] == "HEALTH_ERR"
    assert by_code["RGW_ALERT_ACCESS_DENIED_SPIKE"]["observed"]["count"] == 15
    assert by_code["RGW_ALERT_HOT_BUCKET"]["observed"]["bucket"] == "hot"
    assert by_code["RGW_ALERT_BUCKET_QUOTA"]["observed"]["ratio"] == 0.95
    assert all(alert["read_only"] is True and alert["action_id"] is None for alert in result["alerts"])


def test_rgw_alert_rules_are_quiet_below_threshold_and_fail_closed_on_missing_data():
    result = evaluate_rgw_alerts(_snapshot(
        request_count=100,
        status_counts={"200": 96, "403": 4},
        top_buckets={"hot": 49, "other": 49},
        bucket_stats=[{
            "bucket": "safe", "quota_enabled": True,
            "size_bytes": 79, "quota_max_size_bytes": 100,
        }],
    ))
    assert result["alerts"] == []

    missing = evaluate_rgw_alerts({})
    assert missing["alerts"] == []
    assert missing["evidence_gaps"]


def test_snapshot_from_audit_rows_is_bounded_and_secret_free():
    snapshot = snapshot_from_audit_rows([
        {"http_status": 403, "bucket": "archive", "secret_key": "must-not-appear"},
        {"status": 200, "bucket": "archive"},
    ])

    assert snapshot["metrics"]["request_count"] == 2
    assert snapshot["metrics"]["status_counts"] == {"403": 1, "200": 1}
    assert snapshot["metrics"]["top_buckets"] == {"archive": 2}
    assert "secret_key" not in str(snapshot)


def test_snapshot_wires_audit_intelligence_into_abnormal_access_alerts():
    rows = [
        {
            "method": "PUT", "http_status": 200, "bucket": "public",
            "requester": "anonymous", "remote_addr": "198.51.100.7",
        },
    ]
    snapshot = snapshot_from_audit_rows(rows)
    result = evaluate_rgw_alerts(snapshot)

    assert any(alert["code"] == "RGW_ALERT_ANONYMOUS_SUCCESSFUL_WRITE" for alert in result["alerts"])


def test_bucket_quota_collector_is_bounded_and_uses_secondary_cluster_credentials(monkeypatch):
    from watcher import rgw_alerting

    cluster = SimpleNamespace(id="secondary-rgw", is_default=False)
    monkeypatch.setattr(
        rgw_alerting,
        "configured_nodes",
        lambda current: [{"host": "rgw-secondary", "roles": ["RGW"]}],
    )
    monkeypatch.setattr(
        rgw_alerting,
        "resolve_ssh_creds",
        lambda current: ("rgw-user", "/keys/secondary", "podman", "rgw-container"),
    )
    monkeypatch.setattr(
        rgw_alerting,
        "fetch_bucket_list_with",
        lambda *args: ["archive", "images"],
    )
    monkeypatch.setattr(
        rgw_alerting,
        "fetch_bucket_stats_with",
        lambda host, bucket, *args: {"bucket": bucket},
    )
    monkeypatch.setattr(
        rgw_alerting,
        "summarize_bucket_stats",
        lambda raw: {
            "quota_enabled": True, "size_bytes": 90,
            "quota_max_size_bytes": 100, "num_objects": 1,
            "quota_max_objects": 10,
        },
    )

    stats, gaps = collect_bucket_quota_stats(cluster, max_buckets=1)

    assert [row["bucket"] for row in stats] == ["archive"]
    assert any("1/2" in gap for gap in gaps)


def test_rgw_incident_target_is_rendered_as_a_safe_bucket_deep_link():
    from dashboard.routes.incidents import _rgw_incident_target

    incident = SimpleNamespace(
        ceph_code="RGW_ALERT_BUCKET_QUOTA",
        signal_evidence_json='{"target": {"type": "bucket", "id": "archive/test"}}',
    )

    target = _rgw_incident_target(incident)

    assert target["label"] == "Bucket archive/test"
    assert target["url"] == "/object-storage/buckets/archive%2Ftest"


def test_metric_snapshot_persists_aggregates_and_prunes_old_rows(monkeypatch):
    from watcher import rgw_alerting

    cluster_id = f"rgw-metrics-{uuid4()}"
    now = utc_now()
    monkeypatch.setattr(rgw_alerting.settings, "rgw_metric_snapshot_retention_days", 1)
    with db.SessionLocal() as session:
        session.add(RgwMetricSnapshot(
            cluster_id=cluster_id,
            captured_at=now - timedelta(days=2),
            request_count=999,
        ))
        session.commit()

    snapshot = _snapshot(
        request_count=12,
        status_counts={"200": 10, "500": 2},
        top_buckets={"archive": 12},
    )
    snapshot["metrics"].update({
        "bytes_total": 4096,
        "error_count": 2,
        "error_rate_percent": 16.6667,
        "latency_p95_ms": 22.5,
        "top_requesters": {"operator": 12},
    })
    persist_rgw_metric_snapshot(cluster_id, snapshot, now=now)

    with db.SessionLocal() as session:
        rows = session.query(RgwMetricSnapshot).filter_by(cluster_id=cluster_id).all()
        assert len(rows) == 1
        assert rows[0].request_count == 12
        assert rows[0].bytes_total == 4096
        assert rows[0].top_buckets_json == '{"archive": 12}'


def test_rgw_metrics_history_api_returns_bounded_aggregate_only(dashboard_client, default_cluster_id):
    with db.SessionLocal() as session:
        session.add(RgwMetricSnapshot(
            cluster_id=default_cluster_id,
            captured_at=utc_now(),
            available=True,
            request_count=12,
            bytes_total=4096,
            error_count=2,
            error_rate_percent=16.67,
            latency_p95_ms=22.5,
            top_buckets_json='{"archive": 12}',
            top_requesters_json='{"operator": 12}',
            evidence_gaps_json='[]',
        ))
        session.commit()
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    response = dashboard_client.get("/api/object-storage/rgw-metrics/history?hours=24")

    assert response.status_code == 200
    body = response.json()
    assert body["cluster_id"] == default_cluster_id
    assert body["items"][0]["request_count"] == 12
    assert body["items"][0]["top_buckets"] == {"archive": 12}
    assert body["read_only"] is True


def test_rgw_alert_incident_lifecycle_deduplicates_and_resolves_per_cluster():
    cluster_id = f"rgw-alert-{uuid4()}"
    with db.SessionLocal() as session:
        cluster = Cluster(
            name=cluster_id, ceph_mon_nodes="", ssh_user="tester", ssh_key_path="/tmp/test-key",
            is_default=False, is_active=True,
        )
        session.add(cluster)
        session.commit()

        snapshot = _snapshot(
            request_count=100,
            status_counts={"200": 80, "500": 20},
        )
        first = sync_rgw_alerts(session, cluster, snapshot, send_notifications=False)
        second = sync_rgw_alerts(session, cluster, snapshot, send_notifications=False)
        assert first["created"] == 1
        assert second["created"] == 0
        assert second["updated"] == 1

        rows = session.query(Incident).filter(Incident.cluster_id == cluster.id).all()
        assert len(rows) == 1
        assert rows[0].status == IncidentStatus.NEW.value

        resolved = sync_rgw_alerts(
            session,
            cluster,
            _snapshot(request_count=100, status_counts={"200": 100}),
            send_notifications=False,
        )
        assert resolved["resolved"] == 1
        assert rows[0].status == IncidentStatus.RESOLVED.value


def test_rgw_alert_incidents_are_cluster_scoped():
    with db.SessionLocal() as session:
        first_cluster = Cluster(
            name=f"rgw-a-{uuid4()}", ceph_mon_nodes="", ssh_user="tester", ssh_key_path="/tmp/test-key",
            is_default=False, is_active=True,
        )
        second_cluster = Cluster(
            name=f"rgw-b-{uuid4()}", ceph_mon_nodes="", ssh_user="tester", ssh_key_path="/tmp/test-key",
            is_default=False, is_active=True,
        )
        session.add_all([first_cluster, second_cluster])
        session.commit()
        snapshot = _snapshot(request_count=100, status_counts={"200": 80, "500": 20})

        sync_rgw_alerts(session, first_cluster, snapshot, send_notifications=False)
        untouched = sync_rgw_alerts(
            session, second_cluster,
            _snapshot(request_count=100, status_counts={"200": 100}),
            send_notifications=False,
        )
        assert untouched["resolved"] == 0


def test_quota_alerts_notify_on_80_90_95_band_transitions(monkeypatch):
    from watcher import rgw_alerting

    delivered = []
    monkeypatch.setattr(
        rgw_alerting.telegram_alerts,
        "send_incident_alert",
        lambda *args, **kwargs: delivered.append(args[0]),
    )
    with db.SessionLocal() as session:
        cluster = Cluster(
            name=f"rgw-quota-{uuid4()}", ceph_mon_nodes="", ssh_user="tester",
            ssh_key_path="/tmp/test-key", is_default=False, is_active=True,
        )
        session.add(cluster)
        session.commit()

        def quota_snapshot(value):
            return _snapshot(
                request_count=1,
                status_counts={"200": 1},
                bucket_stats=[{
                    "bucket": "archive", "quota_enabled": True,
                    "size_bytes": value, "quota_max_size_bytes": 100,
                }],
            )

        first = sync_rgw_alerts(session, cluster, quota_snapshot(80), send_notifications=True)
        second = sync_rgw_alerts(session, cluster, quota_snapshot(90), send_notifications=True)
        third = sync_rgw_alerts(session, cluster, quota_snapshot(91), send_notifications=True)
        fourth = sync_rgw_alerts(session, cluster, quota_snapshot(95), send_notifications=True)

        assert first["created"] == 1
        assert second["updated"] == 1
        assert third["updated"] == 1
        assert fourth["updated"] == 1
        assert len(delivered) == 3
