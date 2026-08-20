import json

import pytest

import watcher.ceph_client as ceph_client
from watcher.collector import collect_relevant_logs, identify_relevant_nodes

MON_CODE_DETAIL = {
    "severity": "HEALTH_WARN",
    "summary": {"message": "clock skew detected on mon.khiempx-mon2, mon.khiempx-mon3", "count": 2},
    "detail": [
        {"message": "mon.khiempx-mon2 clock skew 0.100382s > max 0.05s"},
        {"message": "mon.khiempx-mon3 clock skew 0.854861s > max 0.05s"},
    ],
}

OSD_CODE_DETAIL = {
    "severity": "HEALTH_ERR",
    "summary": {"message": "1 osds down", "count": 1},
    "detail": [{"message": "osd.3 (root=default,host=khiempx-data-b2) is down"}],
}

# No parseable "mon.NAME" mention at all — exercises the fallback path.
UNPARSEABLE_MON_DETAIL = {
    "severity": "HEALTH_WARN",
    "summary": {"message": "some generic mon-related warning", "count": 1},
    "detail": [{"message": "no specific node named here"}],
}


class _FakeChannel:
    def __init__(self, exit_status=0):
        self._exit_status = exit_status

    def recv_exit_status(self):
        return self._exit_status


class _FakeStream:
    def __init__(self, text: str, exit_status: int = 0):
        self._text = text
        self.channel = _FakeChannel(exit_status)

    def read(self):
        return self._text.encode()


class FakeSSHClient:
    """Like tests/test_ceph_client.py's fake, but returns raw text (not JSON) —
    collector.py fetches `docker logs` output, not the health JSON."""

    log_text_by_host: dict = {}
    calls: list = []

    def __init__(self):
        self._host = None

    def set_missing_host_key_policy(self, policy):
        pass

    def load_host_keys(self, path):
        pass

    def save_host_keys(self, path):
        pass

    def connect(self, hostname, username, key_filename, timeout):
        self._host = hostname

    def exec_command(self, command, timeout=None):
        FakeSSHClient.calls.append((self._host, command))
        text = FakeSSHClient.log_text_by_host.get(self._host, "")
        return None, _FakeStream(text), _FakeStream("")

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_ssh(monkeypatch):
    FakeSSHClient.log_text_by_host = {}
    FakeSSHClient.calls = []
    monkeypatch.setattr(ceph_client.paramiko, "SSHClient", FakeSSHClient)
    yield FakeSSHClient


# 2026-08-20: với ceph_code OSD_/PG_, collector hỏi systemd của từng node OSD
# xem osd nào chạy ở đó trước khi lấy log (watcher/osd_hosts.py — trước đây nó
# đoán host). Các assert dưới nói về LỆNH LẤY LOG, nên lệnh thăm dò vị trí phải
# được loại ra thay vì làm chúng sai.
_PLACEMENT_PROBE = "systemctl list-units --all 2>/dev/null | grep -i osd || true"


def _log_calls(fake_ssh):
    return [(h, c) for h, c in fake_ssh.calls if c != _PLACEMENT_PROBE]

def test_identify_relevant_nodes_for_osd_code_returns_only_osd_nodes(monkeypatch):
    from config.settings import settings

    nodes = identify_relevant_nodes("OSD_DOWN", OSD_CODE_DETAIL)

    osd_nodes = set(h.strip() for h in settings.ceph_osd_nodes.split(","))
    mon_nodes = set(h.strip() for h in settings.ceph_mon_nodes.split(","))
    assert set(nodes) <= osd_nodes
    assert not (set(nodes) & mon_nodes)
    assert len(nodes) > 0


def test_identify_relevant_nodes_for_mon_code_parses_mon_names():
    from config.settings import settings

    nodes = identify_relevant_nodes("MON_CLOCK_SKEW", MON_CODE_DETAIL)

    ips = settings.ceph_mon_nodes.split(",")
    names = settings.ceph_mon_hostnames.split(",")
    name_to_ip = dict(zip(names, ips))
    assert set(nodes) == {name_to_ip["khiempx-mon2"], name_to_ip["khiempx-mon3"]}


