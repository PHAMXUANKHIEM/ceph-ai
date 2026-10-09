"""Which cluster is the Failure Lab staging cluster, and how it may be used.

Settings > "Cụm Staging (Failure Lab)" and Deploy Cluster write this file;
the fault runner and the Worker read it on every use. It lives next to the
live .env (/var/lib/ceph-ai/config) but is a separate JSON file on purpose:
containers copy .env into their environment only when they are created, so a
setting saved from the Dashboard would not reach a running Worker until the
next deploy. Missing keys fall back to the FAILURE_LAB_* settings.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings

CONFIG_PATH = Path(os.environ.get("CEPH_AI_FAILURE_LAB_CONFIG", "/var/lib/ceph-ai/config/failure-lab.json"))
_WINDOW_RE = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}$")
_FSID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CHAT_RE = re.compile(r"^-?\d{1,20}$")


class LabConfigError(ValueError):
    """Shown to the operator as is."""


@dataclass(frozen=True)
class LabConfig:
    cluster_id: str = ""
    fsid: str = ""
    fault_enabled: bool = False
    window: str = "02:00-05:00"
    telegram_chat_id: str = ""
    updated_by: str = ""
    updated_at: str = ""


def _stored(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def load(path: Path | None = None) -> LabConfig:
    stored = _stored(path or CONFIG_PATH)
    return LabConfig(
        cluster_id=str(stored.get("cluster_id") or ""),
        fsid=str(stored.get("fsid") or settings.failure_lab_cluster_fsid or "").strip(),
        fault_enabled=bool(stored["fault_enabled"]) if "fault_enabled" in stored else settings.failure_lab_fault_enabled,
        window=str(stored.get("window") or settings.failure_lab_window),
        telegram_chat_id=str(stored.get("telegram_chat_id") or settings.failure_lab_telegram_chat_id or "").strip(),
        updated_by=str(stored.get("updated_by") or ""), updated_at=str(stored.get("updated_at") or ""),
    )


def save(actor: str, *, path: Path | None = None, **changes: object) -> LabConfig:
    """Validate and write ``changes`` over the current file (atomic, 0640)."""
    path = path or CONFIG_PATH
    current = asdict(load(path))
    unknown = set(changes) - set(current)
    if unknown:
        raise LabConfigError(f"Trường không hợp lệ: {sorted(unknown)}")
    current.update(changes)
    if current["window"] and not _WINDOW_RE.match(str(current["window"])):
        raise LabConfigError("Khung giờ phải có dạng HH:MM-HH:MM.")
    if current["fsid"] and not _FSID_RE.match(str(current["fsid"])):
        raise LabConfigError("fsid không đúng định dạng UUID.")
    if current["telegram_chat_id"] and not _CHAT_RE.match(str(current["telegram_chat_id"])):
        raise LabConfigError("Chat id Telegram phải là số (nhóm có dấu - ở đầu).")
    if current["fault_enabled"] and not (current["cluster_id"] and current["fsid"]):
        raise LabConfigError("Chọn cụm staging và ghim fsid trước khi bật gây lỗi.")
    current.update(updated_by=actor, updated_at=datetime.now(timezone.utc).isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o640)
    temporary.replace(path)
    return load(path)
