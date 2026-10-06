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
