from watcher import main as watcher_main


def test_watcher_process_lock_allows_only_one_owner(tmp_path):
    lock_path = tmp_path / "watcher.lock"
    first = watcher_main._acquire_watcher_process_lock(str(lock_path))
    assert first is not None

    try:
        assert watcher_main._acquire_watcher_process_lock(str(lock_path)) is None
    finally:
        first.close()

    released = watcher_main._acquire_watcher_process_lock(str(lock_path))
    assert released is not None
    released.close()
