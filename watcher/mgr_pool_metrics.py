"""Pool usage and client I/O from the mgr prometheus module.

The Pools inventory section ran four Ceph commands through `cephadm shell`
on every 60 s poll, 8.8-12.7 s on CS-LAB (09/10/2026). Two of them are
configuration that only changes with the osdmap (pool ls detail, crush rule
dump); the other two are numbers the mgr already serves in ~10 ms:
ceph_pool_stored / max_avail / objects, and the ceph_pool_rd / wr operation
counters, turned into ops/s from the previous sample.

The payloads built here have the shape of `ceph df detail` and `ceph osd pool
stats`, so watcher.inventory_queries normalizes them unchanged.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

MGR_SERVICES_TTL_SECONDS = 600
FETCH_TIMEOUT_SECONDS = 3
_SAMPLE_RE = re.compile(r"^(ceph_pool_[a-z_]+)\{([^}]*)\}\s+([0-9.eE+-]+)$")
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')
_WANTED = {"ceph_pool_stored", "ceph_pool_max_avail", "ceph_pool_objects", "ceph_pool_rd", "ceph_pool_wr"}

_lock = threading.Lock()
_url: str | None = None
_url_read_at = 0.0
# pool name -> (monotonic time, rd counter, wr counter) of the previous sample
_previous: dict[str, tuple[float, float, float]] = {}


def parse_pools(text: str) -> dict[str, dict[str, float]]:
    """{pool name: {stored, max_avail, objects, rd, wr}} from a metrics page; {} if it has no pools."""
    names: dict[str, str] = {}
    values: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        match = _SAMPLE_RE.match(line)
        if not match:
            continue
        metric, raw_labels, raw_value = match.groups()
        labels = dict(_LABEL_RE.findall(raw_labels))
        pool_id = labels.get("pool_id")
        if pool_id is None:
            continue
        if metric == "ceph_pool_metadata" and labels.get("name"):
            names[pool_id] = labels["name"]
        elif metric in _WANTED:
            values.setdefault(pool_id, {})[metric.removeprefix("ceph_pool_")] = float(raw_value)
    return {names[pool_id]: sample for pool_id, sample in values.items() if pool_id in names}


def payloads(pools: dict[str, dict[str, float]], now: float) -> tuple[dict, dict]:
    """(`ceph df detail`-shaped, `ceph osd pool stats`-shaped) payloads; rates need a previous sample."""
    df_rows, io_rows = [], []
    with _lock:
        for name, sample in sorted(pools.items()):
            df_rows.append({"name": name, "stats": {
                "stored": int(sample.get("stored", 0)),
                "max_avail": int(sample.get("max_avail", 0)),
                "objects": int(sample.get("objects", 0)),
            }})
            rd, wr = sample.get("rd", 0.0), sample.get("wr", 0.0)
            before = _previous.get(name)
            _previous[name] = (now, rd, wr)
            if before is None or now <= before[0] or rd < before[1] or wr < before[2]:
                rates = {}  # first sample, or a counter reset (OSD restart): no rate yet
            else:
                elapsed = now - before[0]
                rates = {"read_op_per_sec": round((rd - before[1]) / elapsed, 1),
                         "write_op_per_sec": round((wr - before[2]) / elapsed, 1)}
            io_rows.append({"pool_name": name, "client_io_rate": rates})
    return {"pools": df_rows}, {"pool_stats": io_rows}


def _discover() -> str | None:
    from watcher import ceph_client

    try:
        _host, payload = ceph_client.run_ceph_json_command("ceph mgr services")
    except Exception as exc:
        logger.info("mgr pool metrics: ceph mgr services unavailable: %s", exc)
        return None
    url = payload.get("prometheus") if isinstance(payload, dict) else None
    return url.rstrip("/") + "/metrics" if isinstance(url, str) and url.startswith("http") else None


def fetch_pools(*, discover=_discover, fetch=None) -> dict[str, dict[str, float]] | None:
    """Pool samples from the active mgr, or None (no URL, error, or a standby's empty page)."""
    global _url, _url_read_at
    with _lock:
        if _url is None or time.monotonic() - _url_read_at > MGR_SERVICES_TTL_SECONDS:
            _url, _url_read_at = discover(), time.monotonic()
        url = _url
    if url is None:
        return None
    try:
        if fetch is not None:
            text = fetch(url)
        else:
            with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:  # nosec B310
                text = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.info("mgr pool metrics: %s unavailable: %s", url, exc)
        text = ""
    pools = parse_pools(text)
    if not pools:
        with _lock:
            _url = None  # look the active mgr up again next time
        return None
    return pools
