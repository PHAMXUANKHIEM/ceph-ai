import json

import pytest

from shared import deterministic_triage as dt

PING_OK = "PING 10.3.53.9 56(84) bytes of data.\n3 packets transmitted, 3 received, 0% packet loss, time 2003ms"
PING_FAIL = "PING 10.3.53.9 56(84) bytes of data.\n3 packets transmitted, 0 received, 100% packet loss, time 2044ms"


def _tree(**states):
    nodes = [{"id": -1, "name": "default", "type": "root"}]
    nodes += [{"id": int(osd), "name": f"osd.{osd}", "type": "osd", "status": state} for osd, state in states.items()]
    return json.dumps({"nodes": nodes, "stray": []})


def ev(collector_id, status="ok", output="", ref=None):
    return {"collector_id": collector_id, "status": status, "output_redacted": output, "id": ref or f"ev-{collector_id}"}


@pytest.mark.parametrize("rows,conclusion,action", [
    ([ev("mon_ping", output=PING_OK), ev("host_uptime", "timeout", "timed out"), ev("ceph_osd_tree", output=_tree(**{"0": "up"}))],
     "SSH_UNAVAILABLE", None),
    ([ev("mon_ping", "error", PING_FAIL), ev("host_uptime", "error"), ev("ceph_osd_tree", output=_tree(**{"0": "up", "1": "down"}))],
     "HOST_DOWN", None),
    ([ev("mon_ping", "error", PING_FAIL), ev("host_uptime", "error"), ev("ceph_osd_tree", output=_tree(**{"0": "up", "1": "up"}))],
     "MGMT_NETWORK", None),
    ([ev("mon_ping", output=PING_OK), ev("host_uptime", output=" 10:01:02 up 4 min,  0 users,  load average: 0.5")],
     "HOST_RECENTLY_REBOOTED", None),
    ([ev("mon_ping", output=PING_OK), ev("host_uptime", output=" 13:57:06 up 2 days,  5:12,  0 users")],
     "TRANSIENT", None),
    ([ev("mon_ping", "timeout", "timed out")], dt.UNKNOWN, None),
    ([], dt.UNKNOWN, None),
])
def test_node_unreachable_rules(rows, conclusion, action):
    result = dt.triage("NODE_UNREACHABLE:10.3.53.9", rows)
    assert result.conclusion == conclusion
    assert result.action_id == action
    if result.is_known:
        assert result.cited and result.recommendation
        assert "reboot" not in (result.action_id or "")


def test_management_network_never_recommends_a_reboot_action():
    rows = [ev("mon_ping", "error", PING_FAIL), ev("host_uptime", "error"), ev("ceph_osd_tree", output=_tree(**{"0": "up"}))]
    result = dt.triage("NODE_UNREACHABLE:10.3.53.9", rows)
    assert result.action_id is None and "không reboot" in result.recommendation


def _perf(**latency):
    return json.dumps({"osdstats": {"osd_perf_infos": [
        {"id": int(osd), "perf_stats": {"commit_latency_ms": value, "apply_latency_ms": value}}
        for osd, value in latency.items()]}})


@pytest.mark.parametrize("code,perf,conclusion", [
    ("OSD_LATENCY_HIGH:3", _perf(**{"0": 5, "1": 6, "2": 4, "3": 180}), "SINGLE_OSD_OUTLIER"),
    ("OSD_LATENCY_HIGH:osd.3", _perf(**{"0": 150, "1": 160, "2": 120, "3": 180}), "CLUSTER_WIDE_LOAD"),
    ("OSD_LATENCY_HIGH:3", _perf(**{"0": 5, "1": 6, "2": 4, "3": 7}), "RECOVERED"),
    ("OSD_LATENCY_HIGH:9", _perf(**{"0": 5, "1": 6, "2": 4, "3": 7}), dt.UNKNOWN),
    ("OSD_LATENCY_HIGH", _perf(**{"0": 5, "1": 6, "2": 4, "3": 7}), dt.UNKNOWN),
    ("OSD_LATENCY_HIGH:3", _perf(**{"0": 5, "3": 180}), dt.UNKNOWN),
])
def test_osd_latency_rules(code, perf, conclusion):
    assert dt.triage(code, [ev("ceph_osd_perf", output=perf)]).conclusion == conclusion


def test_legacy_top_level_perf_format_is_read():
    legacy = json.dumps({"osd_perf_infos": json.loads(_perf(**{"0": 5, "1": 6, "2": 4, "3": 180}))["osdstats"]["osd_perf_infos"]})
    assert dt.triage("OSD_LATENCY_HIGH:3", [ev("ceph_osd_perf", output=legacy)]).conclusion == "SINGLE_OSD_OUTLIER"


def test_truncated_or_failed_json_is_unknown_not_a_guess():
    truncated = _perf(**{"0": 5, "1": 6, "2": 4, "3": 180})[:40]
    assert dt.triage("OSD_LATENCY_HIGH:3", [ev("ceph_osd_perf", output=truncated)]).conclusion == dt.UNKNOWN
    assert dt.triage("OSD_LATENCY_HIGH:3", [ev("ceph_osd_perf", "timeout")]).conclusion == dt.UNKNOWN


def test_clock_skew_names_the_mon_and_an_approval_gated_action():
    status = {"time_skew_status": {"mon-a": {"health": "HEALTH_OK", "skew": 0}, "mon-b": {"health": "HEALTH_WARN", "skew": 0.9}}}
    result = dt.triage("MON_CLOCK_SKEW", [ev("ceph_time_sync", output=json.dumps(status))])
    assert result.conclusion == "MON_SKEWED" and "mon-b" in result.summary
    assert result.action_id == "resync_ntp"
    healthy = {"time_skew_status": {"mon-a": {"health": "HEALTH_OK"}}}
    assert dt.triage("MON_CLOCK_SKEW", [ev("ceph_time_sync", output=json.dumps(healthy))]).conclusion == "RECOVERED"
    assert dt.triage("MON_CLOCK_SKEW", []).conclusion == dt.UNKNOWN


def test_suggested_actions_exist_in_the_action_policy():
    import yaml

    from shared.investigation_runbooks import RUNBOOK_PATH

    policy = yaml.safe_load((RUNBOOK_PATH.parent / "action_policy.yaml").read_text(encoding="utf-8"))
    assert "resync_ntp" in policy["action_ids"]


def test_orm_like_rows_and_unknown_family():
    class Row:
        id, collector_id, status = "row-1", "ceph_time_sync", "ok"
        output_redacted = json.dumps({"time_skew_status": {"mon-a": {"health": "HEALTH_WARN"}}})

    result = dt.triage("MON_CLOCK_SKEW", [Row()])
    assert result.cited == ["row-1"]
    assert dt.triage("POOL_NEARFULL", [Row()]).conclusion == dt.UNKNOWN


@pytest.mark.parametrize("text,minutes", [
    (" 13:57:06 up  5:12,  0 users", 312), ("up 3 min, 1 user", 3), ("up 2 days,  1:00", 2940),
    ("up 1 day, 4 min", 1444), ("garbage", None),
])
def test_uptime_parser(text, minutes):
    assert dt.uptime_minutes(text) == minutes
