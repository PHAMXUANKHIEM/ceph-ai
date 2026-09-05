import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db
from shared.db import Base
from shared.models import VitastorCluster, VitastorMetricSample, VitastorOsdMetricSample
from vitastor.client import VitastorConnectionError
from watcher import vitastor_monitor


@pytest.fixture()
def cluster(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(vitastor_monitor, "query_node_hardware", lambda host, *_: {"host": host, "osd_processes": 1, "cpu_percent": 2, "ram_bytes": 1024, "devices": []})
    monkeypatch.setattr(vitastor_monitor, "query_node_network", lambda source, targets, *_: {"source": source, "interfaces": [], "probes": [{"target": target, "reachable": True, "rtt_ms": 1, "jumbo_9000": True} for target in targets]})
    with factory() as session:
        row = VitastorCluster(
            name="vita-prod", management_host="10.0.0.20", etcd_address="etcd:2379",
            etcd_prefix="/vitastor", config_path="", ssh_user="root", ssh_key_path="/key",
            exec_mode="none", container_name="", created_by="admin",
        )
        session.add(row); session.commit(); session.expunge(row)
    return row


def _data(osd_up=3, osd_count=3):
    return {"status": {
        "etcd_alive": 1, "etcd_count": 1, "osd_up": osd_up, "osd_count": osd_count,
        "pool_count": 1, "active_pool_count": 1, "pg_states": {"active": 8},
    }, "pools": [], "osds": [], "images": [], "errors": {}}


def test_alerts_once_on_problem_and_once_on_recovery(cluster, monkeypatch):
    alerts = []
    monkeypatch.setattr(vitastor_monitor, "send_vitastor_alert", lambda *args: alerts.append(args))
    monkeypatch.setattr(vitastor_monitor, "query_dashboard", lambda *_: _data(2, 3))

    assert vitastor_monitor.poll_cluster_once(cluster) == "CRITICAL"
    assert vitastor_monitor.poll_cluster_once(cluster) == "CRITICAL"
    assert [item[1] for item in alerts] == ["CRITICAL"]

    monkeypatch.setattr(vitastor_monitor, "query_dashboard", lambda *_: _data())
    assert vitastor_monitor.poll_cluster_once(cluster) == "HEALTHY"
    assert vitastor_monitor.poll_cluster_once(cluster) == "HEALTHY"
    assert [item[1] for item in alerts] == ["CRITICAL", "HEALTHY"]


def test_connection_failure_is_deduplicated_and_preserves_cached_data(cluster, monkeypatch):
    cluster.last_status_json = json.dumps({"deployment": {"nodes": ["10.0.0.20"]}})
    with db.SessionLocal() as session:
        row = session.get(VitastorCluster, cluster.id)
        row.last_status_json = cluster.last_status_json
        session.commit()
    alerts = []
    monkeypatch.setattr(vitastor_monitor, "send_vitastor_alert", lambda *args: alerts.append(args))
    def fail(*_): raise VitastorConnectionError("timeout")
    monkeypatch.setattr(vitastor_monitor, "query_dashboard", fail)

    vitastor_monitor.poll_cluster_once(cluster)
    vitastor_monitor.poll_cluster_once(cluster)

    assert [item[1] for item in alerts] == ["UNREACHABLE"]
    with db.SessionLocal() as session:
        cache = json.loads(session.get(VitastorCluster, cluster.id).last_status_json)
        assert cache["deployment"]["nodes"] == ["10.0.0.20"]
        assert cache["_telegram_health"] == "UNREACHABLE"


def test_successful_poll_records_cluster_and_per_osd_history(cluster, monkeypatch):
    data = _data()
    data["status"].update({
        "total_raw": 1000, "free_raw": 250,
        "op_stats": {"read": {"iops": 12, "bps": 1200, "latency_us": 500}, "write": {"iops": 7, "bps": 700, "latency_us": 800}},
        "recovery_stats": {"degraded": {"bps": 333}}, "degraded_data": 64,
    })
    data["osds"] = [{
        "type": "osd", "name": 1, "parent": "node-a", "up": True,
        "size": 500, "free": 100,
        "op_stats": {"read": {"iops": 5, "bps": 500, "latency_us": 400}, "write": {"iops": 3, "bps": 300, "latency_us": 900}},
    }]
    monkeypatch.setattr(vitastor_monitor, "query_dashboard", lambda *_: data)
    monkeypatch.setattr(vitastor_monitor, "send_vitastor_alert", lambda *_: None)
    vitastor_monitor.poll_cluster_once(cluster)
    with db.SessionLocal() as session:
        sample = session.query(VitastorMetricSample).one()
        osd = session.query(VitastorOsdMetricSample).one()
        saved_cluster = session.get(VitastorCluster, cluster.id)
        assert saved_cluster.last_checked_at is not None
        assert json.loads(saved_cluster.last_status_json)["checked_at"].endswith("Z")
        assert sample.used_percent == 75
        assert sample.read_latency_ms == 0.5
        assert sample.recovery_bps == 333
        assert osd.osd_id == "1" and osd.used_percent == 80
        assert osd.write_latency_ms == 0.9


def test_capacity_alerts_transition_without_spam_for_cluster_and_osd(cluster, monkeypatch):
    alerts = []
    data = _data()
    data["status"].update({"total_raw": 1000, "free_raw": 100})
    data["osds"] = [{"type": "osd", "name": 7, "parent": "node-a", "up": True, "size": 1000, "free": 140}]
    monkeypatch.setattr(vitastor_monitor, "query_dashboard", lambda *_: data)
    monkeypatch.setattr(vitastor_monitor, "send_vitastor_alert", lambda *args: alerts.append(args))

    vitastor_monitor.poll_cluster_once(cluster)
    vitastor_monitor.poll_cluster_once(cluster)
    capacity = [item for item in alerts if "Dung lượng" in item[2]]
    assert [(item[1], "Toàn cụm" in item[2], "OSD 7" in item[2]) for item in capacity] == [
        ("CRITICAL", True, False), ("WARNING", False, True),
    ]

    data["status"]["free_raw"] = 200
    data["osds"][0]["free"] = 200
    vitastor_monitor.poll_cluster_once(cluster)
    recovered = [item for item in alerts if "Dung lượng" in item[2] and item[1] == "HEALTHY"]
    assert len(recovered) == 2


def test_etcd_latency_alert_is_transition_scoped(cluster, monkeypatch):
    alerts = []
    data = _data()
    data["etcd_status"] = [{"Endpoint": "e1", "Status": {"header": {"member_id": 1}, "leader": 1}}]
    data["etcd_health"] = [{"endpoint": "e1", "health": True, "took": "600ms"}]
    monkeypatch.setattr(vitastor_monitor, "query_dashboard", lambda *_: data)
    monkeypatch.setattr(vitastor_monitor, "send_vitastor_alert", lambda *args: alerts.append(args))
    vitastor_monitor.poll_cluster_once(cluster)
    vitastor_monitor.poll_cluster_once(cluster)
    assert len([item for item in alerts if item[2].startswith("Etcd")]) == 1
    data["etcd_health"][0]["took"] = "2ms"
    vitastor_monitor.poll_cluster_once(cluster)
    etcd_alerts = [item for item in alerts if item[2].startswith("Etcd")]
    assert [item[1] for item in etcd_alerts] == ["CRITICAL", "HEALTHY"]


def test_etcd_query_failure_is_alerted_and_recovers(cluster, monkeypatch):
    alerts = []
    data = _data()
    data["etcd_status"] = [{"Endpoint": "e1", "Status": {"header": {"member_id": 1}, "leader": 1}}]
    data["etcd_health"] = [{"endpoint": "e1", "health": True, "took": "2ms"}]
    data["errors"] = {"etcd_health": "permission denied"}
    monkeypatch.setattr(vitastor_monitor, "query_dashboard", lambda *_: data)
    monkeypatch.setattr(vitastor_monitor, "send_vitastor_alert", lambda *args: alerts.append(args))

    vitastor_monitor.poll_cluster_once(cluster)
    vitastor_monitor.poll_cluster_once(cluster)
    data["errors"] = {}
    vitastor_monitor.poll_cluster_once(cluster)

    etcd_alerts = [item for item in alerts if "Etcd" in item[2]]
    assert [item[1] for item in etcd_alerts] == ["UNREACHABLE", "HEALTHY"]


def test_slow_osd_needs_consecutive_relative_outlier_scans(cluster, monkeypatch):
    alerts = []
    data = _data()
    data["osds"] = [
        {"type": "osd", "name": 1, "parent": "a", "up": True, "op_stats": {"read": {"latency_ms": 2}}},
        {"type": "osd", "name": 2, "parent": "b", "up": True, "op_stats": {"read": {"latency_ms": 3}}},
        {"type": "osd", "name": 3, "parent": "c", "up": True, "op_stats": {"read": {"latency_ms": 30}}},
    ]
    monkeypatch.setattr(vitastor_monitor, "query_dashboard", lambda *_: data)
    monkeypatch.setattr(vitastor_monitor, "send_vitastor_alert", lambda *args: alerts.append(args))
    vitastor_monitor.poll_cluster_once(cluster); vitastor_monitor.poll_cluster_once(cluster)
    assert not any("Slow OSD" in item[2] for item in alerts)
    vitastor_monitor.poll_cluster_once(cluster)
    assert len([item for item in alerts if "Slow OSD 3" in item[2]]) == 1
    vitastor_monitor.poll_cluster_once(cluster)
    assert len([item for item in alerts if "Slow OSD 3" in item[2]]) == 1


def test_degraded_and_recovery_bandwidth_have_independent_alerts(cluster, monkeypatch):
    alerts = []
    data = _data()
    data["status"].update({"degraded_data": 4096, "recovery_stats": {"recovery": {"bps": 600_000_000}}})
    monkeypatch.setattr(vitastor_monitor, "query_dashboard", lambda *_: data)
    monkeypatch.setattr(vitastor_monitor, "send_vitastor_alert", lambda *args: alerts.append(args))
    vitastor_monitor.poll_cluster_once(cluster); vitastor_monitor.poll_cluster_once(cluster)
    assert len([item for item in alerts if "Data integrity" in item[2]]) == 1
    assert len([item for item in alerts if "Recovery/Rebalance đang" in item[2]]) == 1
    data["status"].update({"degraded_data": 0, "recovery_stats": {}})
    vitastor_monitor.poll_cluster_once(cluster)
    assert any("CLEAN" in item[2] for item in alerts)
    assert any("đã giảm" in item[2] for item in alerts)


def test_network_rtt_alert_is_deduplicated_and_recovers(cluster, monkeypatch):
    alerts = []
    data = _data(); data["osds"] = [
        {"type": "osd", "name": 1, "parent": "node-a", "up": True},
        {"type": "osd", "name": 2, "parent": "node-b", "up": True},
    ]
    current_rtt = {"value": 25}
    monkeypatch.setattr(vitastor_monitor, "query_dashboard", lambda *_: data)
    monkeypatch.setattr(vitastor_monitor, "query_node_network", lambda source, targets, *_: {"source": source, "interfaces": [{"name": "eth0", "state": "up", "mtu": 9000, "speed_mbps": 10000, "rx_errors": 0, "rx_dropped": 0, "tx_errors": 0, "tx_dropped": 0}], "probes": [{"target": target, "reachable": True, "rtt_ms": current_rtt["value"], "jumbo_9000": True} for target in targets]})
    monkeypatch.setattr(vitastor_monitor, "send_vitastor_alert", lambda *args: alerts.append(args))
    vitastor_monitor.poll_cluster_once(cluster); vitastor_monitor.poll_cluster_once(cluster)
    network_alerts = [item for item in alerts if item[2].startswith("Network")]
    assert network_alerts and all(item[1] == "CRITICAL" for item in network_alerts)
    before = len(network_alerts)
    current_rtt["value"] = 1
    vitastor_monitor.poll_cluster_once(cluster)
    network_alerts = [item for item in alerts if item[2].startswith("Network")]
    assert len(network_alerts) == before * 2
    assert all(item[1] == "HEALTHY" for item in network_alerts[before:])


def test_dynamic_loop_discovers_active_clusters(cluster, monkeypatch):
    seen = []
    monkeypatch.setattr(vitastor_monitor, "poll_cluster_once", lambda row: seen.append(row.name))

    vitastor_monitor.run_all_clusters_loop(max_iterations=1)

    assert seen == ["vita-prod"]
