import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "live_readonly_acceptance.py"
spec = importlib.util.spec_from_file_location("live_readonly_acceptance", MODULE)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module  # dataclasses resolve their module by name
spec.loader.exec_module(module)


@pytest.mark.parametrize("command", [
    "sudo docker exec ceph-mon-a ceph health detail --format json",
    "timeout --signal=TERM --kill-after=5s 30s flock -w 10 /run/lock cephadm shell -- ceph osd pool ls detail --format json",
    "bash -lc 'set -o pipefail\nbatch_dir=$(mktemp -d)\ntrap '\\''rm -rf \"$batch_dir\"'\\'' EXIT\n"
    "rbd status --pool rbd --image vol-1 --format json 2>/dev/null'",
    "podman exec ceph ceph osd crush rule dump --format json",
])
def test_transport_allows_read_only_calls_including_batch_scaffolding(command):
    module.assert_read_only_transport(command)


@pytest.mark.parametrize("command", [
    "sudo docker exec ceph-mon-a ceph osd pool set rbd size 3",
    "cephadm shell -- ceph config set global mon_allow_pool_delete true",
    "podman exec ceph ceph osd out 3",
    "docker exec ceph ceph osd crush rule rm replicated_ssd",
    "podman exec ceph rbd snap create rbd/vol@s1",
    "podman exec ceph ceph orch daemon restart osd.3",
    "podman exec ceph radosgw-admin bucket rm --bucket b",
    "bash -lc 'rbd status --pool rbd --image a; rbd rm --pool rbd b'",
    "systemctl restart ceph.target",
])
def test_transport_refuses_mutations_and_unknown_commands(command):
    with pytest.raises(module.RefusedCommand):
        module.assert_read_only_transport(command)


def test_probe_allowlist_rejects_anything_else():
    module.assert_read_only_probe("ceph osd tree")
    module.assert_read_only_probe("rbd du --pool rbd.ssd")
    with pytest.raises(module.RefusedCommand):
        module.assert_read_only_probe("ceph osd pool rm rbd rbd --yes-i-really-really-mean-it")
    with pytest.raises(module.RefusedCommand):
        module.assert_read_only_probe("rbd du --pool rbd; rbd rm x")


def test_safety_window_handles_midnight_wrap():
    at = lambda hour: datetime(2026, 9, 25, hour, 30, tzinfo=timezone.utc)  # noqa: E731
    assert module.within_window(None, at(12))
    assert module.within_window("01:00-05:00", at(3))
    assert not module.within_window("01:00-05:00", at(12))
    assert module.within_window("22:00-02:00", at(23))
    assert module.within_window("22:00-02:00", at(1))
    assert not module.within_window("22:00-02:00", at(12))


def test_p95_and_helpers():
    assert module.p95([]) is None
    assert module.p95([float(value) for value in range(1, 101)]) == 95.0
    pools = [
        {"pool_name": "rbd", "application_metadata": {"rbd": {}}},
        {"pool_name": "cephfs_data", "application_metadata": {"cephfs": {}}},
    ]
    assert module.rbd_pools(pools) == ["rbd"]
    assert module.has_rgw({"servicemap": {"services": {"rgw": {}}}})
    assert not module.has_rgw({"servicemap": {"services": {}}})
    versions = {"overall": {"ceph version 18.2.4 (abc) reef (stable)": 9, "ceph version 19.2.1 (d) squid": 1}}
    assert module.ceph_release(versions) == ["18", "19"]


class FakeClient:
    def __init__(self, *, swallow_missing_pool=False, release="18.2.4"):
        self.swallow_missing_pool = swallow_missing_pool
        self.release = release
        self.commands = []

    def run_ceph_json_command_with(self, *args):
        command = args[-1]
        self.commands.append(command)
        payloads = {
            "ceph health detail": {"status": "HEALTH_OK", "checks": {}},
            "ceph status": {"servicemap": {"services": {}}},
            "ceph versions": {"overall": {f"ceph version {self.release} (x) reef (stable)": 3}},
            "ceph osd pool ls detail": [{"pool_name": "rbd", "application_metadata": {"rbd": {}}}],
        }
        return "mon-1", payloads.get(command, {})

    def query_rbd_inventory_with(self, pool, *connection):
        if self.swallow_missing_pool:
            return []
        raise RuntimeError(f"rbd: error opening pool '{pool}': (2) No such file or directory")


CONNECTION = (["mon-1"], "ceph", "root", "/key", "docker")


def test_probe_cluster_covers_the_read_only_matrix():
    client = FakeClient()
    result = module.probe_cluster("lab-a", CONNECTION, client)
    names = [probe["probe"] for probe in result["probes"]]
    assert names[:8] == ["health", "status", "versions", "inventory", "pools", "capacity", "pg", "crush"]
    assert "rbd:rbd" in names
    rgw = next(probe for probe in result["probes"] if probe["probe"] == "rgw")
    assert rgw["status"] == "not_applicable"
    error_probe = next(probe for probe in result["probes"] if probe["probe"] == "error_not_zero")
    assert error_probe["status"] == "ok" and error_probe["observed"] == "error"
    assert result["ceph_releases"] == ["18"]
    assert all(module.READ_ONLY_COMMANDS[0].match(c) or c.startswith("rbd du") for c in client.commands)


def test_error_swallowed_as_empty_list_fails_the_wave():
    result = module.probe_cluster("lab-a", CONNECTION, FakeClient(swallow_missing_pool=True))
    report = {"clusters": [result, module.probe_cluster("lab-b", CONNECTION, FakeClient(release="19.2.1"))]}
    assert module.summarise(report, min_clusters=2, min_releases=2) == ("failed", 1)


def test_matrix_requirement_is_reported_separately():
    one = {"clusters": [module.probe_cluster("lab-a", CONNECTION, FakeClient())]}
    assert module.summarise(one, min_clusters=2, min_releases=2) == ("incomplete_matrix", 2)
    two = {"clusters": [
        module.probe_cluster("lab-a", CONNECTION, FakeClient()),
        module.probe_cluster("lab-b", CONNECTION, FakeClient(release="19.2.1")),
    ]}
    assert module.summarise(two, min_clusters=2, min_releases=2) == ("passed", 0)


def test_transport_meter_counts_times_and_guards():
    meter = module.TransportMeter()
    sent = []

    def original(host, command, *args, **kwargs):
        sent.append(command)
        return "{}"

    guarded = meter.wrap(original)
    guarded("mon-1", "docker exec ceph ceph health detail --format json", "root", "/key")
    with pytest.raises(module.RefusedCommand):
        guarded("mon-1", "docker exec ceph ceph osd pool delete rbd", "root", "/key")
    assert sent == ["docker exec ceph ceph health detail --format json"]
    assert len(meter.calls) == 1
    assert meter.calls[0]["commands"] == ["ceph health detail --format json"]
