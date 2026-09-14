import json

from shared.models import Cluster
from watcher import capacity_evidence


DF = {
    "stats": {
        "total_bytes": 1000, "total_used_bytes": 820,
        "total_avail_bytes": 180, "total_used_raw_ratio": 0.82,
    },
    "pools": [
        {"name": "images", "stats": {"bytes_used": 100, "max_avail": 50, "percent_used": 0.67}},
        {"name": "vms", "stats": {"bytes_used": 300, "max_avail": 20, "percent_used": 0.94}},
    ],
}
OSD_DF = {"nodes": [
    {"id": 1, "utilization": 91.2, "kb": 100, "kb_used": 91, "kb_avail": 9},
    {"id": 2, "utilization": 72.5, "kb": 100, "kb_used": 72, "kb_avail": 28},
]}
OSD_TREE = {"nodes": [
    {"id": -3, "name": "ceph-node-a", "type": "host", "children": [1]},
    {"id": -5, "name": "ceph-node-b", "type": "host", "children": [2]},
]}


def test_collect_capacity_evidence_is_structured_and_orders_pressure():
    def query(_cluster, command):
        if command == "ceph osd tree":
            return OSD_TREE
        return OSD_DF if command == "ceph osd df" else DF

    raw = capacity_evidence.collect_capacity_evidence(
        "OSD_NEARFULL", {"severity": "HEALTH_WARN"}, query=query
    )
    evidence = json.loads(raw)
    assert evidence["source"] == "ceph_capacity_snapshot"
    assert evidence["cluster"]["used_percent"] == 82.0
    assert evidence["pools"][0]["pool"] == "vms"
    assert evidence["pools"][0]["used_percent"] == 94.0
    assert evidence["osds"][0]["osd_id"] == 1
    assert evidence["osds"][0]["host"] == "ceph-node-a"
    assert "key" not in raw.lower()


def test_format_capacity_alert_context_names_pools_and_osd_hosts():
    evidence = {
        "source": "ceph_capacity_snapshot",
        "cluster": {"used_percent": 82.0},
        "pools": [{"pool": "vms", "used_percent": 94.0}],
        "osds": [{"osd_id": 1, "host": "ceph-node-a", "used_percent": 91.2}],
    }
    text = capacity_evidence.format_capacity_alert_context(json.dumps(evidence))
    assert "Pool áp lực: vms 94.00%" in text
    assert "OSD áp lực: osd.1 trên ceph-node-a 91.20%" in text


def test_non_capacity_code_does_not_query():
    called = []
    assert capacity_evidence.collect_capacity_evidence(
        "OSD_DOWN", {}, query=lambda *_: called.append(1)
    ) is None
    assert called == []


def test_partial_query_failure_keeps_auditable_snapshot():
    def query(_cluster, command):
        if command == "ceph df detail":
            raise RuntimeError("unavailable")
        return OSD_DF

    evidence = json.loads(capacity_evidence.collect_capacity_evidence(
        "POOL_FULL", {"severity": "HEALTH_ERR"}, query=query
    ))
    assert evidence["df_available"] is False
    assert evidence["osds"][0]["used_percent"] == 91.2


def test_cluster_scoped_query_uses_observed_cluster_connection(monkeypatch):
    cluster = Cluster(
        name="secondary", ceph_mon_nodes="10.0.0.2,10.0.0.3",
        ceph_container_name="mon", ssh_user="ceph", ssh_key_path="/ssh",
        ceph_exec_mode="docker", is_default=False,
    )
    calls = []
    monkeypatch.setattr(
        capacity_evidence.ceph_client, "run_ceph_json_command_with",
        lambda *args, **kwargs: (calls.append(args) or ("10.0.0.2", DF)),
    )
    capacity_evidence.collect_capacity_evidence("POOL_NEAR_FULL", {}, cluster=cluster)
    assert len(calls) == 3
    assert calls[0][:5] == (["10.0.0.2", "10.0.0.3"], "mon", "ceph", "/ssh", "docker")
