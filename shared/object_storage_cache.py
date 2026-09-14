"""Process-local stale-if-error cache for expensive cluster read operations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import RLock
from time import monotonic
from typing import Callable, TypeVar


T = TypeVar("T")
_lock = RLock()
_entries: dict[tuple[str, str], tuple[float, object]] = {}
_refreshing: set[tuple[str, str]] = set()
_refresh_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ceph-cache")


def is_refreshing(namespace: str, key: str) -> bool:
    with _lock:
        return (namespace, key) in _refreshing


def _refresh(cache_key: tuple[str, str], loader: Callable[[], T]) -> None:
    try:
        value = loader()
    except Exception:
        return
    finally:
        with _lock:
            _refreshing.discard(cache_key)
    with _lock:
        _entries[cache_key] = (monotonic(), deepcopy(value))


def _schedule_refresh(cache_key: tuple[str, str], loader: Callable[[], T]) -> None:
    with _lock:
        if cache_key in _refreshing:
            return
        _refreshing.add(cache_key)
    _refresh_executor.submit(_refresh, cache_key, loader)


def get_or_load(
    namespace: str,
    key: str,
    loader: Callable[[], T],
    ttl_seconds: int = 3600,
    *,
    stale_ttl_seconds: int | None = None,
    background_on_miss: bool = False,
    fallback: T | None = None,
) -> T:
    """Return cached data immediately while refreshing expired data."""
    cache_key = (namespace, key)
    now = monotonic()
    with _lock:
        cached = _entries.get(cache_key)
        if cached:
            age = now - cached[0]
            if age < ttl_seconds:
                return deepcopy(cached[1])  # type: ignore[return-value]
            if stale_ttl_seconds is not None and age < stale_ttl_seconds:
                stale = deepcopy(cached[1])
                _schedule_refresh(cache_key, loader)
                return stale  # type: ignore[return-value]
        if background_on_miss:
            _schedule_refresh(cache_key, loader)
            return deepcopy(fallback)  # type: ignore[return-value]
    try:
        value = loader()
    except Exception:
        with _lock:
            stale = _entries.get(cache_key)
            if stale:
                return deepcopy(stale[1])  # type: ignore[return-value]
        raise
    with _lock:
        _entries[cache_key] = (monotonic(), deepcopy(value))
    return value


def invalidate(cluster_id: str, namespace: str | None = None) -> None:
    prefix = f"{cluster_id}:"
    with _lock:
        for cache_key in list(_entries):
            entry_namespace, key = cache_key
            if key.startswith(prefix) and (namespace is None or entry_namespace == namespace):
                _entries.pop(cache_key, None)


def clear() -> None:
    with _lock:
        _entries.clear()
