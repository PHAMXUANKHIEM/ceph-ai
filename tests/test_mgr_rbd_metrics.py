"""Volume I/O from the mgr's per-image RBD counters (09/10/2026)."""

import pytest

from watcher import mgr_rbd_metrics as rbd
from watcher import volume_monitor

REAL_FETCH = rbd.fetch_images  # conftest stubs it for every other test

PAGE = """# HELP ceph_rbd_write_latency_sum RBD image writes latency (nsec) Total
ceph_rbd_read_ops{pool="volumes",namespace="",image="vol-a"} 100.0
ceph_rbd_write_ops{pool="volumes",namespace="",image="vol-a"} 1000.0
ceph_rbd_read_latency_sum{pool="volumes",namespace="",image="vol-a"} 2000000.0
ceph_rbd_read_latency_count{pool="volumes",namespace="",image="vol-a"} 100.0
ceph_rbd_write_latency_sum{pool="volumes",namespace="",image="vol-a"} 50000000.0
ceph_rbd_write_latency_count{pool="volumes",namespace="",image="vol-a"} 1000.0
ceph_rbd_write_ops{pool="volumes",namespace="",image="vol-idle"} 7.0
ceph_rbd_write_ops{pool="images",namespace="",image="img-1"} 3.0
"""


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(rbd, "_previous", {})
    monkeypatch.setattr(rbd, "_page", None)
    monkeypatch.setattr(rbd, "_url", None)


def test_counters_are_keyed_by_pool_and_image():
    images = rbd.parse_images(PAGE)

    assert images[("volumes", "vol-a")]["write_ops"] == 1000.0
    assert set(images) == {("volumes", "vol-a"), ("volumes", "vol-idle"), ("images", "img-1")}
    assert rbd.parse_images("") == {}


def test_rates_and_latency_come_from_the_difference_with_the_previous_sample():
    first = rbd.parse_images(PAGE)
    later = rbd.parse_images(PAGE.replace("} 100.0\nceph_rbd_write_ops", "} 160.0\nceph_rbd_write_ops")
                             .replace("} 1000.0\nceph_rbd_read_latency_sum", "} 1540.0\nceph_rbd_read_latency_sum")
                             .replace("read_latency_sum{pool=\"volumes\",namespace=\"\",image=\"vol-a\"} 2000000.0",
                                      "read_latency_sum{pool=\"volumes\",namespace=\"\",image=\"vol-a\"} 5000000.0")
                             .replace("read_latency_count{pool=\"volumes\",namespace=\"\",image=\"vol-a\"} 100.0",
                                      "read_latency_count{pool=\"volumes\",namespace=\"\",image=\"vol-a\"} 160.0")
                             .replace("write_latency_sum{pool=\"volumes\",namespace=\"\",image=\"vol-a\"} 50000000.0",
                                      "write_latency_sum{pool=\"volumes\",namespace=\"\",image=\"vol-a\"} 77000000.0")
                             .replace("write_latency_count{pool=\"volumes\",namespace=\"\",image=\"vol-a\"} 1000.0",
                                      "write_latency_count{pool=\"volumes\",namespace=\"\",image=\"vol-a\"} 1540.0"))

    assert rbd.samples_for_pool("volumes", first, now=1000.0) == []  # first sample: no rate yet
    samples = rbd.samples_for_pool("volumes", later, now=1060.0)

    assert samples == [{"pool": "volumes", "image": "vol-a", "iops": 10.0,
                        "read_latency_ms": 0.05, "write_latency_ms": 0.05}]  # vol-idle had no I/O


def test_a_counter_reset_gives_no_sample():
    rbd.samples_for_pool("volumes", rbd.parse_images(PAGE), now=1000.0)
    reset = rbd.parse_images(PAGE.replace("} 1000.0", "} 3.0"))

    assert rbd.samples_for_pool("volumes", reset, now=1060.0) == []


def test_one_page_serves_every_pool_of_a_poll_and_failures_rediscover(monkeypatch):
    found, downloads = [], []

    def discover():
        found.append(1)
        return "http://mgr:9283/metrics"

    def download(url):
        downloads.append(url)
        return PAGE

    assert REAL_FETCH(discover=discover, download=download) is not None
    REAL_FETCH(discover=discover, download=download)
    assert len(downloads) == 1  # reused within the poll

    monkeypatch.setattr(rbd, "_page", None)
    assert REAL_FETCH(discover=discover, download=lambda url: "") is None  # standby / stats unset
    assert REAL_FETCH(discover=discover, download=download) is not None
    assert len(found) == 2


def test_volume_monitor_uses_the_mgr_and_never_spawns_iostat(monkeypatch):
    monkeypatch.setattr(volume_monitor.mgr_rbd_metrics, "fetch_images", lambda: rbd.parse_images(PAGE))
    monkeypatch.setattr(volume_monitor.ceph_client, "configured_rbd_pools", lambda: ["volumes", "vms"])
    monkeypatch.setattr(volume_monitor.ceph_client, "query_rbd_iostat", lambda pool: pytest.fail("no cephadm"))

    assert volume_monitor._pool_samples_from_mgr("vms", rbd.parse_images(PAGE)) == []  # pool without stats
    volume_monitor.check_volumes()


def test_without_mgr_stats_the_monitor_falls_back_to_iostat(monkeypatch):
    queried = []
    monkeypatch.setattr(volume_monitor.mgr_rbd_metrics, "fetch_images", lambda: None)
    monkeypatch.setattr(volume_monitor.ceph_client, "configured_rbd_pools", lambda: ["volumes"])
    monkeypatch.setattr(volume_monitor.ceph_client, "query_rbd_iostat", lambda pool: queried.append(pool) or [])

    volume_monitor.check_volumes()

    assert queried == ["volumes"]
