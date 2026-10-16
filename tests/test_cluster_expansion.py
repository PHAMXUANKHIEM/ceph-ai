"""Thêm node vào cụm đang chạy (worker/executor/cluster_expansion.py) run through cluster_deploy.run()."""

import copy
import json

import pytest

from worker.executor import cluster_deploy, cluster_expansion

NEW = [{"ip": "10.0.0.9", "roles": ["osd"], "osd_disks": ["/dev/vdb", "/dev/vdc"]},
       {"ip": "10.0.0.10", "roles": [], "osd_disks": []}]


class Fake:
    def __init__(self, *, existing=("10.0.0.1",), osd_polls=1):
        self.existing, self.osd_polls = set(existing), osd_polls
        self.phases: list[tuple[str, list[str]]] = []
        self.commands: list[tuple[str, str]] = []

    def execute(self, host, command, **kwargs):
        self.commands.append((host, command))
        if "ceph orch host ls" in command:
            return json.dumps([{"hostname": f"h{ip}", "addr": ip} for ip in self.existing])
        if "ceph osd tree" in command:
            up = self.osd_polls <= 0
            self.osd_polls -= 1
            return json.dumps({"nodes": [
                {"type": "host", "name": "node9.lab", "children": [7, 8]},
                {"type": "osd", "id": 7, "status": "up" if up else "down", "reweight": 1},
                {"type": "osd", "id": 8, "status": "up" if up else "down", "reweight": 1},
            ]})
        raise AssertionError(f"unexpected {command}")

    def phase(self, name):
        def run(nodes, params, on_host_update):
            self.phases.append((name, [node["ip"] for node in nodes]))
            if name == "ssh_check":
                params["_node_hostnames"] = {"10.0.0.9": "node9.lab", "10.0.0.10": "node10.lab"}
            on_host_update([{"host": node["ip"], "status": "done"} for node in nodes])
        return run


@pytest.fixture
def fake(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(cluster_expansion, "execute_command", fake.execute)
    for name, attr in (("ssh_check", "_phase_ssh_check"), ("dependencies", "_phase_ceph_deploy_dependencies_cephadm"),
                       ("host_add", "_phase_cephadm_orch_host_add"), ("osd", "_phase_cephadm_orch_apply_osd")):
        monkeypatch.setattr(cluster_deploy, attr, fake.phase(name))
    monkeypatch.setattr(cluster_expansion, "sleep", lambda seconds: None)
    monkeypatch.setattr(cluster_expansion, "apply_config", lambda params: fake.phases.append(("config", [])))
    return fake


def _run(**params):
    calls = []
    body = {"nodes": copy.deepcopy(NEW), "_admin_mon": "10.0.0.1", **params}
    ok = cluster_deploy.run("a1", "add_cluster_nodes", body, "i1", lambda pk, progress: calls.append(copy.deepcopy(progress)))
    return ok, calls[-1]


def test_new_hosts_are_added_through_an_existing_mon_and_their_osds_awaited(fake):
    ok, progress = _run()

    assert ok, progress
    assert fake.phases == [
        ("ssh_check", ["10.0.0.9", "10.0.0.10"]),
        ("dependencies", ["10.0.0.9", "10.0.0.10"]),
        ("host_add", ["10.0.0.1", "10.0.0.9", "10.0.0.10"]),   # the existing MON first: cephadm runs there
        ("osd", ["10.0.0.1", "10.0.0.9"]),                     # only the host with disks
        ("config", []),
    ]
    assert progress[-1]["hosts"] == [{"host": "10.0.0.9", "status": "done", "message": "2/2 OSD up/in"}]


def test_a_host_already_in_the_cluster_is_refused_before_anything_runs(fake):
    fake.existing.add("10.0.0.9")

    ok, progress = _run()

    assert ok is False and "10.0.0.9 đã có trong cụm" in progress[0]["message"]
    assert fake.phases == []


def test_osds_that_never_come_up_fail_the_action(fake, monkeypatch):
    fake.osd_polls = 10**6
    ticks = iter(range(0, 10**7, 300))
    monkeypatch.setattr(cluster_expansion, "clock", lambda: next(ticks))

    ok, progress = _run()

    assert ok is False and "chưa up/in" in progress[-1]["message"]


def test_the_new_osd_hosts_join_the_clusters_osd_node_list(monkeypatch):
    from types import SimpleNamespace

    row = SimpleNamespace(is_default=False, ceph_osd_nodes="10.0.0.1,10.0.0.2")

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, model, key):
            return row

        def commit(self):
            pass

    monkeypatch.setattr(cluster_expansion.db, "SessionLocal", Session)

    cluster_expansion.apply_config({"_cluster_id": "c2", "nodes": NEW})

    assert row.ceph_osd_nodes == "10.0.0.1,10.0.0.2,10.0.0.9"
