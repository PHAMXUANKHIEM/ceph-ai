from types import SimpleNamespace

import dashboard.cache_warmup as cache_warmup


def test_snapshot_warmup_reads_persistent_snapshots_without_collecting(monkeypatch):
    clusters = [
        SimpleNamespace(id="cluster-a"),
        SimpleNamespace(id="cluster-b"),
    ]
    calls = []

    def fake_read_snapshot(cluster_id):
        calls.append(cluster_id)
        if cluster_id == "cluster-a":
            return {"generation": 7, "age_seconds": 1.2, "stale": False}
        return None

    monkeypatch.setattr(cache_warmup, "read_snapshot", fake_read_snapshot)

    warmed = cache_warmup._warm_cluster_snapshots(clusters)

    assert warmed == 1
    assert calls == ["cluster-a", "cluster-b"]


def test_snapshot_warmup_isolates_one_cluster_read_failure(monkeypatch):
    clusters = [
        SimpleNamespace(id="cluster-a"),
        SimpleNamespace(id="cluster-b"),
    ]

    def broken_read_snapshot(cluster_id):
        if cluster_id == "cluster-a":
            raise OSError("cache temporarily unavailable")
        return {"generation": 3, "age_seconds": 2.0, "stale": False}

    monkeypatch.setattr(cache_warmup, "read_snapshot", broken_read_snapshot)

    assert cache_warmup._warm_cluster_snapshots(clusters) == 1


def test_warm_only_hydrates_snapshots(monkeypatch):
    clusters = [SimpleNamespace(id="cluster-a")]
    calls = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def expunge_all(self):
            return None

    monkeypatch.setattr(cache_warmup.db, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(cache_warmup, "list_active_clusters", lambda _session: clusters)
    monkeypatch.setattr(
        cache_warmup,
        "_warm_cluster_snapshots",
        lambda selected: calls.append(selected) or 1,
    )
    monkeypatch.setattr(
        cache_warmup,
        "_warm_block_storage",
        lambda selected: calls.append(("block-storage", selected)) or 1,
    )

    cache_warmup._warm()

    assert calls == [clusters, ("block-storage", clusters)]


def test_block_storage_warmup_schedules_one_background_refresh_per_cluster(monkeypatch):
    clusters = [SimpleNamespace(id="cluster-a"), SimpleNamespace(id="cluster-b")]
    calls = []
    monkeypatch.setattr(cache_warmup, "get_or_load", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert cache_warmup._warm_block_storage(clusters) == 2
    assert [call[0][:2] for call in calls] == [
        ("block-storage", "cluster-a:inventory"),
        ("block-storage", "cluster-b:inventory"),
    ]
    assert all(call[1]["background_on_miss"] is True for call in calls)
