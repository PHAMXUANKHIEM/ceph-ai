"""Small persistent stale-if-error cache for expensive read-only Ceph queries."""

import fcntl
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from pathlib import Path
from threading import RLock
from time import monotonic, sleep, time
from typing import Callable, Dict, Tuple, TypeVar


T = TypeVar("T")
_lock = RLock()
# (created_at, serialized record) — xem `_load_entry` về lý do giữ text.
_memory: Dict[Tuple[str, str], Tuple[float, str]] = {}
_cache_dir = Path(os.environ.get("CEPH_AI_CACHE_DIR", "/var/lib/ceph-ai/cache"))
_MAX_STALE_SECONDS = 900
_CACHE_LOCK_TIMEOUT_SECONDS = 5.0
_CACHE_LOCK_POLL_SECONDS = 0.05
_MISSING = object()
_refreshing: set[Tuple[str, str]] = set()
_refresh_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ceph-query-cache")
_metrics = {
    "cache_hit_total": 0,
    "cache_miss_total": 0,
    "cache_stale_total": 0,
    "cache_load_total": 0,
    "cache_refresh_enqueued_total": 0,
}


class CacheLockError(RuntimeError):
    """Raised when a cache operation cannot obtain its per-key lock."""


class CachePersistenceError(RuntimeError):
    """Raised when a versioned cache value cannot be persisted."""


def _record_cache_metric(name: str) -> None:
    with _lock:
        _metrics[name] += 1


def get_metrics() -> dict[str, int]:
    """Return cache counters for the admin diagnostics endpoint."""
    with _lock:
        return dict(_metrics)


def _path(namespace: str, key: str) -> Path:
    safe_namespace = "".join(char if char.isalnum() or char in "-_" else "-" for char in namespace)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return _cache_dir / f"{safe_namespace}-{digest}.json"


_DECODE_ERRORS = (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError)


def _decode_value(text: str):
    """Deserialize one record's value into an object owned by the caller."""
    return json.loads(text)["value"]


def _read_entry(namespace: str, key: str):
    """Return (created_at, serialized record, freshly decoded value)."""
    try:
        text = _path(namespace, key).read_text(encoding="utf-8")
        record = json.loads(text)
        return float(record["created_at"]), text, record["value"]
    except _DECODE_ERRORS:
        return None


def _read(namespace: str, key: str):
    entry = _read_entry(namespace, key)
    if entry is None:
        return None
    created_at, _text, value = entry
    return created_at, value


def _write(namespace: str, key: str, created_at: float, value: object) -> str | None:
    """Persist one record and return the exact text written.

    Callers reuse that text to populate the in-memory cache: the value has to
    be serialized for disk anyway, so keeping the serialized form costs
    nothing extra and removes the need to deep-copy the value.
    """
    try:
        text = json.dumps({"created_at": created_at, "value": value}, separators=(",", ":"))
        _cache_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        destination = _path(namespace, key)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        return text
    except (OSError, TypeError, ValueError):
        # Cache failures must never make the live Ceph page unavailable.
        return None



def _lock_path(namespace: str, key: str) -> Path:
    return _path(namespace, key).with_suffix(".lock")


@contextmanager
def _loader_lock(namespace: str, key: str, *, timeout_seconds: float | None = _CACHE_LOCK_TIMEOUT_SECONDS):
    """Serialize cache access, with a bounded wait for normal readers."""
    handle = None
    acquired = False
    try:
        try:
            _cache_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
            lock_path = _lock_path(namespace, key)
            handle = lock_path.open("a+")
            try:
                lock_path.chmod(0o600)
            except OSError:
                pass
            if timeout_seconds is None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                acquired = True
            else:
                deadline = monotonic() + timeout_seconds
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except BlockingIOError:
                        if monotonic() >= deadline:
                            break
                        sleep(_CACHE_LOCK_POLL_SECONDS)
        except OSError:
            pass
        yield acquired
    finally:
        if handle is not None:
            if acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


@contextmanager
def key_lock(namespace: str, key: str, *, timeout_seconds: float | None = None):
    """Hold the cross-process lock for one cache key.

    Callers use this for work that must be serialized with other processes,
    such as a network refresh followed by publishing its result.  The lock is
    intentionally separate from the snapshot value key when the caller needs
    to protect a longer operation than a single cache write.
    """
    with _loader_lock(namespace, key, timeout_seconds=timeout_seconds) as acquired:
        if not acquired:
            raise CacheLockError(f"could not acquire cache lock for {namespace}:{key}")
        yield


