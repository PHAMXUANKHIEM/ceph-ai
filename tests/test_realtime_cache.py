from __future__ import annotations

import json
import os
import time


def test_disk_cache_prune_keeps_newest_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("CEPH_AI_CACHE_DIR", str(tmp_path))
    import shared.ceph_query_cache as cache

    cache._cache_dir = tmp_path
    for index in range(3):
        path = tmp_path / f"cluster-state-events-{index}.json"
        path.write_text(json.dumps({"created_at": index, "value": {"index": index}}), encoding="utf-8")
        os.utime(path, (index + 1, index + 1))
    result = cache.prune_disk_cache(max_bytes=10_000, max_files=2)
    assert result["removed"] == 1
    assert len(list(tmp_path.glob("*.json"))) == 2