def test_collect_relevant_logs_for_osd_code_never_touches_mon_nodes(fake_ssh):
    from config.settings import settings

    fake_ssh.log_text_by_host = {h: f"osd log from {h}" for h in settings.ceph_osd_nodes.split(",")}

    nodes, log_excerpt = collect_relevant_logs("OSD_DOWN", OSD_CODE_DETAIL)

    mon_nodes = set(settings.ceph_mon_nodes.split(","))
    contacted_hosts = {host for host, _cmd in fake_ssh.calls}
    assert not (contacted_hosts & mon_nodes)
    for host, command in _log_calls(fake_ssh):
        assert command == f"docker logs {settings.ceph_osd_container_name} --tail 50 2>&1"
    assert "osd log from" in log_excerpt


def test_collect_relevant_logs_for_mon_code_uses_mon_container(fake_ssh):
    from config.settings import settings

    ips = settings.ceph_mon_nodes.split(",")
    names = settings.ceph_mon_hostnames.split(",")
    mon2_ip = dict(zip(names, ips))["khiempx-mon2"]
    fake_ssh.log_text_by_host = {mon2_ip: "mon2 clock skew log line"}

    nodes, log_excerpt = collect_relevant_logs("MON_CLOCK_SKEW", MON_CODE_DETAIL)

    for host, command in fake_ssh.calls:
        assert command == f"docker logs {settings.ceph_container_name} --tail 50 2>&1"
    assert "mon2 clock skew log line" in log_excerpt


def test_collect_relevant_logs_survives_unreachable_node(fake_ssh, monkeypatch):
    from config.settings import settings

    ips = settings.ceph_mon_nodes.split(",")
    names = settings.ceph_mon_hostnames.split(",")
    name_to_ip = dict(zip(names, ips))
    # mon2 has no entry in log_text_by_host -> exec_command still "succeeds"
    # with empty text via the fake; simulate a hard failure instead by making
    # connect() itself unreachable for one host.
    original_connect = fake_ssh.connect

    def flaky_connect(self, hostname, username, key_filename, timeout):
        if hostname == name_to_ip["khiempx-mon3"]:
            raise OSError("no route to host")
        return original_connect(self, hostname, username, key_filename, timeout)

    # monkeypatch (not a raw class-attribute assignment) so this is
    # automatically restored after the test — a bare `fake_ssh.connect = ...`
    # would permanently override FakeSSHClient.connect for every later test
    # in the same pytest session.
    monkeypatch.setattr(fake_ssh, "connect", flaky_connect)
    fake_ssh.log_text_by_host = {name_to_ip["khiempx-mon2"]: "mon2 log ok"}

    nodes, log_excerpt = collect_relevant_logs("MON_CLOCK_SKEW", MON_CODE_DETAIL)

    assert "mon2 log ok" in log_excerpt
    assert "unavailable" in log_excerpt.lower()


def test_identify_relevant_nodes_falls_back_to_last_successful_mon_node(monkeypatch):
    from config.settings import settings

    ips = settings.ceph_mon_nodes.split(",")
    names = settings.ceph_mon_hostnames.split(",")
    mon3_ip = dict(zip(names, ips))["khiempx-mon3"]

    # Simulate: the most recent query_cluster_health() succeeded against mon3.
    monkeypatch.setattr(ceph_client, "last_successful_mon_node", mon3_ip)

    nodes = identify_relevant_nodes("MON_SOMETHING_GENERIC", UNPARSEABLE_MON_DETAIL)

    assert nodes == [mon3_ip]


def test_identify_relevant_nodes_falls_back_to_first_configured_node_when_no_prior_success(
    monkeypatch,
):
    from config.settings import settings

    monkeypatch.setattr(ceph_client, "last_successful_mon_node", None)

    nodes = identify_relevant_nodes("MON_SOMETHING_GENERIC", UNPARSEABLE_MON_DETAIL)

    assert nodes == [settings.ceph_mon_nodes.split(",")[0]]


