"""Bảo trì node lần lượt (worker/executor/host_maintenance.py) run through cluster_deploy.run()."""

import copy
import json
import shlex

import pytest

from worker.executor import cluster_deploy, host_maintenance, node_removal
from worker.executor.ssh_executor import ExecutorError

HOSTS = {"10.0.0.1": "ceph1", "10.0.0.2": "ceph2", "10.0.0.3": "ceph3"}
NODES = [{"host": ip, "roles": ["MON", "OSD"]} for ip in HOSTS]


class FakeCluster:
    def __init__(self, *, mons=3, active_mgr="ceph1.abc", standbys=("ceph2.def",), recover_after=1,
                 never_back=None, not_ok=None, baseline=("POOL_NO_REDUNDANCY",)):
        self.mons, self.active_mgr, self.standbys = mons, active_mgr, list(standbys)
        self.recover_after, self.never_back, self.not_ok = recover_after, never_back, not_ok
        self.baseline = set(baseline)
        self.in_maintenance: set[str] = set()
        self.rebooting: dict[str, int] = {}
        self.degraded_polls = 0
        self.commands: list[str] = []
        self.ssh: list[tuple[str, str]] = []

    def ceph(self, host, command, **kwargs):
        inner = shlex.split(command)[-1] if command.startswith("cephadm shell") else command
        self.commands.append(inner)
        cmd = inner.replace(" --format json", "")
        if cmd == "ceph orch host ls":
            return json.dumps([{"hostname": name, "addr": ip} for ip, name in HOSTS.items()])
        if cmd == "ceph health":
            return json.dumps({"status": "HEALTH_WARN"})
        if cmd == "ceph health detail":
            checks = set(self.baseline)
            if self.in_maintenance or self.degraded_polls > 0:
                checks |= {"OSD_DOWN", "PG_DEGRADED"}
                self.degraded_polls -= 1
            return json.dumps({"checks": {code: {} for code in checks}})
        if cmd == "ceph mon dump":
            return json.dumps({"mons": [{"name": f"ceph{i}"} for i in range(1, self.mons + 1)]})
        if cmd == "ceph mgr dump":
            return json.dumps({"active_name": self.active_mgr, "standbys": [{"name": n} for n in self.standbys]})
        if cmd.startswith("ceph mgr fail"):
            self.active_mgr, self.standbys = self.standbys[0], [self.active_mgr]
            return ""
        if cmd.startswith("ceph orch host ok-to-stop"):
            name = cmd.split()[-1]
            return f"{name}: NOT OK to stop: would reduce availability" if name == self.not_ok else "ok to stop"
        if cmd.startswith("ceph orch host maintenance enter"):
            self.in_maintenance.add(cmd.split()[-1])
            return ""
        if cmd.startswith("ceph orch host maintenance exit"):
            self.in_maintenance.discard(cmd.split()[-1])
            self.degraded_polls = self.recover_after
            return ""
        raise AssertionError(f"unexpected {cmd}")

    def ssh_call(self, host, command, **kwargs):
        self.ssh.append((host, command))
        if command == host_maintenance.REBOOT_COMMAND:
            self.rebooting[host] = 2
            raise ExecutorError("connection closed")
        if command == "true":
            if host == self.never_back:
                raise ExecutorError("timed out")
            if self.rebooting.get(host, 0) > 0:
                self.rebooting[host] -= 1
                raise ExecutorError("connection refused")
            return ""
        raise AssertionError(f"unexpected ssh {command}")


def _patch(monkeypatch, fake):
    monkeypatch.setattr(node_removal, "execute_command", fake.ceph)
    monkeypatch.setattr(node_removal, "configured_nodes", lambda cluster=None: NODES)
    monkeypatch.setattr(node_removal, "resolve_ssh_creds", lambda cluster=None: ("root", "/k", "cephadm", ""))
    monkeypatch.setattr(node_removal, "_cluster", lambda params: None)
    monkeypatch.setattr(host_maintenance, "execute_command", fake.ssh_call)
    monkeypatch.setattr(host_maintenance, "sleep", lambda seconds: None)
    ticks = iter(range(0, 10**7, 60))
    monkeypatch.setattr(host_maintenance, "clock", lambda: next(ticks))


def _run(targets):
    calls = []
    body = {"targets": targets}
    ok = cluster_deploy.run("a1", "rolling_node_maintenance", body, "i1",
                            lambda pk, progress: calls.append(copy.deepcopy(progress)))
    return ok, calls[-1], body


def test_every_host_is_rebooted_in_order_under_maintenance(monkeypatch):
    fake = FakeCluster()
    _patch(monkeypatch, fake)

    ok, progress, body = _run(["10.0.0.2", "10.0.0.1", "10.0.0.3"])

    assert ok, progress
    enters = [c.split()[-1] for c in fake.commands if c.startswith("ceph orch host maintenance enter")]
    assert enters == ["ceph2", "ceph1", "ceph3"]
    assert [h for h, c in fake.ssh if c == host_maintenance.REBOOT_COMMAND] == ["10.0.0.2", "10.0.0.1", "10.0.0.3"]
    assert not fake.in_maintenance and body["_done"] == ["10.0.0.2", "10.0.0.1", "10.0.0.3"]
    # the active MGR moved away before ceph1 went down, and the pre-existing warning never blocked the run
    assert fake.commands.index("ceph mgr fail ceph1.abc") < fake.commands.index("ceph orch host maintenance enter ceph1")


def test_a_host_that_does_not_come_back_stops_the_run_before_the_next(monkeypatch):
    fake = FakeCluster(never_back="10.0.0.1")
    _patch(monkeypatch, fake)

    ok, progress, _ = _run(["10.0.0.1", "10.0.0.2"])

    assert ok is False and "10.0.0.1 chưa trả lời SSH" in progress[-1]["message"]
    assert "ceph1" in fake.in_maintenance  # left in maintenance, and said so
    assert not any("ceph2" in c for c in fake.commands if "maintenance enter" in c)


def test_a_cluster_that_does_not_recover_stops_the_run(monkeypatch):
    fake = FakeCluster(recover_after=10**6)
    _patch(monkeypatch, fake)

    ok, progress, _ = _run(["10.0.0.1", "10.0.0.2"])

    assert ok is False and "OSD_DOWN" in progress[-1]["message"]
    assert [c for c in fake.commands if "maintenance enter" in c] == ["ceph orch host maintenance enter ceph1"]


@pytest.mark.parametrize(("kwargs", "targets", "reason"), [
    ({"mons": 2}, ["10.0.0.1"], "mất quorum"),
    ({"not_ok": "ceph1"}, ["10.0.0.1"], "chưa an toàn để dừng"),
    ({"standbys": ()}, ["10.0.0.1"], "MGR active duy nhất"),
    ({}, ["10.0.0.9"], "không có trong"),
])
def test_unsafe_maintenance_never_enters(monkeypatch, kwargs, targets, reason):
    fake = FakeCluster(**kwargs)
    _patch(monkeypatch, fake)

    ok, progress, _ = _run(targets)

    message = " ".join(step.get("message") or "" for step in progress)
    assert ok is False and reason in message
    assert not any("maintenance enter" in c for c in fake.commands)
