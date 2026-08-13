import json

import pytest

from vitastor import client


def test_cli_command_uses_official_etcd_options():
    command = client._cli_command("10.0.0.10:2379/v3", "/vita-lab")
    assert command == "vitastor-cli --json --no-color --etcd_address 10.0.0.10:2379/v3 --etcd_prefix /vita-lab status"


def test_cli_command_wraps_docker_and_quotes_values():
    command = client._cli_command("10.0.0.10:2379/v3", exec_mode="docker", container_name="vita mon")
    assert command.startswith("docker exec 'vita mon' vitastor-cli")


def test_container_mode_requires_container_name():
    with pytest.raises(client.VitastorConnectionError):
        client._cli_command("10.0.0.10:2379/v3", exec_mode="podman")


def test_query_status_parses_json(monkeypatch, tmp_path):
    class Channel:
        def recv_exit_status(self): return 0
    class Stream:
        channel = Channel()
        def __init__(self, value): self.value = value
        def read(self): return self.value
    class SSH:
        def load_host_keys(self, *_): pass
        def set_missing_host_key_policy(self, *_): pass
        def connect(self, **kwargs): self.kwargs = kwargs
        def save_host_keys(self, *_): pass
        def exec_command(self, command, timeout):
            return None, Stream(json.dumps({"cluster": {"osd": "3 / 3 up"}}).encode()), Stream(b"")
        def close(self): pass
    monkeypatch.setattr(client.paramiko, "SSHClient", SSH)
    monkeypatch.setattr(client, "KNOWN_HOSTS_PATH", str(tmp_path / "known_hosts"))

    assert client.query_status("10.0.0.20", "root", "/key", "10.0.0.10:2379/v3")["cluster"]


def test_normalize_status_exposes_dashboard_health_and_capacity():
    result = client.normalize_status({
        "etcd_alive": 3, "etcd_count": 3, "etcd_db_size": 4096,
        "mon_count": 2, "mon_master": "mon-1", "osd_up": 3, "osd_count": 3,
        "total_raw": 1000, "free_raw": 400, "pool_count": 2,
        "active_pool_count": 2, "pg_states": {"active": 32},
        "op_stats": {"read": {"iops": 120, "bps": 491520}},
    })

    assert result["health"] == "HEALTHY"
    assert result["capacity"] == {
        "total": 1000, "used": 600, "free": 400, "down": 0, "used_percent": 60.0,
    }
    assert result["io"]["read"]["iops"] == 120


def test_normalize_status_marks_down_osd_critical():
    result = client.normalize_status({
        "etcd_alive": 1, "etcd_count": 1, "osd_up": 2, "osd_count": 3,
        "pg_states": {"active": 8},
    })
    assert result["health"] == "CRITICAL"


def test_normalize_etcd_exposes_quorum_leader_and_slowest_latency():
    result = client.normalize_etcd([
        {"Endpoint": "http://e1:2379", "Status": {"header": {"member_id": 1}, "leader": 1, "dbSize": 100, "raftIndex": 9}},
        {"Endpoint": "http://e2:2379", "Status": {"header": {"member_id": 2}, "leader": 1, "dbSize": 100, "raftIndex": 9}},
        {"Endpoint": "http://e3:2379", "Status": {"header": {"member_id": 3}, "leader": 1, "dbSize": 100, "raftIndex": 9}},
    ], [
        {"endpoint": "http://e1:2379", "health": True, "took": "2.5ms"},
        {"endpoint": "http://e2:2379", "health": True, "took": "800µs"},
        {"endpoint": "http://e3:2379", "health": False, "took": "1s", "error": "timeout"},
    ])
    assert result["quorum"] is True
    assert result["leader_count"] == 1
    assert result["latency_ms"] == 1000
    assert result["db_size"] == 300


def test_query_dashboard_keeps_detail_command_failures_best_effort(monkeypatch, tmp_path):
    class Channel:
        def __init__(self, status): self.status = status
        def recv_exit_status(self): return self.status
    class Stream:
        def __init__(self, value, status=0): self.value, self.channel = value, Channel(status)
        def read(self): return self.value
    class SSH:
        def load_host_keys(self, *_): pass
        def set_missing_host_key_policy(self, *_): pass
        def connect(self, **_): pass
        def save_host_keys(self, *_): pass
        def exec_command(self, command, timeout):
            if command.endswith("status"):
                return None, Stream(b'{"osd_up":2,"osd_count":2}'), Stream(b"")
            if "ls-pools" in command:
                return None, Stream(b"", 1), Stream(b"flag unavailable")
            return None, Stream(b"[]"), Stream(b"")
        def close(self): pass
    monkeypatch.setattr(client.paramiko, "SSHClient", SSH)
    monkeypatch.setattr(client, "KNOWN_HOSTS_PATH", str(tmp_path / "known_hosts"))

    result = client.query_dashboard("host", "root", "/key", "etcd:2379")

    assert result["status"]["osd_up"] == 2
    assert result["pools"] == []
    assert "flag unavailable" in result["errors"]["pools"]