# --- Multi-deploy-mode support (see tests/test_ceph_client.py's equivalent
# section for the health-check side) -----------------------------------


def test_collect_relevant_logs_uses_podman_logs_in_podman_mode(fake_ssh, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "podman")
    fake_ssh.log_text_by_host = {h: f"osd log from {h}" for h in settings.ceph_osd_nodes.split(",")}

    collect_relevant_logs("OSD_DOWN", OSD_CODE_DETAIL)

    for _host, command in _log_calls(fake_ssh):
        assert command == f"podman logs {settings.ceph_osd_container_name} --tail 50 2>&1"


def test_collect_relevant_logs_uses_journalctl_glob_in_none_mode_for_osd(fake_ssh, monkeypatch):
    """"none" mode has no osd-id -> host mapping (same known gap as
    identify_relevant_nodes' OSD_/PG_ handling) — targets ALL ceph-osd@
    units on the host via a systemd unit glob instead of guessing one id."""
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    fake_ssh.log_text_by_host = {h: "osd unit log" for h in settings.ceph_osd_nodes.split(",")}

    nodes, log_excerpt = collect_relevant_logs("OSD_DOWN", OSD_CODE_DETAIL)

    assert len(nodes) > 0
    for _host, command in _log_calls(fake_ssh):
        assert command == "journalctl -u 'ceph-osd@*' -n 50 --no-pager 2>&1"
    assert "(ceph-osd@*)" in log_excerpt
    assert "osd unit log" in log_excerpt


def test_collect_relevant_logs_uses_journalctl_glob_in_none_mode_for_mon(fake_ssh, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "none")
    ips = settings.ceph_mon_nodes.split(",")
    names = settings.ceph_mon_hostnames.split(",")
    mon2_ip = dict(zip(names, ips))["khiempx-mon2"]
    fake_ssh.log_text_by_host = {mon2_ip: "mon unit log"}

    nodes, log_excerpt = collect_relevant_logs("MON_CLOCK_SKEW", MON_CODE_DETAIL)

    for _host, command in fake_ssh.calls:
        assert command == "journalctl -u 'ceph-mon@*' -n 50 --no-pager 2>&1"
    assert "mon unit log" in log_excerpt


# --- MGR node routing (parallel to OSD_/PG_ -> ceph_osd_nodes) --------------

MGR_CODE_DETAIL = {
    "severity": "HEALTH_ERR",
    "summary": {"message": "mgr module 'balancer' has failed", "count": 1},
    "detail": [{"message": "Module 'balancer' has failed: some traceback"}],
}


