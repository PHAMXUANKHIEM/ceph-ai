"""Thay OSD hỏng giữ ID (worker/executor/osd_replacement.py) run through cluster_deploy.run()."""

import copy
import json
import shlex

import pytest

from worker.executor import cluster_deploy, node_removal, osd_replacement

GiB = 1048576  # kB


class FakeCluster:
    """Hosts ceph1-4 with OSDs 0-3 (one each), pool size 3, cephadm."""

    def __init__(self, *, hosts=4, status="up", used_kb=10 * GiB, destroy_polls=0, auto_create=False):
        self.osds = {i: {"host": f"ceph{i + 1}", "status": status, "reweight": 1} for i in range(hosts)}
        self.used_kb, self.destroy_polls, self.auto_create = used_kb, destroy_polls, auto_create
        self.commands: list[str] = []

    def __call__(self, host, command, **kwargs):
        inner = shlex.split(command)[-1] if command.startswith("cephadm shell") else command
        self.commands.append(inner)
        cmd = inner.replace(" --format json", "")
        if cmd == "ceph health":
            return json.dumps({"status": "HEALTH_WARN"})
        if cmd == "ceph osd tree":
            nodes = [{"type": "host", "name": o["host"], "children": [i]} for i, o in self.osds.items()]
            nodes += [{"type": "osd", "id": i, "status": o["status"], "reweight": o["reweight"]} for i, o in self.osds.items()]
            return json.dumps({"nodes": nodes})
        if cmd.startswith("ceph osd metadata"):
            osd_id = int(cmd.split()[-1])
            return json.dumps({"hostname": self.osds[osd_id]["host"], "devices": "vdb"})
        if cmd == "ceph osd pool ls detail":
            return json.dumps([{"pool_name": "rbd", "size": 3}])
        if cmd == "ceph osd df":
            return json.dumps({"nodes": [{"id": i, "kb": 100 * GiB, "kb_used": self.used_kb,
                                          "kb_avail": 100 * GiB - self.used_kb, "reweight": 1} for i in self.osds]})
        if cmd == "ceph osd dump":
            return json.dumps({"nearfull_ratio": 0.85})
        if cmd.startswith("ceph orch osd rm"):
            self._removing = int(cmd.split()[4])
            if not self.destroy_polls:
                self.osds[self._removing]["status"] = "destroyed"
            return ""
        if cmd.startswith("ceph orch device zap"):
            if self.auto_create:
                self._recreate(next(i for i, o in self.osds.items() if o["status"] == "destroyed"))
            return ""
        if cmd.startswith("ceph orch daemon add osd"):
            self._recreate(next(i for i, o in self.osds.items() if o["status"] == "destroyed"))
            return ""
        raise AssertionError(f"unexpected {cmd}")

    def _recreate(self, osd_id):
        self.osds[osd_id]["status"] = "up"

    def tick(self):
        if self.destroy_polls and hasattr(self, "_removing"):
            self.destroy_polls -= 1
            if not self.destroy_polls:
                self.osds[self._removing]["status"] = "destroyed"


NODES = [{"host": f"10.0.0.{i}", "roles": ["MON", "OSD"]} for i in range(1, 5)]


@pytest.fixture
def env(monkeypatch):
    fake = FakeCluster()
    _patch(monkeypatch, fake)
    return fake


def _patch(monkeypatch, fake):
    monkeypatch.setattr(node_removal, "execute_command", fake)
    monkeypatch.setattr(node_removal, "configured_nodes", lambda cluster=None: NODES)
    monkeypatch.setattr(node_removal, "resolve_ssh_creds", lambda cluster=None: ("root", "/k", "cephadm", ""))
    monkeypatch.setattr(node_removal, "_cluster", lambda params: None)
    monkeypatch.setattr(osd_replacement, "sleep", lambda seconds: fake.tick())


def _run(action_id, **params):
    calls = []
    body = dict(params)
    ok = cluster_deploy.run("a1", action_id, body, "i1", lambda pk, progress: calls.append(copy.deepcopy(progress)))
    return ok, calls[-1], body


def test_a_healthy_osd_is_drained_destroyed_and_its_disk_recorded(env):
    ok, progress, _ = _run("replace_failed_osd", osd_id=2)

    assert ok, progress
    assert "ceph orch osd rm 2 --replace" in env.commands
    preflight = next(step for step in progress if step["step"] == "osd_preflight")
    assert (preflight["hosts"][0]["osd_host"], preflight["hosts"][0]["osd_device"]) == ("ceph3", "/dev/vdb")
    assert "destroyed, giữ ID" in progress[-1]["hosts"][0]["message"]


def test_the_new_disk_is_zapped_and_the_osd_recreated_with_its_id(env):
    env.osds[2]["status"] = "destroyed"

    ok, progress, _ = _run("finish_replace_osd", osd_id=2, _hostname="ceph3", _device="/dev/vdb")

    assert ok, progress
    assert "ceph orch device zap ceph3 /dev/vdb --force" in env.commands
    assert "ceph orch daemon add osd ceph3:/dev/vdb" in env.commands
    assert env.osds[2]["status"] == "up"


def test_an_osd_cephadm_recreates_by_itself_is_not_added_twice(monkeypatch):
    fake = FakeCluster(auto_create=True)
    fake.osds[1]["status"] = "destroyed"
    _patch(monkeypatch, fake)

    ok, progress, _ = _run("finish_replace_osd", osd_id=1, _hostname="ceph2", _device="/dev/vdb")

    assert ok, progress
    assert not any(command.startswith("ceph orch daemon add osd") for command in fake.commands)


@pytest.mark.parametrize(("fake_kwargs", "osd_id", "reason"), [
    ({"hosts": 3}, 0, "dữ liệu không có chỗ để dời"),
    ({"used_kb": 70 * GiB}, 0, "Không đủ chỗ"),
    ({}, 9, "không có trong"),
])
def test_unsafe_replacements_stop_before_removing_anything(monkeypatch, fake_kwargs, osd_id, reason):
    fake = FakeCluster(**fake_kwargs)
    _patch(monkeypatch, fake)

    ok, progress, _ = _run("replace_failed_osd", osd_id=osd_id)

    assert ok is False and reason in progress[0]["message"]
    assert not any(command.startswith("ceph orch osd rm") for command in fake.commands)


def test_a_down_osd_is_replaced_even_without_spare_hosts(monkeypatch):
    """A failed disk holds nothing the cluster can still read; waiting for spare hosts helps no one."""
    fake = FakeCluster(hosts=3, status="down")
    _patch(monkeypatch, fake)

    ok, progress, _ = _run("replace_failed_osd", osd_id=0)

    assert ok, progress
    assert "ceph orch osd rm 0 --replace" in fake.commands


def test_finishing_refuses_an_osd_that_is_not_destroyed_yet(env):
    ok, progress, _ = _run("finish_replace_osd", osd_id=2, _hostname="ceph3", _device="/dev/vdb")

    assert ok is False and "chưa ở trạng thái destroyed" in progress[0]["message"]
    assert not any("zap" in command for command in env.commands)


def test_a_slow_drain_is_left_running(monkeypatch):
    fake = FakeCluster(destroy_polls=10**6)
    _patch(monkeypatch, fake)
    ticks = iter(range(0, 10**6, 600))
    monkeypatch.setattr(osd_replacement, "clock", lambda: next(ticks))

    ok, progress, body = _run("replace_failed_osd", osd_id=3)

    assert ok and body["_removal_pending"] is True
    assert "vẫn đang dời dữ liệu" in progress[-1]["hosts"][0]["message"]
