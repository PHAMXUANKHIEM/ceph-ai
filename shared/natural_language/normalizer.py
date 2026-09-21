"""Small deterministic normalizer for Vietnamese Ceph operator language."""

from __future__ import annotations

import re
import unicodedata


_WHITESPACE_RE = re.compile(r"\s+")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_OSD_RE = re.compile(r"\bosd(?:\s*(?:id|so|so\s*osd))?\s*[-:#]?\s*(\d+)\b")
_PG_RE = re.compile(r"\bpg(?:id)?\s*[-:#]?\s*([0-9a-f]+(?:\.[0-9a-f]+)?)\b", re.I)
_POOL_RE = re.compile(r"\bpool\s*[-:#]?\s*([a-zA-Z0-9][a-zA-Z0-9_.-]*)\b", re.I)
_VOLUME_RE = re.compile(
    r"\b(?:volume|rbd|image|cinder)\s*[-:#]?\s*([a-zA-Z0-9][a-zA-Z0-9_.:-]*)\b",
    re.I,
)
_BUCKET_RE = re.compile(
    r"\b(?:bucket|container)\s*[-:#]?\s*([a-zA-Z0-9][a-zA-Z0-9_.-]*)\b", re.I
)
_HOST_RE = re.compile(
    r"\b(?:node|host|may\s*chu|may\s*ch)\s*[-:#]?\s*([a-zA-Z0-9][a-zA-Z0-9_.-]*)\b",
    re.I,
)
_REQUEST_ID_RE = re.compile(
    r"\b(?:request\s*id|request_id|req(?:uest)?[-_ ]?id)\s*[-:#]?\s*([a-zA-Z0-9][a-zA-Z0-9_.:-]*)\b",
    re.I,
)
_PERCENT_GTE_RE = re.compile(
    r"(?:tren|hon|it nhat|>=|>|tu)\s*(\d{1,3})(?:\s*%)?"
)
_PERCENT_LTE_RE = re.compile(
    r"(?:duoi|it hon|thap hon|<=|<|below)\s*(\d{1,3})(?:\s*%)?"
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
    if any(char in text for char in "ăâđêôơưĂÂĐÊÔƠƯáàảãạắằẳẵặấầẩẫậéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ"):
        return "vi"
    vietnamese_markers = (
        " cụm ", " suc khoe ", " dung luong ", " kiem tra ", " dang ",
        " cho toi ", " xem ", " tai sao ", " loi ", " node ",
        " ngay ", " gio ", " phut ", " de xuat ", " khuyen nghi ",
        " giai thich ", " phan tich ", " giup toi ", " toi can ", " ho tro ",
        " nen lam gi ",
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
    else:
        pool_ids = tuple(match.group(1) for match in _POOL_RE.finditer(text))
        volume_ids = tuple(match.group(1) for match in _VOLUME_RE.finditer(text))
        bucket_ids = tuple(match.group(1) for match in _BUCKET_RE.finditer(text))
        host_ids = tuple(match.group(1) for match in _HOST_RE.finditer(folded))
        request_ids = tuple(match.group(1) for match in _REQUEST_ID_RE.finditer(text))
        if pool_ids:
            resource_type = "pool"
            resource_ids.extend(pool_ids)
        elif volume_ids:
            resource_type = "volume"
            resource_ids.extend(volume_ids)
        elif bucket_ids:
            resource_type = "bucket"
            resource_ids.extend(bucket_ids)
        elif host_ids:
            resource_type = "node"
            resource_ids.extend(host_ids)
        elif request_ids:
            resource_type = "request"
            resource_ids.extend(request_ids)

    percent_match = _PERCENT_GTE_RE.search(folded)
    if percent_match:
        filters["utilization_gte"] = int(percent_match.group(1))
    else:
        percent_lte_match = _PERCENT_LTE_RE.search(folded)
        if percent_lte_match:
            filters["utilization_lte"] = int(percent_lte_match.group(1))
    if not filters and any(term in folded for term in ("nearfull", "near full")):
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
