from types import SimpleNamespace

from watcher import host_metrics
from watcher.node_metrics import parse_node_metrics


def test_telemetry_hosts_includes_all_configured_ceph_roles(monkeypatch):
    monkeypatch.setattr(
        host_metrics,
        "configured_nodes",
        lambda _cluster: [
            {"host": "mon-1", "roles": ["MON"]},
            {"host": "mgr-1", "roles": ["MGR"]},
            {"host": "osd-1", "roles": ["OSD"]},
            {"host": "rgw-1", "roles": ["RGW"]},
        ],
    )

    assert host_metrics._telemetry_hosts(SimpleNamespace()) == [
        "mon-1", "mgr-1", "osd-1", "rgw-1"
    ]


def test_telemetry_targets_collapse_ip_aliases_by_physical_hostname(monkeypatch):
    nodes = [
        {"host": "10.3.53.1", "roles": ["MON", "MGR"]},
        {"host": "10.3.53.69", "roles": ["MON", "RGW"]},
        {"host": "10.3.54.118", "roles": ["MON", "RGW"]},
        {"host": "10.20.1.39", "roles": ["OSD"]},
        {"host": "10.20.1.153", "roles": ["OSD"]},
        {"host": "10.20.1.195", "roles": ["OSD"]},
    ]
    hostnames = {
        "10.3.53.1": "ceph1",
        "10.20.1.39": "ceph1",
        "10.3.53.69": "ceph2",
        "10.20.1.153": "ceph2",
        "10.3.54.118": "ceph3",
        "10.20.1.195": "ceph3",
    }
    monkeypatch.setattr(host_metrics, "configured_nodes", lambda _cluster: nodes)
    monkeypatch.setattr(host_metrics, "_identity", lambda _cluster_id, host, _cluster: hostnames[host])

    targets = host_metrics._telemetry_targets("cluster-a", SimpleNamespace())

    assert [target["host"] for target in targets] == [
        "10.3.53.1", "10.3.53.69", "10.3.54.118"
    ]
    assert targets[0]["roles"] == ["MGR", "MON", "OSD"]
    assert targets[0]["aliases"] == ["10.3.53.1", "10.20.1.39"]


def test_parse_node_metrics_includes_network_rates_without_counting_loopback():
    raw = """===CPU1===
cpu  100 0 100 800 0 0 0 0
===DISK1===
  8 0 sda 1 0 1 1 1 0 1 1 0 0 0
===NET1===
Inter-| Receive | Transmit
 lo: 100 0 0 0 0 0 0 0 100 0 0 0 0 0 0 0
 eth0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0
===CPU2===
cpu  100 0 100 900 0 0 0 0
===DISK2===
  8 0 sda 1 0 1 1 1 0 1 1 0 0 0
===NET2===
Inter-| Receive | Transmit
 lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0
 eth0: 1600 0 0 0 0 0 0 0 2800 0 0 0 0 0 0 0
===MEM===
MemTotal: 1000 kB
MemAvailable: 500 kB
"""
    result = parse_node_metrics(raw)
    assert result["network_rx_bytes_per_sec"] == 600.0
    assert result["network_tx_bytes_per_sec"] == 800.0