def _load_entry(namespace: str, key: str, *, prefer_disk: bool = False):
    """Return (created_at, a value owned by the caller), or None.

    `_memory` holds each record's SERIALIZED text, not a parsed object. The
    cache must hand every caller an independent object, and re-parsing the
    text is roughly four times cheaper than deep-copying a parsed one — the
    old `deepcopy` cost more than reading the file from disk. Storing text
    also makes aliasing structurally impossible, so no caller can corrupt
    the cache by mutating what it received.

    Must be called with `_lock` held.
    """
    cache_key = (namespace, key)
    cached = _memory.get(cache_key)
    if prefer_disk:
        entry = _read_entry(namespace, key)
        if entry is not None:
            created_at, text, value = entry
            _memory[cache_key] = (created_at, text)
            return created_at, value
        if not _path(namespace, key).exists():
            _memory.pop(cache_key, None)
            return None
    else:
        if cached is not None and not _path(namespace, key).exists():
            _memory.pop(cache_key, None)
            cached = None
        if cached is None:
            entry = _read_entry(namespace, key)
            if entry is None:
                return None
            created_at, text, value = entry
            _memory[cache_key] = (created_at, text)
            return created_at, value
    if cached is None:
        return None
    created_at, text = cached
    return created_at, _decode_value(text)


def _fresh_value(namespace: str, key: str, ttl_seconds: int):
    now = time()
    with _lock:
        entry = _load_entry(namespace, key)
        if entry is not None:
            created_at, value = entry
            if now - created_at < ttl_seconds:
                return value
    return _MISSING


def _stale_value(namespace: str, key: str):
    with _lock:
        entry = _load_entry(namespace, key)
        if entry is not None and time() - entry[0] < _MAX_STALE_SECONDS:
            return entry[1]
    return _MISSING


def get_cached(
    namespace: str,
    key: str,
    *,
    max_age_seconds: int | None = None,
    prefer_disk: bool = False,
) -> tuple[object, float] | None:
    """Return a cached value and its age without invoking a loader."""
    now = time()
    with _lock:
        entry = _load_entry(namespace, key, prefer_disk=prefer_disk)
        if entry is None:
            return None
        created_at, value = entry
        age_seconds = max(0.0, now - created_at)
        if max_age_seconds is not None and age_seconds > max_age_seconds:
            return None
        return value, age_seconds


def store(namespace: str, key: str, value: object) -> None:
    """Persist a JSON-compatible value for an immediate cache reader."""
    with _loader_lock(namespace, key, timeout_seconds=None) as lock_acquired:
        if not lock_acquired:
            raise CacheLockError(f"could not acquire cache lock for {namespace}:{key}")
        created_at = time()
        cache_key = (namespace, key)
        with _lock:
            text = _write(namespace, key, created_at, value)
            if text:
                _memory[cache_key] = (created_at, text)


def store_versioned(namespace: str, key: str, value: dict) -> dict:
    """Atomically persist a mapping with a monotonically increasing generation.

    The generation is read and written while holding the same cross-process
    lock used by the cache loader.  Snapshot publishers can therefore stamp
    an ordering number without racing another web/worker process.
    """
    if not isinstance(value, dict):
        raise TypeError("versioned cache values must be dictionaries")
    with _loader_lock(namespace, key, timeout_seconds=None) as lock_acquired:
        if not lock_acquired:
            raise CacheLockError(f"could not acquire cache lock for {namespace}:{key}")
        previous = _read(namespace, key)
        previous_value = previous[1] if previous is not None else {}
        try:
            previous_generation = int(previous_value.get("generation", 0)) if isinstance(previous_value, dict) else 0
        except (TypeError, ValueError):
            previous_generation = 0
        # Bản sao nông là đủ: chỉ thêm một khóa ở tầng trên, và `_memory`
        # giữ text nên không còn đường nào alias vào cache.
        stored = dict(value)
        stored["generation"] = max(0, previous_generation) + 1
        created_at = time()
        cache_key = (namespace, key)
        with _lock:
            text = _write(namespace, key, created_at, stored)
            if not text:
                raise CachePersistenceError(f"could not persist cache value for {namespace}:{key}")
            _memory[cache_key] = (created_at, text)
        return stored


def update_value(namespace: str, key: str, updates: dict) -> dict | None:
    """Atomically update an existing value without changing its generation.

    This is for metadata such as a failed refresh attempt. The cached data
    version and its original creation time remain unchanged.
    """
    if not isinstance(updates, dict):
        raise TypeError("cache updates must be dictionaries")
    with _loader_lock(namespace, key, timeout_seconds=None) as lock_acquired:
        if not lock_acquired:
            raise CacheLockError(f"could not acquire cache lock for {namespace}:{key}")
        previous = _read(namespace, key)
        if previous is None or not isinstance(previous[1], dict):
            return None
        created_at, previous_value = previous
        # `_read` vừa parse ra một object mới, không ai khác giữ tham chiếu.
        stored = previous_value
        stored.update(updates)
        with _lock:
            text = _write(namespace, key, created_at, stored)
            if not text:
                raise CachePersistenceError(f"could not persist cache value for {namespace}:{key}")
            _memory[(namespace, key)] = (created_at, text)
        return stored


