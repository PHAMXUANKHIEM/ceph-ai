import copy

import pytest

from shared import investigation_runbooks as ir
from shared.evidence_collectors import COLLECTORS, render

TOP_FAMILIES = [
    "NODE_UNREACHABLE", "BLUESTORE_SLOW_OP_ALERT", "OSD_LATENCY_HIGH", "OSD_DOWN", "MON_CLOCK_SKEW",
    "PG_DEGRADED", "POOL_NEARFULL", "LARGE_OMAP_OBJECTS", "DEVICE_HEALTH", "SLOW_OPS",
]


def test_shipped_runbooks_are_valid_and_cover_the_top_codes():
    data = ir.load()
    assert ir.validate(data) == []
    assert set(TOP_FAMILIES) <= set(data["runbooks"])


def test_every_planned_request_renders_with_a_full_context():
    data = ir.load()
    context = {"host": "10.3.53.1", "osd_id": "3", "devid": "SAMSUNG_X"}
    for family in list(data["runbooks"]) + ["SOMETHING_NEW"]:
        result = ir.plan(f"{family}:3", context, mon_host="10.3.53.2", data=data)
        assert result.skipped == []
        for request in result.requests:
            render(request.collector_id, request.params)


def test_node_unreachable_pings_from_the_mon_and_reads_the_host():
    result = ir.plan("NODE_UNREACHABLE:10.3.53.9", ir.context_for("NODE_UNREACHABLE:10.3.53.9"), mon_host="10.3.53.1")
    ping = next(r for r in result.requests if r.collector_id == "mon_ping")
    uptime = next(r for r in result.requests if r.collector_id == "host_uptime")
    assert (ping.host, ping.params) == ("10.3.53.1", {"target": "10.3.53.9"})
    assert uptime.host == "10.3.53.9"
    assert result.runbook == "NODE_UNREACHABLE" and result.questions


def test_missing_context_is_skipped_not_guessed():
    result = ir.plan("OSD_LATENCY_HIGH", {}, mon_host="10.3.53.1")
    skipped = {item["collector_id"] for item in result.skipped}
    assert skipped == {"ceph_osd_slow_ops", "ceph_osd_metadata"}
    assert all(r.collector_id not in skipped for r in result.requests)

    no_mon = ir.plan("NODE_UNREACHABLE:10.0.0.9", {"host": "10.0.0.9"}, mon_host=None)
    assert {"collector_id": "mon_ping", "reason": "không có mon"} in no_mon.skipped


def test_unknown_family_uses_the_default_runbook():
    result = ir.plan("BRAND_NEW_CHECK", {}, mon_host="m")
    assert result.runbook == "default"
    assert [r.collector_id for r in result.requests] == ["ceph_health_detail", "ceph_osd_tree", "ceph_crash_ls_new"]


@pytest.mark.parametrize("code,extra,expected", [
    ("OSD_LATENCY_HIGH:3", None, {"osd_id": "3"}),
    ("OSD_DOWN:osd.12", None, {"osd_id": "12"}),
    ("NODE_UNREACHABLE:ceph-node-1.lab", None, {"host": "ceph-node-1.lab"}),
    ("OSD_DOWN", {"osd_id": "7", "host": "10.0.0.2"}, {"osd_id": "7", "host": "10.0.0.2"}),
    ("OSD_DOWN", {"osd_id": "7; reboot", "host": "a b", "junk": "x"}, {}),
    ("NODE_UNREACHABLE:bad host;", None, {}),
])
def test_context_is_extracted_and_validated(code, extra, expected):
    assert ir.context_for(code, extra) == expected


def _broken(mutator):
    data = copy.deepcopy(ir.load())
    mutator(data)
    return ir.validate(data)


@pytest.mark.parametrize("mutator,message", [
    (lambda d: d["runbooks"]["OSD_DOWN"]["collectors"].append("rm_everything"), "unknown collector"),
    (lambda d: d["runbooks"]["OSD_DOWN"]["collectors"].append("host_uptime"), "HOST collector needs host"),
    (lambda d: d["runbooks"]["OSD_DOWN"]["collectors"].append({"id": "ceph_osd_tree", "host": "mon"}),
     "CEPH collector takes no host"),
    (lambda d: d["runbooks"]["OSD_DOWN"]["collectors"].append({"id": "ceph_osd_metadata"}), "params"),
    (lambda d: d["runbooks"]["OSD_DOWN"]["collectors"].append(
        {"id": "ceph_osd_metadata", "params": {"osd_id": "{pool}"}}), "unknown context"),
    (lambda d: d["runbooks"]["OSD_DOWN"].update(collectors=["ceph_osd_tree"] * 9), "ceph collectors"),
    (lambda d: d["runbooks"]["OSD_DOWN"].update(questions=[]), "questions"),
    (lambda d: d["runbooks"]["OSD_DOWN"].update(conclusions=[{"when": "x"}]), "when/then"),
    (lambda d: d["runbooks"].update({"OSD_DOWN:3": {"collectors": ["ceph_osd_tree"], "questions": ["q"]}}),
     "fault family"),
    (lambda d: d.update(version=2), "version"),
])
def test_validator_rejects_broken_runbooks(mutator, message):
    problems = _broken(mutator)
    assert any(message in problem for problem in problems), problems


def test_registry_and_runbooks_stay_in_sync():
    used = {ir._entry(raw)["id"] for rb in [ir.load()["default"], *ir.load()["runbooks"].values()]
            for raw in rb["collectors"]}
    assert used <= set(COLLECTORS)
