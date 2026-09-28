from datetime import datetime, timedelta

import pytest

from shared import evidence_collectors as ec

NOW = datetime(2026, 9, 28, 12, 0)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class FakeTransport:
    def __init__(self, clock=None, ceph=None, host=None, cost=0.0):
        self.calls = []
        self.clock, self.cost = clock, cost
        self._ceph = ceph or (lambda command: {"ok": command})
        self._host = host or (lambda host, command: f"{host}: {command}")

    def _tick(self):
        if self.clock is not None:
            self.clock.value += self.cost

    def ceph(self, command, timeout):
        self.calls.append(("ceph", command, timeout))
        self._tick()
        return self._ceph(command)

    def host(self, host, command, timeout):
        self.calls.append(("host", host, command, timeout))
        self._tick()
        return self._host(host, command)


def test_every_registered_collector_renders_to_a_read_only_command():
    samples = {"osd_id": "12", "devid": "SAMSUNG_MZ7LH_S4X", "target": "10.3.53.1"}
    for collector_id, collector in ec.COLLECTORS.items():
        _, command = ec.render(collector_id, {name: samples[name] for name in collector.params})
        assert command.split()[0] in {"ceph", "uptime", "free", "ip", "ping"}
        assert not set(command.lower().split()) & ec.MUTATING_VERBS


@pytest.mark.parametrize("command", [
    "ceph osd out 3", "ceph osd set noout", "ceph mgr module enable x", "ceph tell osd.1 injectargs --debug",
    "ceph config-key get x", "uptime; reboot", "ping -c 1 host | tee /etc/x", "free -m && halt",
    "echo $(id)", "sudo smartctl -a /dev/sda", "systemctl restart ceph-osd@1", "ip link set eth0 down",
])
def test_mutating_or_chained_commands_are_refused(command):
    with pytest.raises(ec.CollectorRefused):
        ec.assert_read_only(command)


@pytest.mark.parametrize("collector_id,params", [
    ("ceph_osd_metadata", {"osd_id": "1; ceph osd out 1"}),
    ("ceph_osd_metadata", {"osd_id": "osd.1"}),
    ("ceph_osd_metadata", {}),
    ("ceph_device_health", {"devid": "a b"}),
    ("mon_ping", {"target": "host; reboot"}),
    ("mon_ping", {"target": "-f 10.0.0.1"}),
    ("no_such_collector", {}),
])
def test_invalid_parameters_and_unknown_collectors_are_refused(collector_id, params):
    with pytest.raises(ec.CollectorRefused):
        ec.render(collector_id, params)


def test_runner_collects_ceph_and_host_evidence_with_redaction_and_cap():
    transport = FakeTransport(
        ceph=lambda command: {"key": "AQBsecretsecretsecretsecret==", "cmd": command},
        host=lambda host, command: "x" * 5000,
    )
    runner = ec.EvidenceRunner(transport)
    results = runner.run([
        ec.EvidenceRequest("ceph_osd_metadata", params={"osd_id": 3}),
        ec.EvidenceRequest("host_uptime", host="10.0.0.5"),
    ])
    assert [r.status for r in results] == [ec.OK, ec.OK]
    assert results[0].command == "ceph osd metadata 3"
    assert "AQBsecret" not in results[0].output and "[REDACTED]" in results[0].output
    assert results[1].truncated and len(results[1].output) == ec.COLLECTORS["host_uptime"].max_chars
    assert transport.calls[1] == ("host", "10.0.0.5", "uptime", 10)


def test_refused_requests_never_reach_the_transport():
    transport = FakeTransport()
    results = ec.EvidenceRunner(transport).run([
        ec.EvidenceRequest("ceph_osd_metadata", params={"osd_id": "1 && reboot"}),
        ec.EvidenceRequest("host_uptime"),  # a host collector without a host
    ])
    assert [r.status for r in results] == [ec.REFUSED, ec.REFUSED]
    assert transport.calls == []


def test_run_budget_skips_the_remaining_collectors():
    clock = FakeClock()
    transport = FakeTransport(clock=clock, cost=25.0)
    runner = ec.EvidenceRunner(transport, clock=clock)
    ids = ["ceph_osd_tree", "ceph_osd_perf", "ceph_osd_df", "ceph_health_detail"]
    results = runner.run([ec.EvidenceRequest(item) for item in ids])
    assert [r.status for r in results] == [ec.OK, ec.OK, ec.OK, ec.SKIPPED_BUDGET]
    assert len(transport.calls) == 3


