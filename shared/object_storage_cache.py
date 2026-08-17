"""Small process-local cache for expensive RGW inventory reads."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from time import monotonic
from typing import Callable, TypeVar


T = TypeVar("T")
_lock = RLock()
_entries: dict[tuple[str, str], tuple[float, object]] = {}


def get_or_load(namespace: str, key: str, loader: Callable[[], T], ttl_seconds: int = 3600) -> T:
    """Return a fresh cached value, retaining the last good value on RGW errors."""
    cache_key = (namespace, key)
    now = monotonic()
    with _lock:
        cached = _entries.get(cache_key)
        if cached and now - cached[0] < ttl_seconds:
            return deepcopy(cached[1])  # type: ignore[return-value]
    try:
        value = loader()
    except Exception:
        with _lock:
            stale = _entries.get(cache_key)
            if stale:
                return deepcopy(stale[1])  # type: ignore[return-value]
        raise
    with _lock:
        _entries[cache_key] = (now, deepcopy(value))
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
