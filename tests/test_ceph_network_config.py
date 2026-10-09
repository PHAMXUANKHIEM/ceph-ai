from types import SimpleNamespace

import pytest

from watcher import cluster_snapshot_collector, inventory_queries
from watcher.ceph_client import CephQueryError

CLUSTER = SimpleNamespace(id="c1", is_default=True, ceph_mon_nodes="10.3.53.1,10.3.53.69")


@pytest.fixture(autouse=True)
def _fresh_reuse_cache(monkeypatch):
    monkeypatch.setattr(cluster_snapshot_collector, "_SLOW_FACTS", {})
    monkeypatch.setattr(cluster_snapshot_collector, "_map_epochs", lambda cluster: None)


@pytest.mark.parametrize("raw, expected", [
    ("10.20.1.0/24\n", ["10.20.1.0/24"]),
    ("", []),
    ("10.20.1.0/24, 10.30.0.0/16", ["10.20.1.0/24", "10.30.0.0/16"]),
    ("10.20.1.5/24", ["10.20.1.0/24"]),
    ("fd00::/64", ["fd00::/64"]),
    ("garbage; rm -rf /", []),
    (None, []),
])
def test_network_values_are_strict_cidrs(raw, expected):
    assert inventory_queries.parse_network_list(raw) == expected


def test_collects_public_and_cluster_network_with_read_only_commands(monkeypatch):
    commands = []
    outputs = {
        "ceph config get mon public_network": "10.20.1.0/24\n",
        "ceph config get osd cluster_network": "\n",
    }
    monkeypatch.setattr(inventory_queries, "resolve_ssh_creds", lambda cluster: ("root", "/key", "cephadm", ""))

    def run(nodes, container, user, key, mode, inner):
        commands.append(inner)
        return nodes[0], outputs[inner]

    monkeypatch.setattr(inventory_queries.ceph_client, "run_ceph_text_command_with", run)

    assert inventory_queries.collect_network_config(CLUSTER) == {"public": ["10.20.1.0/24"], "cluster": []}
    assert commands == list(outputs)


def test_a_failed_network_query_keeps_the_nodes_section(monkeypatch):
    def broken(cluster):
        raise CephQueryError("All MON nodes failed")

    monkeypatch.setattr(inventory_queries, "collect_network_config", broken)

    assert cluster_snapshot_collector._network_config(CLUSTER) is None


def test_daemon_addresses_keep_only_names_addresses_and_state():
    mon_dump = {"epoch": 3, "auth_service_cipher": {"name": "aes256k"}, "quorum": [0], "mons": [
        {"rank": 0, "name": "ceph1", "public_addrs": {"addrvec": [
            {"type": "v2", "addr": "10.20.1.39:3300", "nonce": 0}, {"type": "v1", "addr": "10.20.1.39:6789", "nonce": 0}]}},
        {"rank": 1, "name": "ceph2", "public_addrs": {"addrvec": [{"type": "v2", "addr": "$(reboot):1", "nonce": 0}]}},
    ]}
    osd_dump = {"pools": [{"pool_name": "rbd"}], "blocklist": {"10.0.0.9:0/1": "x"}, "osds": [
        {"osd": 3, "up": 0, "in": 1, "uuid": "u",
         "public_addrs": {"addrvec": [{"type": "v2", "addr": "[fd00::3]:6800", "nonce": 7}]},
         "cluster_addrs": {"addrvec": [{"type": "v2", "addr": "10.20.1.39:6802", "nonce": 7}]}},
        {"osd": "bad"},
    ]}

    result = inventory_queries.parse_daemon_addresses(mon_dump, osd_dump)

    assert result == {
        "mons": [
            {"name": "ceph1", "rank": 0, "in_quorum": True,
             "public": [{"type": "v2", "addr": "10.20.1.39:3300"}, {"type": "v1", "addr": "10.20.1.39:6789"}]},
            {"name": "ceph2", "rank": 1, "in_quorum": False, "public": []},
        ],
        "osds": [{"id": 3, "up": False, "in": True, "public": [{"type": "v2", "addr": "[fd00::3]:6800"}],
                  "cluster": [{"type": "v2", "addr": "10.20.1.39:6802"}]}],
    }


