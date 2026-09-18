"""Process-local stale-if-error cache for expensive cluster read operations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextvars import copy_context
from threading import RLock
from time import monotonic
from typing import Callable, TypeVar


T = TypeVar("T")
_lock = RLock()
_entries: dict[tuple[str, str], tuple[float, object]] = {}
_refreshing: set[tuple[str, str]] = set()
_refresh_errors: set[tuple[str, str]] = set()
_replace_generations: dict[tuple[str, str], int] = {}
_refresh_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ceph-cache")


def is_refreshing(namespace: str, key: str) -> bool:
    with _lock:
        return (namespace, key) in _refreshing


def state(namespace: str, key: str) -> dict[str, object]:
    """Return safe cache state without exposing loader exception details."""
    cache_key = (namespace, key)
    with _lock:
        cached = _entries.get(cache_key)
        return {
            "available": cached is not None,
            "age_seconds": max(0.0, monotonic() - cached[0]) if cached else None,
            "refreshing": cache_key in _refreshing,
            "error": cache_key in _refresh_errors,
        }


def _scope(cache_key: tuple[str, str]) -> tuple[str, str] | None:
    namespace, key = cache_key
    if ":" not in key:
        return None
    return namespace, key.split(":", 1)[0]


def _remove_previous_locked(cache_key: tuple[str, str]) -> None:
    scope = _scope(cache_key)
    if scope is None:
        return
    namespace, cluster_id = scope
    prefix = f"{cluster_id}:"
    for existing in list(_entries):
        if existing != cache_key and existing[0] == namespace and existing[1].startswith(prefix):
            _entries.pop(existing, None)
            _refresh_errors.discard(existing)


def _refresh(
    cache_key: tuple[str, str], loader: Callable[[], T], replace_cluster: bool,
    generation: int | None,
) -> None:
    try:
        value = loader()
    except Exception:
        with _lock:
            _refresh_errors.add(cache_key)
        return
    finally:
        with _lock:
            _refreshing.discard(cache_key)
    with _lock:
        scope = _scope(cache_key)
        if replace_cluster and scope is not None and _replace_generations.get(scope) != generation:
            _refresh_errors.discard(cache_key)
            return
        if replace_cluster:
            _remove_previous_locked(cache_key)
        _entries[cache_key] = (monotonic(), deepcopy(value))
        _refresh_errors.discard(cache_key)


def _schedule_refresh(
    cache_key: tuple[str, str], loader: Callable[[], T], replace_cluster: bool,
) -> None:
    with _lock:
        if cache_key in _refreshing:
            return
        _refreshing.add(cache_key)
        scope = _scope(cache_key) if replace_cluster else None
        generation = None
        if scope is not None:
            generation = _replace_generations.get(scope, 0) + 1
            _replace_generations[scope] = generation
    _refresh_executor.submit(
        copy_context().run, _refresh, cache_key, loader, replace_cluster, generation
    )


def get_or_load(
    namespace: str,
    key: str,
    loader: Callable[[], T],
    ttl_seconds: int = 3600,
    *,
    stale_ttl_seconds: int | None = None,
    background_on_miss: bool = False,
    fallback: T | None = None,
    replace_cluster: bool = False,
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
                _schedule_refresh(cache_key, loader, replace_cluster)
                return stale  # type: ignore[return-value]
        if background_on_miss:
            _schedule_refresh(cache_key, loader, replace_cluster)
            return deepcopy(fallback)  # type: ignore[return-value]
    try:
        value = loader()
    except Exception:
        with _lock:
            _refresh_errors.add(cache_key)
        with _lock:
            stale = _entries.get(cache_key)
        if stale:
                return deepcopy(stale[1])  # type: ignore[return-value]
        raise
    with _lock:
        if replace_cluster:
            _remove_previous_locked(cache_key)
        _entries[cache_key] = (monotonic(), deepcopy(value))
        _refresh_errors.discard(cache_key)
    return value


def invalidate(cluster_id: str, namespace: str | None = None) -> None:
    prefix = f"{cluster_id}:"
    with _lock:
        for cache_key in list(_entries):
            entry_namespace, key = cache_key
            if key.startswith(prefix) and (namespace is None or entry_namespace == namespace):
                _entries.pop(cache_key, None)
                _refresh_errors.discard(cache_key)
        for cache_key in list(_refresh_errors):
            entry_namespace, key = cache_key
            if key.startswith(prefix) and (namespace is None or entry_namespace == namespace):
                _refresh_errors.discard(cache_key)
        for scope in list(_replace_generations):
            scope_namespace, scope_cluster = scope
            if scope_cluster == cluster_id and (namespace is None or scope_namespace == namespace):
                _replace_generations.pop(scope, None)


def clear() -> None:
    with _lock:
        _entries.clear()
        _refresh_errors.clear()
        _replace_generations.clear()