def test_identify_relevant_nodes_for_mgr_code_returns_configured_mgr_nodes(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mgr_nodes", "10.20.1.112,10.20.1.95", raising=False)

    nodes = identify_relevant_nodes("MGR_MODULE_ERROR", MGR_CODE_DETAIL)

    assert nodes == ["10.20.1.112", "10.20.1.95"]


def test_identify_relevant_nodes_for_mgr_code_falls_back_when_no_mgr_nodes_configured(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mgr_nodes", "", raising=False)
    monkeypatch.setattr(ceph_client, "last_successful_mon_node", None)

    nodes = identify_relevant_nodes("MGR_MODULE_ERROR", MGR_CODE_DETAIL)

    # Degrades to the generic MON fallback rather than returning nothing.
    assert nodes == settings.ceph_mon_nodes.split(",")[:1]


# --- cephadm mode log collection: discovers exact daemon names via
# `cephadm ls` instead of guessing a container/unit name -------------------


def test_collect_relevant_logs_cephadm_mode_discovers_and_fetches_osd_daemon(monkeypatch):
    import watcher.collector as collector

    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "10.20.1.112", raising=False)

    calls = []

    def fake_run(host, command):
        calls.append((host, command))
        if command == "cephadm ls --no-detail":
            return (
                '[{"name": "mon.khiempx-ceph1"}, '
                '{"name": "mgr.khiempx-ceph1.loylll"}, '
                '{"name": "osd.0"}]'
            )
        return "osd.0 log line"

    monkeypatch.setattr(collector, "run_command_on_node", fake_run)

    nodes, log_excerpt = collect_relevant_logs("OSD_DOWN", OSD_CODE_DETAIL)

    assert nodes == ["10.20.1.112"]
    # Discovery call first, then exactly the matching osd.* daemon (not
    # mon./mgr. even though this host runs all three, colocated).
    assert calls[0] == ("10.20.1.112", "cephadm ls --no-detail")
    log_commands = [c for h, c in calls[1:]]
    assert log_commands == ["cephadm logs --name osd.0 2>&1 | tail -n 50"]
    assert "(osd.0)" in log_excerpt
    assert "osd.0 log line" in log_excerpt


def test_collect_relevant_logs_cephadm_mode_mgr_code_fetches_mgr_daemon_with_random_suffix(monkeypatch):
    import watcher.collector as collector

    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "10.20.1.112", raising=False)

    def fake_run(host, command):
        if command == "cephadm ls --no-detail":
            return '[{"name": "mon.khiempx-ceph1"}, {"name": "mgr.khiempx-ceph1.loylll"}]'
        assert command == "cephadm logs --name mgr.khiempx-ceph1.loylll 2>&1 | tail -n 50"
        return "mgr log line"

    monkeypatch.setattr(collector, "run_command_on_node", fake_run)

    nodes, log_excerpt = collect_relevant_logs("MGR_MODULE_ERROR", MGR_CODE_DETAIL)

    assert "(mgr.khiempx-ceph1.loylll)" in log_excerpt
    assert "mgr log line" in log_excerpt


def test_collect_relevant_logs_cephadm_mode_no_matching_daemon_reports_clearly(monkeypatch):
    import watcher.collector as collector

    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "10.20.1.112", raising=False)

    monkeypatch.setattr(
        collector, "run_command_on_node", lambda host, command: '[{"name": "mon.khiempx-ceph1"}]'
    )

    nodes, log_excerpt = collect_relevant_logs("OSD_DOWN", OSD_CODE_DETAIL)

    assert "no osd.* daemon found" in log_excerpt


# --- Story A (Crash-module visibility): RECENT_CRASH bypasses
# identify_relevant_nodes entirely, using ceph_client.run_ceph_json_command
# ("ceph crash ls-new") instead of an SSH daemon-log grep -----------------

RECENT_CRASH_DETAIL = {
    "severity": "HEALTH_WARN",
    "summary": {"message": "1 daemons have recently crashed", "count": 1},
    "detail": [{"message": "osd.3 crashed on host khiempx-data-b2 at 2026-07-30 10:00:00"}],
}


def test_collect_relevant_logs_for_recent_crash_uses_crash_ls_new_not_ssh_logs(fake_ssh, monkeypatch):
    from config.settings import settings

    mon_ip = settings.ceph_mon_nodes.split(",")[0]
    fake_ssh.log_text_by_host = {
        mon_ip: json.dumps(
            [
                {
                    "crash_id": "2026-07-30_10:00:00.000000Z_abcd1234",
                    "entity_name": "osd.3",
                    "utsname_hostname": "khiempx-data-b2",
                    "timestamp": "2026-07-30 10:00:00.000000",
                    "backtrace": ["frame 1", "frame 2"],
                }
            ]
        )
    }

    nodes, log_excerpt = collect_relevant_logs("RECENT_CRASH", RECENT_CRASH_DETAIL)

    assert nodes == [mon_ip]
    assert "2026-07-30_10:00:00.000000Z_abcd1234" in log_excerpt
    assert "osd.3" in log_excerpt
    assert "khiempx-data-b2" in log_excerpt
    assert "frame 1\nframe 2" in log_excerpt
    # Never touched the OSD/MON log-tail SSH path.
    for _host, command in fake_ssh.calls:
        assert "ceph crash ls-new" in command


