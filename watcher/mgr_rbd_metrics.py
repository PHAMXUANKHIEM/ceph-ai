"""Per-image RBD I/O from the mgr prometheus module, instead of `rbd perf image iostat`.

The volume monitor ran `rbd perf image iostat` once per RBD pool through
`cephadm shell`. Under the per-MON cephadm lock those calls kept losing and
opened the circuit on every MON, so volume metrics fell from ~110 polls a
day to 1-8 from 21/09/2026 and volume forecasting stopped. With
mgr/prometheus/rbd_stats_pools set (09/10/2026: volumes, images, vms,
everest-rbd; 31 images), the mgr serves per-image counters in ~10 ms:
ceph_rbd_{read,write}_ops and ceph_rbd_{read,write}_latency_{sum,count}
(latency in nanoseconds).

Rates and average latencies come from the difference with the previous
sample, so the first sample (or a counter reset) yields nothing rather than
a wrong number. Like `rbd perf image iostat`, an image without I/O since the
previous sample is left out.
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
# One page serves every pool of one volume poll.
PAGE_REUSE_SECONDS = 5.0
_SAMPLE_RE = re.compile(r"^ceph_rbd_([a-z_]+)\{([^}]*)\}\s+([0-9.eE+-]+)$")
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')
_COUNTERS = ("read_ops", "write_ops", "read_latency_sum", "read_latency_count",
             "write_latency_sum", "write_latency_count")

_lock = threading.Lock()
_url: str | None = None
_url_read_at = 0.0
_page: tuple[float, dict] | None = None
# (pool, image) -> (monotonic time, counters) of the previous sample
_previous: dict[tuple[str, str], tuple[float, dict[str, float]]] = {}


def parse_images(text: str) -> dict[tuple[str, str], dict[str, float]]:
    """{(pool, image): counters} from a metrics page; {} if it has no RBD samples."""
    images: dict[tuple[str, str], dict[str, float]] = {}
    for line in text.splitlines():
        match = _SAMPLE_RE.match(line)
        if not match or match.group(1) not in _COUNTERS:
            continue
        labels = dict(_LABEL_RE.findall(match.group(2)))
        if not labels.get("pool") or not labels.get("image"):
            continue
        key = (labels["pool"], labels["image"])
        images.setdefault(key, {})[match.group(1)] = float(match.group(3))
    return images


def _rates(before: tuple[float, dict[str, float]] | None, now: float, counters: dict[str, float]) -> dict | None:
    if before is None or now <= before[0]:
        return None
    earlier = before[1]
    delta = {name: counters.get(name, 0.0) - earlier.get(name, 0.0) for name in _COUNTERS}
    if any(value < 0 for value in delta.values()):
        return None  # counters reset (client reopened the image)
    ops = delta["read_ops"] + delta["write_ops"]
    if ops <= 0:
        return None  # no I/O since the previous sample: not listed, like rbd perf image iostat

    def latency_ms(kind: str) -> float:
        count = delta[f"{kind}_latency_count"]
        return round(delta[f"{kind}_latency_sum"] / count / 1_000_000, 3) if count > 0 else 0.0

    return {"iops": round(ops / (now - before[0]), 2),
            "read_latency_ms": latency_ms("read"), "write_latency_ms": latency_ms("write")}


def samples_for_pool(pool: str, images: dict[tuple[str, str], dict[str, float]], now: float) -> list[dict]:
    """VolumeIoSample-shaped dicts for one pool, updating the previous-sample memory."""
    samples = []
    with _lock:
        for (image_pool, image), counters in sorted(images.items()):
            if image_pool != pool:
                continue
            key = (image_pool, image)
            rates = _rates(_previous.get(key), now, counters)
            _previous[key] = (now, counters)
            if rates is not None:
                samples.append({"pool": pool, "image": image, **rates})
    return samples


def _discover() -> str | None:
    from watcher import ceph_client

    try:
        _host, payload = ceph_client.run_ceph_json_command("ceph mgr services")
    except Exception as exc:
        logger.info("mgr rbd metrics: ceph mgr services unavailable: %s", exc)
        return None
    url = payload.get("prometheus") if isinstance(payload, dict) else None
    return url.rstrip("/") + "/metrics" if isinstance(url, str) and url.startswith("http") else None


def _download(url: str) -> str:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:  # nosec B310 - URL from Ceph
        return response.read().decode("utf-8", errors="replace")


def fetch_images(*, discover=_discover, download=_download) -> dict[tuple[str, str], dict[str, float]] | None:
    """Per-image counters from the active mgr (one page per poll), or None when unavailable."""
    global _url, _url_read_at, _page
    now = time.monotonic()
    with _lock:
        if _page is not None and now - _page[0] < PAGE_REUSE_SECONDS:
            return _page[1]
        if _url is None or now - _url_read_at > MGR_SERVICES_TTL_SECONDS:
            _url, _url_read_at = discover(), now
        url = _url
    if url is None:
        return None
    try:
        images = parse_images(download(url))
    except Exception as exc:
        logger.info("mgr rbd metrics: %s unavailable: %s", url, exc)
        images = {}
    with _lock:
        if not images:
            _url = None  # error, standby page or rbd_stats_pools unset: rediscover next time
            return None
        _page = (now, images)
    return images