def test_failures_open_the_circuit_breaker_per_host_and_it_recovers():
    clock = FakeClock()

    def broken(host, command):
        if host == "bad":
            raise TimeoutError("timed out")
        return "ok"

    transport = FakeTransport(host=broken)
    runner = ec.EvidenceRunner(transport, clock=clock)
    for _ in range(ec.BREAKER_FAILURES):
        assert runner.run([ec.EvidenceRequest("host_uptime", host="bad")])[0].status == ec.TIMEOUT
    assert runner.run([ec.EvidenceRequest("host_uptime", host="bad")])[0].status == ec.SKIPPED_BREAKER
    assert runner.run([ec.EvidenceRequest("host_uptime", host="good")])[0].status == ec.OK
    calls_before = len(transport.calls)
    clock.value += ec.BREAKER_COOLDOWN_SECONDS
    assert runner.run([ec.EvidenceRequest("host_uptime", host="bad")])[0].status == ec.TIMEOUT
    assert len(transport.calls) == calls_before + 1
    # Half-open: a single failure re-opens the breaker.
    assert runner.run([ec.EvidenceRequest("host_uptime", host="bad")])[0].status == ec.SKIPPED_BREAKER


def test_errors_are_evidence_not_exceptions():
    transport = FakeTransport(ceph=lambda command: (_ for _ in ()).throw(RuntimeError("password=hunter2 failed")))
    result = ec.EvidenceRunner(transport).run([ec.EvidenceRequest("ceph_osd_tree")])[0]
    assert result.status == ec.ERROR
    assert "hunter2" not in result.output


def test_per_host_concurrency_is_bounded():
    runner = ec.EvidenceRunner(FakeTransport(), host_concurrency=1)
    slot = runner._slot("busy-host")
    slot.acquire()
    try:
        result = runner.run([ec.EvidenceRequest("host_uptime", host="busy-host")])[0]
    finally:
        slot.release()
    assert result.status == ec.SKIPPED_BUSY


def test_one_run_per_incident_per_cooldown():
    runner = ec.EvidenceRunner(FakeTransport())
    assert runner.claim("inc-1", NOW)
    assert not runner.claim("inc-1", NOW + timedelta(minutes=9))
    assert runner.claim("inc-2", NOW)
    assert runner.claim("inc-1", NOW + timedelta(minutes=10))


def test_ssh_transport_refuses_mutations_before_the_wire(monkeypatch):
    class Cluster:
        ceph_mon_nodes = "10.0.0.1"

    monkeypatch.setattr("shared.cluster_nodes.resolve_ssh_creds", lambda cluster: ("u", "/k", "cephadm", "c"))
    sent = []
    monkeypatch.setattr("watcher.ceph_client._run_remote_command_with", lambda *args, **kwargs: sent.append(args))
    transport = ec.SshTransport(Cluster())
    with pytest.raises(ec.CollectorRefused):
        transport.host("10.0.0.2", "reboot", 5)
    with pytest.raises(ec.CollectorRefused):
        transport.ceph("ceph osd out 1", 5)
    assert sent == []


def test_run_budget_is_a_hard_deadline_not_a_soft_one():
    import threading
    import time

    release = threading.Event()

    def slow(command):
        release.wait(5)
        return {"late": True}

    runner = ec.EvidenceRunner(FakeTransport(ceph=slow), budget_seconds=0.3)
    begin = time.monotonic()
    results = runner.run([ec.EvidenceRequest("ceph_osd_tree"), ec.EvidenceRequest("ceph_osd_perf")])
    elapsed = time.monotonic() - begin
    release.set()
    assert results[0].status == ec.TIMEOUT and "deadline" in results[0].output
    assert results[1].status == ec.SKIPPED_BUDGET
    assert elapsed < 2.0


def test_an_abandoned_slow_command_keeps_its_host_slot():
    import threading

    release = threading.Event()

    def slow(host, command):
        release.wait(5)
        return "late"

    runner = ec.EvidenceRunner(FakeTransport(host=slow), budget_seconds=0.3, host_concurrency=1)
    first = runner.run([ec.EvidenceRequest("host_uptime", host="h1")])[0]
    second = runner.run([ec.EvidenceRequest("host_memory", host="h1")])[0]
    release.set()
    assert first.status == ec.TIMEOUT
    assert second.status == ec.SKIPPED_BUSY