def test_collect_relevant_logs_for_recent_crash_with_no_entries(fake_ssh, monkeypatch):
    from config.settings import settings

    mon_ip = settings.ceph_mon_nodes.split(",")[0]
    fake_ssh.log_text_by_host = {mon_ip: json.dumps([])}

    nodes, log_excerpt = collect_relevant_logs("RECENT_CRASH", RECENT_CRASH_DETAIL)

    assert nodes == [mon_ip]
    assert "no entries" in log_excerpt


def test_collect_relevant_logs_for_recent_crash_survives_all_mon_nodes_unreachable(
    fake_ssh, monkeypatch
):
    def always_fail_connect(self, hostname, username, key_filename, timeout):
        raise OSError("no route to host")

    monkeypatch.setattr(fake_ssh, "connect", always_fail_connect)

    nodes, log_excerpt = collect_relevant_logs("RECENT_CRASH", RECENT_CRASH_DETAIL)

    assert nodes == []
    assert "unavailable" in log_excerpt.lower()


# --- Story B (DeviceHealth visibility): DEVICE_HEALTH* also bypasses
# identify_relevant_nodes, using "ceph device ls" instead -------------------

DEVICE_HEALTH_DETAIL = {
    "severity": "HEALTH_WARN",
    "summary": {"message": "1 device(s) expected to fail soon", "count": 1},
    "detail": [{"message": "Device ... on khiempx-data-b2 is expected to fail soon"}],
}


def test_collect_relevant_logs_for_device_health_uses_device_ls_not_ssh_logs(fake_ssh, monkeypatch):
    from config.settings import settings

    mon_ip = settings.ceph_mon_nodes.split(",")[0]
    fake_ssh.log_text_by_host = {
        mon_ip: json.dumps(
            [
                {
                    "devid": "SEAGATE_ST12000_ZA12345",
                    "daemons": ["osd.7"],
                    "location": [{"host": "khiempx-data-b2", "dev": "sda"}],
                    "life_expectancy_min": "2026-08-10T00:00:00.000000+00:00",
                    "life_expectancy_max": "2026-08-24T00:00:00.000000+00:00",
                },
                {
                    # No prediction set yet — must be excluded from the excerpt.
                    "devid": "SEAGATE_ST12000_OTHER",
                    "daemons": ["osd.2"],
                    "location": [{"host": "khiempx-data-b1", "dev": "sdb"}],
                    "life_expectancy_min": None,
                    "life_expectancy_max": "0.000000",
                },
            ]
        )
    }

    nodes, log_excerpt = collect_relevant_logs("DEVICE_HEALTH", DEVICE_HEALTH_DETAIL)

    assert nodes == [mon_ip]
    assert "SEAGATE_ST12000_ZA12345" in log_excerpt
    assert "osd.7" in log_excerpt
    assert "khiempx-data-b2:sda" in log_excerpt
    assert "2026-08-24T00:00:00.000000+00:00" in log_excerpt
    assert "SEAGATE_ST12000_OTHER" not in log_excerpt
    for _host, command in fake_ssh.calls:
        assert "ceph device ls" in command


def test_collect_relevant_logs_for_device_health_toomany_variant_also_routed(fake_ssh, monkeypatch):
    from config.settings import settings

    mon_ip = settings.ceph_mon_nodes.split(",")[0]
    fake_ssh.log_text_by_host = {mon_ip: json.dumps([])}

    nodes, log_excerpt = collect_relevant_logs(
        "DEVICE_HEALTH_TOOMANY", {"severity": "HEALTH_ERR", "summary": {"message": "x"}, "detail": []}
    )

    assert nodes == [mon_ip]
    assert "no devices with a life-expectancy prediction" in log_excerpt


