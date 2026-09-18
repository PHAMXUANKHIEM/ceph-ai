from time import monotonic

from shared import object_storage_cache


def test_background_on_miss_uses_fallback_after_stale_window(monkeypatch):
    key = ("buckets", "cluster-1:inventory")
    monkeypatch.setattr(object_storage_cache, "_entries", {
        key: (monotonic() - 30, {"items": ["old"]}),
    })
    scheduled = []
    monkeypatch.setattr(
        object_storage_cache,
        "_schedule_refresh",
        lambda cache_key, loader, replace_cluster: scheduled.append(cache_key),
    )

    result = object_storage_cache.get_or_load(
        "buckets",
        "cluster-1:inventory",
        lambda: {"items": ["fresh"]},
        ttl_seconds=1,
        stale_ttl_seconds=5,
        background_on_miss=True,
        fallback={"items": ["snapshot"]},
    )

    assert result == {"items": ["snapshot"]}
    assert scheduled == [key]
