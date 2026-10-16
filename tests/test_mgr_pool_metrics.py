"""Pools inventory: usage and client I/O from the mgr, configuration cached (09/10/2026)."""

from types import SimpleNamespace

import pytest

from watcher import cluster_snapshot_collector as collector
from watcher import mgr_pool_metrics as mp

PAGE = """ceph_pool_metadata{pool_id="2",name=".rgw.root",type="replicated",description="replica:3"} 1.0
ceph_pool_metadata{pool_id="5",name="volumes",type="replicated",description="replica:3"} 1.0
ceph_pool_stored{pool_id="2"} 4639.0
ceph_pool_max_avail{pool_id="2"} 49997352960.0
ceph_pool_objects{pool_id="2"} 17.0
ceph_pool_rd{pool_id="2"} 1333.0
ceph_pool_wr{pool_id="2"} 0.0
ceph_pool_stored{pool_id="5"} 2048.0
ceph_pool_max_avail{pool_id="5"} 1000.0
ceph_pool_objects{pool_id="5"} 3.0
ceph_pool_rd{pool_id="5"} 100.0
ceph_pool_wr{pool_id="5"} 600.0
ceph_health_status 1.0
"""
DETAIL = {"pools": [
    {"pool_name": ".rgw.root", "size": 3, "pg_num": 32, "crush_rule": 0, "application_metadata": {"rgw": {}}},
    {"pool_name": "volumes", "size": 3, "pg_num": 64, "crush_rule": 0, "application_metadata": {"rbd": {}}},
]}
RULES = [{"rule_id": 0, "rule_name": "replicated_rule"}]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(mp, "_previous", {})
    monkeypatch.setattr(mp, "_url", None)
    monkeypatch.setattr(collector, "_SLOW_FACTS", {})
    monkeypatch.setattr(collector, "_map_epochs", lambda cluster: (100, 7))


def test_pool_samples_are_joined_to_names():
    pools = mp.parse_pools(PAGE)

    assert pools["volumes"] == {"stored": 2048.0, "max_avail": 1000.0, "objects": 3.0, "rd": 100.0, "wr": 600.0}
    assert mp.parse_pools("") == {} and mp.parse_pools("<html>standby</html>") == {}


def test_rates_come_from_the_previous_sample_and_survive_a_counter_reset():
    first_df, first_io = mp.payloads({"volumes": {"rd": 100.0, "wr": 600.0}}, now=1000.0)
    _df, second_io = mp.payloads({"volumes": {"rd": 160.0, "wr": 660.0}}, now=1060.0)
    _df, reset_io = mp.payloads({"volumes": {"rd": 5.0, "wr": 5.0}}, now=1120.0)

    assert first_io["pool_stats"][0]["client_io_rate"] == {}
    assert second_io["pool_stats"][0]["client_io_rate"] == {"read_op_per_sec": 1.0, "write_op_per_sec": 1.0}
    assert reset_io["pool_stats"][0]["client_io_rate"] == {}  # an OSD restart resets counters
    assert first_df["pools"][0] == {"name": "volumes", "stats": {"stored": 0, "max_avail": 0, "objects": 0}}


def _cluster():
    return SimpleNamespace(id="c1", is_default=True)


def test_pool_rows_use_cached_config_and_mgr_usage(monkeypatch):
    from watcher import inventory_queries

    config_reads = []
    monkeypatch.setattr(inventory_queries, "collect_pool_config", lambda cluster: config_reads.append(1) or (DETAIL, RULES))
    monkeypatch.setattr(mp, "fetch_pools", lambda: mp.parse_pools(PAGE))
    monkeypatch.setattr(collector, "collect_pool_rows", lambda cluster: pytest.fail("old path not needed"))

    rows = collector._collect_pool_rows(_cluster())
    collector._collect_pool_rows(_cluster())

    assert len(config_reads) == 1  # osdmap epoch unchanged: configuration reused
    volumes = next(row for row in rows if row["name"] == "volumes")
    assert (volumes["used_bytes"], volumes["total_bytes"], volumes["objects"]) == (2048, 3048, 3)
    assert volumes["crush_rule"] == "replicated_rule" and volumes["applications"] == ["rbd"]


@pytest.mark.parametrize("case", ["no mgr", "new pool", "flag off", "no config"])
def test_any_gap_falls_back_to_the_four_ceph_commands(monkeypatch, case):
    from watcher import inventory_queries

    detail = DETAIL if case != "new pool" else {"pools": [*DETAIL["pools"], {"pool_name": "fresh", "size": 3}]}
    if case == "no config":
        def broken(cluster):
            raise inventory_queries.CephQueryError("down")
        monkeypatch.setattr(inventory_queries, "collect_pool_config", broken)
    else:
        monkeypatch.setattr(inventory_queries, "collect_pool_config", lambda cluster: (detail, RULES))
    monkeypatch.setattr(mp, "fetch_pools", lambda: None if case == "no mgr" else mp.parse_pools(PAGE))
    monkeypatch.setattr(collector.settings, "ceph_pool_usage_from_mgr", case != "flag off")
    monkeypatch.setattr(collector, "collect_pool_rows", lambda cluster: ["old path"])

    assert collector._collect_pool_rows(_cluster()) == ["old path"]


def test_an_empty_standby_page_rediscovers_the_mgr(monkeypatch):
    import importlib

    real = importlib.reload(mp).fetch_pools  # undo the conftest stub for this module-level test
    monkeypatch.setattr(mp, "_url", None)
    found = []

    def discover():
        found.append(1)
        return "http://mgr:9283/metrics"

    assert real(discover=discover, fetch=lambda url: "") is None
    real(discover=discover, fetch=lambda url: PAGE)

    assert len(found) == 2
