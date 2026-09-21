import json
import threading
from contextlib import contextmanager

import pytest

from shared import ceph_query_cache
from shared.request_context import get_request_id, reset_request_id, set_request_id


def test_persistent_cache_survives_memory_reset(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    calls = []

    assert ceph_query_cache.get_or_load("rbd-trash", "cluster:pool", lambda: calls.append(1) or [{"id": "a"}]) == [{"id": "a"}]
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    assert ceph_query_cache.get_or_load("rbd-trash", "cluster:pool", lambda: calls.append(2) or []) == [{"id": "a"}]
    assert calls == [1]
    # The cache also creates a per-key lock file.  Read the JSON snapshot by
    # its deterministic path instead of relying on directory iteration order
    # (an empty lock file can otherwise be selected on CI).
    assert json.loads(
        ceph_query_cache._path("rbd-trash", "cluster:pool").read_text()
    )["value"] == [{"id": "a"}]


def test_cache_metrics_distinguish_load_and_hit(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    before = ceph_query_cache.get_metrics()

    assert ceph_query_cache.get_or_load("metrics", "cluster", lambda: {"ok": True}) == {"ok": True}
    assert ceph_query_cache.get_or_load("metrics", "cluster", lambda: {"ok": False}) == {"ok": True}
    after = ceph_query_cache.get_metrics()
    assert after["cache_load_total"] >= before["cache_load_total"] + 1
    assert after["cache_hit_total"] >= before["cache_hit_total"] + 1


def test_storage_metrics_are_bounded_and_include_file_sizes(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    (tmp_path / "one.json").write_text("123", encoding="utf-8")
    (tmp_path / "two.lock").write_text("12", encoding="utf-8")

    metrics = ceph_query_cache.get_storage_metrics()

    assert metrics["available"] is True
    assert metrics["files"] >= 2
    assert metrics["bytes"] >= 5
    assert metrics["largest_file_bytes"] >= 3


def test_background_refresh_propagates_request_correlation(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    observed = []
    completed = threading.Event()

    def loader():
        observed.append(get_request_id())
        completed.set()
        return {"ok": True}

    ceph_query_cache.store("metrics", "background", {"ok": False})
    token = set_request_id("refresh-trace")
    try:
        assert ceph_query_cache.get_or_load(
            "metrics", "background", loader, ttl_seconds=0, stale_ttl_seconds=60
        ) == {"ok": False}
    finally:
        reset_request_id(token)

    assert completed.wait(2)
    assert observed == ["refresh-trace"]


def test_persistent_cache_keeps_recent_value_when_live_query_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    assert ceph_query_cache.get_or_load("rbd-pools", "cluster", lambda: ["volumes"], ttl_seconds=0) == ["volumes"]

    def broken():
        raise RuntimeError("SSH down")

    assert ceph_query_cache.get_or_load("rbd-pools", "cluster", broken, ttl_seconds=0) == ["volumes"]


def test_invalidate_removes_persistent_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    ceph_query_cache.get_or_load("rbd-inventory", "cluster:pool", lambda: [])
    ceph_query_cache.invalidate("rbd-inventory", "cluster:pool")
    assert not ceph_query_cache._path("rbd-inventory", "cluster:pool").exists()


def test_missing_shared_file_invalidates_another_process_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    assert ceph_query_cache.get_or_load("rbd-trash", "cluster:pool", lambda: ["old"]) == ["old"]
    ceph_query_cache._path("rbd-trash", "cluster:pool").unlink()
    assert ceph_query_cache.get_or_load("rbd-trash", "cluster:pool", lambda: ["new"]) == ["new"]


def test_get_cached_reads_persisted_value_and_reports_its_age(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    ceph_query_cache.store("dashboard-health", "cluster", {"health": "OK"})
    monkeypatch.setattr(ceph_query_cache, "_memory", {})

    cached = ceph_query_cache.get_cached("dashboard-health", "cluster", max_age_seconds=60)

    assert cached is not None
    value, age_seconds = cached
    assert value == {"health": "OK"}
    assert age_seconds >= 0


def test_versioned_store_reports_persistence_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    monkeypatch.setattr(ceph_query_cache, "_write", lambda *args, **kwargs: False)

    with pytest.raises(ceph_query_cache.CachePersistenceError):
        ceph_query_cache.store_versioned("cluster-snapshot", "cluster", {"health": "OK"})


def test_update_value_preserves_generation_and_cache_age(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    first = ceph_query_cache.store_versioned("cluster-snapshot", "cluster", {"health": "OK"})

    updated = ceph_query_cache.update_value(
        "cluster-snapshot", "cluster", {"last_error": "MON unreachable"}
    )

    assert updated is not None
    assert updated["generation"] == first["generation"]
    assert updated["last_error"] == "MON unreachable"
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    assert ceph_query_cache.get_cached("cluster-snapshot", "cluster")[0]["last_error"] == "MON unreachable"


def test_get_or_load_does_not_load_without_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})

    @contextmanager
    def unavailable_lock(*args, **kwargs):
        yield False

    calls = []
    monkeypatch.setattr(ceph_query_cache, "_loader_lock", unavailable_lock)

    with pytest.raises(ceph_query_cache.CacheLockError):
        ceph_query_cache.get_or_load(
            "cluster-snapshot",
            "cluster",
            lambda: calls.append("loaded") or {"health": "OK"},
        )
    assert calls == []


def test_cache_never_hands_out_an_object_it_still_owns(monkeypatch, tmp_path):
    """`_memory` giữ text đã serialize thay vì object đã parse, nên mỗi lần
    đọc là một lần parse mới — vừa rẻ hơn deepcopy ~4 lần, vừa khiến việc
    alias vào cache trở thành bất khả thi. Test này chốt tính chất đó: caller
    sửa thứ mình nhận được thì lần đọc sau vẫn phải sạch."""
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})

    published = ceph_query_cache.store_versioned(
        "cluster-snapshot", "cluster", {"pools": [{"name": "rbd"}]}
    )
    published["pools"][0]["name"] = "ĐÃ BỊ SỬA"

    first, _age = ceph_query_cache.get_cached("cluster-snapshot", "cluster")
    assert first["pools"][0]["name"] == "rbd"
    first["pools"][0]["name"] = "SỬA LẦN HAI"

    second, _age = ceph_query_cache.get_cached("cluster-snapshot", "cluster", prefer_disk=True)
    assert second["pools"][0]["name"] == "rbd"

    updated = ceph_query_cache.update_value("cluster-snapshot", "cluster", {"note": "x"})
    updated["pools"][0]["name"] = "SỬA LẦN BA"
    third, _age = ceph_query_cache.get_cached("cluster-snapshot", "cluster")
    assert third["pools"][0]["name"] == "rbd"
    assert third["note"] == "x"


def test_store_does_not_alias_the_callers_value(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})

    source = {"nodes": [{"host": "node-a"}]}
    ceph_query_cache.store("inventory", "cluster", source)
    source["nodes"][0]["host"] = "ĐÃ BỊ SỬA"

    cached, _age = ceph_query_cache.get_cached("inventory", "cluster")
    assert cached["nodes"][0]["host"] == "node-a"