def test_both_dumps_come_from_one_batch_call(monkeypatch):
    calls = []
    monkeypatch.setattr(inventory_queries, "resolve_ssh_creds", lambda cluster: ("root", "/key", "cephadm", ""))

    def batch(nodes, container, user, key, mode, inner_commands):
        calls.append(inner_commands)
        return nodes[0], [{"mons": [], "quorum": []}, {"osds": []}]

    monkeypatch.setattr(inventory_queries.ceph_client, "run_ceph_json_batch_command_with", batch)

    assert inventory_queries.collect_daemon_addresses(CLUSTER) == {"mons": [], "osds": []}
    assert calls == [["ceph mon dump --format json", "ceph osd dump --format json"]]


def test_a_failed_dump_keeps_the_nodes_section(monkeypatch):
    monkeypatch.setattr(inventory_queries, "resolve_ssh_creds", lambda cluster: ("root", "/key", "cephadm", ""))
    monkeypatch.setattr(inventory_queries.ceph_client, "run_ceph_json_batch_command_with",
                        lambda *args: ("h", [{"mons": []}, None]))

    with pytest.raises(CephQueryError):
        inventory_queries.collect_daemon_addresses(CLUSTER)
    assert cluster_snapshot_collector._daemon_addresses(CLUSTER) is None


# --- reuse between inventory polls (09/10/2026) ------------------------------------------------

def _counting(monkeypatch, name, values):
    calls = []

    def loader(cluster):
        calls.append(cluster.id)
        value = values[min(len(calls), len(values)) - 1]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(inventory_queries, name, loader)
    return calls


def test_network_config_is_asked_once_per_ttl(monkeypatch):
    calls = _counting(monkeypatch, "collect_network_config", [{"public": ["10.20.1.0/24"], "cluster": []}])
    clock = [1000.0]
    monkeypatch.setattr(cluster_snapshot_collector, "monotonic", lambda: clock[0])

    for _ in range(5):
        assert cluster_snapshot_collector._network_config(CLUSTER)["public"] == ["10.20.1.0/24"]
    clock[0] += cluster_snapshot_collector.NETWORK_CONFIG_TTL_SECONDS
    cluster_snapshot_collector._network_config(CLUSTER)

    assert len(calls) == 2


def test_daemon_addresses_are_refetched_when_the_osdmap_or_monmap_epoch_moves(monkeypatch):
    first, second = {"mons": [], "osds": [{"id": 0, "up": True}]}, {"mons": [], "osds": [{"id": 0, "up": False}]}
    calls = _counting(monkeypatch, "collect_daemon_addresses", [first, second, second])
    epochs = [(100, 7)]
    monkeypatch.setattr(cluster_snapshot_collector, "_map_epochs", lambda cluster: epochs[0])

    assert cluster_snapshot_collector._daemon_addresses(CLUSTER) is first
    assert cluster_snapshot_collector._daemon_addresses(CLUSTER) is first  # same maps: no osd dump
    epochs[0] = (101, 7)  # osd.0 went down
    assert cluster_snapshot_collector._daemon_addresses(CLUSTER) is second
    epochs[0] = (101, 8)  # a MON moved
    cluster_snapshot_collector._daemon_addresses(CLUSTER)

    assert len(calls) == 3


def test_a_failed_refresh_keeps_the_last_good_value(monkeypatch):
    good = {"public": ["10.20.1.0/24"], "cluster": []}
    calls = _counting(monkeypatch, "collect_network_config", [good, CephQueryError("All MON nodes failed")])
    clock = [0.0]
    monkeypatch.setattr(cluster_snapshot_collector, "monotonic", lambda: clock[0])

    cluster_snapshot_collector._network_config(CLUSTER)
    clock[0] += cluster_snapshot_collector.NETWORK_CONFIG_TTL_SECONDS + 1

    assert cluster_snapshot_collector._network_config(CLUSTER) is good and len(calls) == 2


def test_map_epochs_come_from_the_published_status(monkeypatch):
    monkeypatch.undo()
    snapshot = {"status": {"osdmap": {"epoch": 24295}, "monmap": {"epoch": 129}}}
    monkeypatch.setattr(cluster_snapshot_collector, "read_section_snapshot", lambda cluster_id, section: snapshot)

    assert cluster_snapshot_collector._map_epochs(CLUSTER) == (24295, 129)
    snapshot["status"] = {}
    assert cluster_snapshot_collector._map_epochs(CLUSTER) is None
