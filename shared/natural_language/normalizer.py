"""Small deterministic normalizer for Vietnamese Ceph operator language."""

from __future__ import annotations

import re
import unicodedata


_WHITESPACE_RE = re.compile(r"\s+")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_OSD_RE = re.compile(r"\bosd(?:\s*(?:id|so|so\s*osd))?\s*[-:#]?\s*(\d+)\b")
_PG_RE = re.compile(r"\bpg(?:id)?\s*[-:#]?\s*([0-9a-f]+(?:\.[0-9a-f]+)?)\b", re.I)
_PERCENT_RE = re.compile(
    r"(?:trên|hon|hơn|>=|>|ít nhất|tu|từ)\s*(\d{1,3})(?:\s*%)?"
)
_DURATION_RE = re.compile(
    r"\b(\d+)\s*(giây|giay|s|phút|phut|m|giờ|gio|h|ngày|ngay|d)\b"
)


def fold_text(text: str) -> str:
    """Return lowercase, accent-insensitive text for matching only."""

    text = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFD", text.lower())
    without_marks = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return _WHITESPACE_RE.sub(" ", without_marks).strip()


def detect_language(text: str) -> str:
    folded = fold_text(text)
    vietnamese_markers = (
        " cụm ", " suc khoe ", " dung luong ", " kiem tra ", " dang ",
        " cho toi ", " xem ", " tai sao ", " loi ", " node ",
    )
    if any(marker in f" {folded} " for marker in vietnamese_markers):
        return "vi"
    return "en" if folded else "unknown"


def extract_entities(text: str) -> tuple[str, tuple[str, ...], dict[str, object]]:
    """Extract safe, non-executing entities from the request."""

    folded = fold_text(text)
    resource_ids: list[str] = []
    resource_type: str | None = None
    filters: dict[str, object] = {}

    osd_ids = tuple(f"osd.{value}" for value in _OSD_RE.findall(folded))
    pg_ids = tuple(_PG_RE.findall(folded))
    ips = tuple(_IP_RE.findall(text))
    if osd_ids:
        resource_type = "osd"
        resource_ids.extend(osd_ids)
    elif pg_ids:
        resource_type = "pg"
        resource_ids.extend(pg_ids)
    elif ips:
        resource_type = "node"
        resource_ids.extend(ips)

    percent_match = _PERCENT_RE.search(folded)
    if percent_match:
        filters["utilization_gte"] = int(percent_match.group(1))
    elif any(term in folded for term in ("gan day", "gan day", "nearfull", "near full")):
        filters["utilization_gte"] = 80

    return resource_type, tuple(resource_ids), filters


def extract_time_range(text: str) -> tuple[int | None, str | None]:
    folded = fold_text(text)
    if "hom qua" in folded:
        return 24 * 60 * 60, "hôm qua"
    if "hom nay" in folded:
        return 24 * 60 * 60, "hôm nay"
    match = _DURATION_RE.search(folded)
    if not match:
        return None, None
    amount = int(match.group(1))
    unit = match.group(2)
    multiplier = {
        "giay": 1, "giây": 1, "s": 1,
        "phut": 60, "phút": 60, "m": 60,
        "gio": 3600, "giờ": 3600, "h": 3600,
        "ngay": 86400, "ngày": 86400, "d": 86400,
    }[unit]
    return amount * multiplier, match.group(0)

