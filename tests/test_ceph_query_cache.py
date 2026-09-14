import json
from contextlib import contextmanager

import pytest

from shared import ceph_query_cache


def test_persistent_cache_survives_memory_reset(monkeypatch, tmp_path):
    monkeypatch.setattr(ceph_query_cache, "_cache_dir", tmp_path)
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    calls = []

    assert ceph_query_cache.get_or_load("rbd-trash", "cluster:pool", lambda: calls.append(1) or [{"id": "a"}]) == [{"id": "a"}]
    monkeypatch.setattr(ceph_query_cache, "_memory", {})
    assert ceph_query_cache.get_or_load("rbd-trash", "cluster:pool", lambda: calls.append(2) or []) == [{"id": "a"}]
    assert calls == [1]
    assert json.loads(next(tmp_path.iterdir()).read_text())["value"] == [{"id": "a"}]


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