def _refresh(
    namespace: str,
    key: str,
    loader: Callable[[], T],
    ttl_seconds: int,
) -> None:
    try:
        get_or_load(namespace, key, loader, ttl_seconds=ttl_seconds)
    except Exception:
        pass
    finally:
        with _lock:
            _refreshing.discard((namespace, key))


def _schedule_refresh(
    namespace: str,
    key: str,
    loader: Callable[[], T],
    ttl_seconds: int,
) -> bool:
    cache_key = (namespace, key)
    with _lock:
        if cache_key in _refreshing:
            return False
        _refreshing.add(cache_key)
    _refresh_executor.submit(
        copy_context().run,
        _refresh,
        namespace,
        key,
        loader,
        ttl_seconds,
    )
    _record_cache_metric("cache_refresh_enqueued_total")
    return True


def schedule_refresh(
    namespace: str,
    key: str,
    loader: Callable[[], T],
    ttl_seconds: int,
) -> bool:
    """Populate a cache key in the background, never blocking the caller.

    For a loader too slow to sit inside a page render: serve whatever cheap
    answer the caller already has and let the next request read the real one.
    Returns False when a refresh for this key is already in flight.
    """
    return _schedule_refresh(namespace, key, loader, ttl_seconds)


def get_or_load(
    namespace: str,
    key: str,
    loader: Callable[[], T],
    *,
    ttl_seconds: int = 45,
    stale_ttl_seconds: int | None = None,
) -> T:
    """Load a JSON-compatible Ceph result, reusing fresh disk or RAM data.

    The cache survives Podman recreation because ``/var/lib/ceph-ai`` is a
    mounted volume.  If a live SSH/Ceph query fails, retain at most 15 minutes
    of the last successful result rather than blanking an operator page.
    """
    cache_key = (namespace, key)
    cached = _fresh_value(namespace, key, ttl_seconds)
    if cached is not _MISSING:
        _record_cache_metric("cache_hit_total")
        return cached  # type: ignore[return-value]
    _record_cache_metric("cache_miss_total")
    if stale_ttl_seconds is not None:
        stale = get_cached(namespace, key)
        if stale is not None and ttl_seconds <= stale[1] < stale_ttl_seconds:
            _schedule_refresh(namespace, key, loader, ttl_seconds)
            _record_cache_metric("cache_stale_total")
            return stale[0]  # type: ignore[return-value]
    try:
        with _loader_lock(namespace, key) as lock_acquired:
            if lock_acquired:
                cached = _fresh_value(namespace, key, ttl_seconds)
                if cached is not _MISSING:
                    _record_cache_metric("cache_hit_total")
                    return cached  # type: ignore[return-value]
                value = loader()
                _record_cache_metric("cache_load_total")
                created_at = time()
                with _lock:
                    text = _write(namespace, key, created_at, value)
                    if text:
                        _memory[cache_key] = (created_at, text)
                return value

        # The lock owner may have filled the cache just after our timeout.
        cached = _fresh_value(namespace, key, ttl_seconds)
        if cached is not _MISSING:
            _record_cache_metric("cache_hit_total")
            return cached  # type: ignore[return-value]
        stale = _stale_value(namespace, key)
        if stale is not _MISSING:
            _record_cache_metric("cache_stale_total")
            return stale  # type: ignore[return-value]
        raise CacheLockError(f"could not acquire cache lock for {namespace}:{key}")
    except Exception:
        cached = _stale_value(namespace, key)
        if cached is not _MISSING:
            _record_cache_metric("cache_stale_total")
            return cached  # type: ignore[return-value]
        raise


def fingerprint(namespace: str, key: str) -> tuple[int, int] | None:
    """Cheap change signal for one cached record: (mtime_ns, size).

    `_write` publishes through `os.replace` of a fresh temp file, so any new
    value lands with a new mtime. That makes a single `stat` enough to answer
    "did this change?" — around a thousand times cheaper than reading and
    deserializing the record, which matters for pollers that only need to
    detect a change rather than consume the payload.

    Returns None when the record does not exist.
    """
    try:
        stat_result = _path(namespace, key).stat()
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size)


def invalidate(namespace: str, key: str) -> None:
    """Remove one cache entry after a confirmed mutation."""
    # Wait for an in-flight loader to finish, then remove its result. This
    # prevents a pre-mutation snapshot from being written back after this
    # function returns.
    with _loader_lock(namespace, key, timeout_seconds=None) as lock_acquired:
        if not lock_acquired:
            raise CacheLockError(f"could not acquire cache lock for {namespace}:{key}")
        with _lock:
            _memory.pop((namespace, key), None)
            try:
                _path(namespace, key).unlink()
            except FileNotFoundError:
                pass
