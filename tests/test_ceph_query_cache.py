import json

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
