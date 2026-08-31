"""Small persistent stale-if-error cache for expensive read-only Ceph queries."""

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from threading import RLock
from time import time
from typing import Callable, Dict, Tuple, TypeVar


T = TypeVar("T")
_lock = RLock()
_memory: Dict[Tuple[str, str], Tuple[float, object]] = {}
_cache_dir = Path(os.environ.get("CEPH_AI_CACHE_DIR", "/var/lib/ceph-ai/cache"))
_MAX_STALE_SECONDS = 900


def _path(namespace: str, key: str) -> Path:
    safe_namespace = "".join(char if char.isalnum() or char in "-_" else "-" for char in namespace)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return _cache_dir / f"{safe_namespace}-{digest}.json"


def _read(namespace: str, key: str):
    try:
        record = json.loads(_path(namespace, key).read_text(encoding="utf-8"))
        created_at = float(record["created_at"])
        return created_at, record["value"]
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        return None


def _write(namespace: str, key: str, created_at: float, value: object) -> None:
    try:
        _cache_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        destination = _path(namespace, key)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"created_at": created_at, "value": value}, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    except (OSError, TypeError, ValueError):
        # Cache failures must never make the live Ceph page unavailable.
        return


def get_or_load(namespace: str, key: str, loader: Callable[[], T], *, ttl_seconds: int = 45) -> T:
    """Load a JSON-compatible Ceph result, reusing fresh disk or RAM data.

    The cache survives Podman recreation because ``/var/lib/ceph-ai`` is a
    mounted volume.  If a live SSH/Ceph query fails, retain at most 15 minutes
    of the last successful result rather than blanking an operator page.
    """
    cache_key = (namespace, key)
    now = time()
    with _lock:
        cached = _memory.get(cache_key)
        # Another service (Worker) invalidates the shared file after a
        # mutation.  Treat its absence as an invalidation of this process's
        # RAM copy too, so Dashboard does not wait for the TTL.
        if cached is not None and not _path(namespace, key).exists():
            _memory.pop(cache_key, None)
            cached = None
        cached = cached or _read(namespace, key)
        if cached:
            created_at, value = cached
            _memory[cache_key] = cached
            if now - created_at < ttl_seconds:
                return deepcopy(value)  # type: ignore[return-value]
    try:
        value = loader()
    except Exception:
        with _lock:
            cached = _memory.get(cache_key) or _read(namespace, key)
            if cached and now - cached[0] < _MAX_STALE_SECONDS:
                return deepcopy(cached[1])  # type: ignore[return-value]
        raise
    with _lock:
        _memory[cache_key] = (now, deepcopy(value))
        _write(namespace, key, now, value)
    return value


def invalidate(namespace: str, key: str) -> None:
    """Remove one cache entry after a confirmed mutation."""
    with _lock:
        _memory.pop((namespace, key), None)
        try:
            _path(namespace, key).unlink()
        except FileNotFoundError:
            pass
