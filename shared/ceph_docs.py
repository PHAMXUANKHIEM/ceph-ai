"""Official Ceph documentation for a health code (FL6.2).

The AI that proposes a lab reproduction must work from the official text,
not from memory, and cite it. This fetches the health-checks reference once
(cached on disk for a week) and returns the section of one code: Sphinx puts
each check in ``<section id="osd-down">`` (113 sections on 09/10/2026).
"""

from __future__ import annotations

import html
import logging
import re
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

HEALTH_CHECKS_URL = "https://docs.ceph.com/en/latest/rados/operations/health-checks/"
CACHE_PATH = Path("/var/lib/ceph-ai/failure-lab/ceph-docs-health-checks.html")
CACHE_SECONDS = 7 * 86400
FETCH_TIMEOUT_SECONDS = 20
SECTION_CHARS = 2500


def _anchor(code: str) -> str:
    return code.strip().lower().replace("_", "-")


def health_checks_page(*, cache_path: Path = CACHE_PATH, download=None) -> str:
    """The reference page, from the cache while it is fresh; '' if it cannot be had."""
    try:
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime < CACHE_SECONDS:
            return cache_path.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        if download is not None:
            text = download(HEALTH_CHECKS_URL)
        else:
            with urllib.request.urlopen(HEALTH_CHECKS_URL, timeout=FETCH_TIMEOUT_SECONDS) as response:  # nosec B310
                text = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("ceph docs: %s unavailable: %s", HEALTH_CHECKS_URL, exc)
        return cache_path.read_text(encoding="utf-8") if cache_path.exists() else ""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return text


def section(page: str, code: str) -> str:
    """Plain text of one health check's section ('' if the page does not document it)."""
    match = re.search(rf'<section id="{re.escape(_anchor(code))}">(.*?)</section>', page, re.S)
    if not match:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
    return re.sub(r"\s+", " ", text).strip()[:SECTION_CHARS]


def documented_codes(page: str) -> list[str]:
    """Every health code the page has a section for, upper-case."""
    return sorted({anchor.upper().replace("-", "_")
                   for anchor in re.findall(r'<section id="([a-z0-9-]+)">', page)
                   if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", anchor)})


def citations_for(page: str, prefixes: tuple[str, ...], limit: int = 4) -> list[dict]:
    """[{code, url, text}] for documented codes starting with one of ``prefixes``."""
    found = []
    for code in documented_codes(page):
        if any(code.startswith(prefix.rstrip(":")) for prefix in prefixes):
            text = section(page, code)
            if text:
                found.append({"code": code, "url": f"{HEALTH_CHECKS_URL}#{_anchor(code)}", "text": text})
        if len(found) >= limit:
            break
    return found
