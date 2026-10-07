"""Gỡ node khỏi cụm (worker/executor/node_removal.py) run through cluster_deploy.run()."""

import copy
import json
import shlex

import pytest

from worker.executor import cluster_deploy, node_removal

GiB = 1048576  # kB


class FakeCephadm:
    """ceph1-3 run MON+MGR, ceph4-7 hold 2 OSDs each (ceph4-6 without the extra host); pool size 3."""

    def __init__(self, *, extra_osd_host=True, used_kb=10 * GiB, drain_polls=1, mons=3):
        self.hosts = {f"10.0.0.{i}": f"ceph{i}" for i in range(1, 8 if extra_osd_host else 7)}
        self.osd_hosts = {name: [2 * i, 2 * i + 1] for i, name in enumerate(
            [h for h in self.hosts.values() if h not in ("ceph1", "ceph2", "ceph3")])}
        self.used_kb, self.drain_polls, self.mons = used_kb, drain_polls, mons
        self.drained: set[str] = set()
        self.removed: set[str] = set()
        self.commands: list[str] = []

    def __call__(self, host, command, **kwargs):
        inner = shlex.split(command)[-1] if command.startswith("cephadm shell") else command
        self.commands.append(inner)
        cmd = inner.replace(" --format json", "")
        if cmd == "ceph orch host ls":
            return json.dumps([{"hostname": n, "addr": ip} for ip, n in self.hosts.items() if n not in self.removed])
        if cmd == "ceph health":
            return json.dumps({"status": "HEALTH_OK"})
        if cmd == "ceph mon dump":
            return json.dumps({"mons": [{"name": f"ceph{i}"} for i in range(1, self.mons + 1)]})
        if cmd == "ceph mgr dump":
            return json.dumps({"active_name": "ceph1.abc", "standbys": [{"name": "ceph2.def"}]})
        if cmd == "ceph osd tree":
            return json.dumps({"nodes": [{"type": "host", "name": n, "children": ids} for n, ids in self.osd_hosts.items()]})
        if cmd == "ceph osd pool ls detail":
            return json.dumps([{"pool_name": "rbd", "size": 3}])
        if cmd == "ceph osd df":
            return json.dumps({"nodes": [{"id": i, "kb": 100 * GiB, "kb_used": self.used_kb, "kb_avail": 100 * GiB - self.used_kb,
                                          "reweight": 1} for ids in self.osd_hosts.values() for i in ids]})
        if cmd == "ceph osd dump":
            return json.dumps({"nearfull_ratio": 0.85})
        if cmd.startswith("ceph orch host drain"):
            self.drained.add(cmd.split()[4])
            return ""
        if cmd == "ceph orch osd rm status":
            if self.drain_polls > 0:
                self.drain_polls -= 1
                return json.dumps([{"osd_id": i} for n in self.drained for i in self.osd_hosts.get(n, [])])
            return json.dumps([])
        if cmd.startswith("ceph orch ps"):
            return json.dumps([{"daemon": "osd"}] if self.drain_polls > 0 else [])
        if cmd.startswith("ceph orch host rm"):
            self.removed.add(cmd.split()[4])
            return ""
        raise AssertionError(f"unexpected {cmd}")


NODES = [{"host": f"10.0.0.{i}", "roles": ["MON", "MGR"] if i <= 3 else ["OSD"]} for i in range(1, 8)]


@pytest.fixture
def env(monkeypatch):
    fake = FakeCephadm()
    written = {}
    monkeypatch.setattr(node_removal, "execute_command", fake)
    monkeypatch.setattr(node_removal, "configured_nodes", lambda cluster=None: NODES)
    monkeypatch.setattr(node_removal, "resolve_ssh_creds", lambda cluster=None: ("root", "/k", "cephadm", ""))
    monkeypatch.setattr(node_removal, "_cluster", lambda params: None)
    monkeypatch.setattr(node_removal, "config_fingerprint", lambda cluster: "fp")
    monkeypatch.setattr(node_removal, "sleep", lambda seconds: None)
    monkeypatch.setattr(node_removal.env_config, "read_env_values", lambda names: {
        "CEPH_MON_NODES": "10.0.0.1,10.0.0.2,10.0.0.3", "CEPH_MGR_NODES": "10.0.0.1",
        "CEPH_OSD_NODES": "10.0.0.4,10.0.0.5,10.0.0.6", "CEPH_RGW_NODES": ""})
    monkeypatch.setattr(node_removal.env_config, "update_env_file_batch", written.update)
    monkeypatch.setattr("shared.clusters.sync_default_cluster_from_env", lambda session: None)
    monkeypatch.setattr(node_removal.db, "SessionLocal", lambda: _NullSession())
    return fake, written


class _NullSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run(action_id, targets, **params):
    calls = []
    body = {"targets": targets, "_node_config_fingerprint": "fp", **params}
    ok = cluster_deploy.run("a1", action_id, body, "i1", lambda pk, progress: calls.append(copy.deepcopy(progress)))
    return ok, calls[-1], body


def test_an_osd_host_is_drained_removed_and_dropped_from_the_config(env):
    fake, written = env

    ok, progress, _ = _run("remove_cluster_nodes", ["10.0.0.6"], zap_devices=True)

    assert ok, progress
    assert "ceph orch host drain ceph6 --zap-osd-devices" in fake.commands
    assert "ceph orch host rm ceph6" in fake.commands
    assert written["CEPH_OSD_NODES"] == "10.0.0.4,10.0.0.5" and written["CEPH_MON_NODES"] == "10.0.0.1,10.0.0.2,10.0.0.3"


@pytest.mark.parametrize(("fake_kwargs", "targets", "reason"), [
    ({}, ["10.0.0.1"], "MON"),
    ({"extra_osd_host": False}, ["10.0.0.5"], "số bản sao"),
    ({"used_kb": 70 * GiB}, ["10.0.0.6"], "Không đủ chỗ"),
    ({}, ["10.0.0.9"], "không có trong"),
])
def test_unsafe_removals_stop_before_any_drain(env, monkeypatch, fake_kwargs, targets, reason):
    fake = FakeCephadm(**fake_kwargs)
    monkeypatch.setattr(node_removal, "execute_command", fake)

    ok, progress, _ = _run("remove_cluster_nodes", targets)

    assert ok is False and reason in progress[0]["message"]
    assert not any("drain" in command or "host rm" in command for command in fake.commands)


def test_a_slow_drain_is_left_running_and_finished_by_the_second_action(env, monkeypatch):
    fake, written = env
    fake.drain_polls = 10**6
    ticks = iter(range(0, 10**6, 600))
    monkeypatch.setattr(node_removal, "clock", lambda: next(ticks))

    ok, progress, _ = _run("remove_cluster_nodes", ["10.0.0.6"])

    assert ok and not any("host rm" in command for command in fake.commands) and written == {}
    assert "chưa gỡ" in progress[-1]["hosts"][0]["message"]

    still, progress, _ = _run("finish_remove_cluster_nodes", ["10.0.0.6"])
    assert still is False and "chưa xong" in progress[0]["message"]

    fake.drain_polls = 0
    done, _progress, _ = _run("finish_remove_cluster_nodes", ["10.0.0.6"])
    assert done and "ceph orch host rm ceph6" in fake.commands and written["CEPH_OSD_NODES"] == "10.0.0.4,10.0.0.5"


def test_a_changed_node_config_refuses_the_proposal(env):
    fake, _written = env

    ok, progress, _ = _run("remove_cluster_nodes", ["10.0.0.6"], _node_config_fingerprint="old")

    assert ok is False and "đã thay đổi" in progress[0]["message"] and fake.commands == []