def test_collect_relevant_logs_for_device_health_survives_all_mon_nodes_unreachable(
    fake_ssh, monkeypatch
):
    def always_fail_connect(self, hostname, username, key_filename, timeout):
        raise OSError("no route to host")

    monkeypatch.setattr(fake_ssh, "connect", always_fail_connect)

    nodes, log_excerpt = collect_relevant_logs("DEVICE_HEALTH", DEVICE_HEALTH_DETAIL)

    assert nodes == []
    assert "unavailable" in log_excerpt.lower()


def test_collect_relevant_logs_cephadm_mode_survives_malformed_ls_output(monkeypatch):
    import watcher.collector as collector

    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "10.20.1.112", raising=False)

    monkeypatch.setattr(collector, "run_command_on_node", lambda host, command: "not json at all")

    nodes, log_excerpt = collect_relevant_logs("OSD_DOWN", OSD_CODE_DETAIL)

    assert "no osd.* daemon found" in log_excerpt


# --- osd_id -> host: tra thật, không đoán (2026-08-20) ----------------------
# Hồi quy cho lỗi: identify_relevant_nodes trả về TOÀN BỘ node OSD cho mọi
# ceph_code OSD_/PG_, danh sách phẳng ấy vào prompt LLM nên model đoán và gán
# osd vào sai node ("osd.2, osd.4 và osd.5 trên node <ip sai>").


def test_osd_code_targets_only_the_host_that_actually_runs_that_osd(fake_ssh, monkeypatch):
    from config.settings import settings

    osd_nodes = [h.strip() for h in settings.ceph_osd_nodes.split(",")]
    assert len(osd_nodes) > 1, "test cần ít nhất 2 node OSD mới có gì để phân biệt"
    owner = osd_nodes[1]

    # Chỉ `owner` báo có unit của osd.3; các node khác không có.
    fake_ssh.log_text_by_host = {
        h: ("ceph-osd@3.service loaded active running" if h == owner else "ceph-osd@9.service")
        for h in osd_nodes
    }

    osd_host_map: dict[int, str] = {}
    nodes = identify_relevant_nodes("OSD_DOWN", OSD_CODE_DETAIL, None, osd_host_map)

    assert nodes == [owner]
    assert osd_host_map == {3: owner}


def test_osd_code_falls_back_to_all_osd_nodes_when_nothing_resolves(fake_ssh):
    """Không node nào nạp unit của osd.3 -> không tra được. Phải quay về gom
    cả cụm VÀ để `osd_host_map` rỗng — chính chỗ rỗng đó là tín hiệu cho
    prompt biết là "chưa xác định được", thay vì một host đoán bừa."""
    from config.settings import settings

    osd_nodes = [h.strip() for h in settings.ceph_osd_nodes.split(",")]
    fake_ssh.log_text_by_host = {h: "ceph-osd@9.service" for h in osd_nodes}

    osd_host_map: dict[int, str] = {}
    nodes = identify_relevant_nodes("OSD_DOWN", OSD_CODE_DETAIL, None, osd_host_map)

    assert set(nodes) == set(osd_nodes)
    assert osd_host_map == {}


def test_osd_code_with_no_osd_id_in_detail_keeps_the_broad_fallback(fake_ssh):
    """Check không nêu đích danh osd nào (ví dụ PG_DEGRADED chung chung) —
    không có gì để tra, giữ nguyên hành vi gom cả cụm."""
    from config.settings import settings

    detail_without_osd_id = {"severity": "HEALTH_WARN", "detail": [{"message": "Degraded data redundancy"}]}
    osd_nodes = [h.strip() for h in settings.ceph_osd_nodes.split(",")]

    osd_host_map: dict[int, str] = {}
    nodes = identify_relevant_nodes("PG_DEGRADED", detail_without_osd_id, None, osd_host_map)

    assert set(nodes) == set(osd_nodes)
    assert osd_host_map == {}
    # Không nêu osd nào thì cũng không được SSH đi thăm dò vô ích.
    assert fake_ssh.calls == []
