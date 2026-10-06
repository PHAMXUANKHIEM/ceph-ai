from types import SimpleNamespace

import pytest

from watcher import cluster_snapshot_collector, inventory_queries
from watcher.ceph_client import CephQueryError

CLUSTER = SimpleNamespace(id="c1", is_default=True, ceph_mon_nodes="10.3.53.1,10.3.53.69")


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
