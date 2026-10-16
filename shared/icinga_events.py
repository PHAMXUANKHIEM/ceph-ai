"""Problems Icinga reports for Ceph clusters (plan icinga-alerting-handoff, I1; 16/10/2026).

Icinga detects and notifies; its NotificationCommand "ceph-ai-webhook" (deploy/icinga) posts one
event per state change of a `ceph-*` service to /api/icinga/events. During the parallel run
(I1-I3) Ceph AI's own Watcher still opens the incidents, so an event is recorded, matched to the
cluster (Icinga host name == Cluster.name) and linked to that cluster's open incidents whose Ceph
code appears in the event; the record is what the I3 comparison reads (which side saw a problem
first, which side missed one). Nothing here opens an incident or runs an action.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from config.settings import settings
from shared import db
from shared.models import Cluster, Incident, IncidentStatus
from shared.time import utc_now

NOTIFICATION_TYPES = frozenset({
    "PROBLEM", "RECOVERY", "ACKNOWLEDGEMENT", "DOWNTIMESTART", "DOWNTIMEEND", "DOWNTIMEREMOVED",
    "FLAPPINGSTART", "FLAPPINGEND", "CUSTOM",
})
STATES = frozenset({"OK", "WARNING", "CRITICAL", "UNKNOWN"})
_CODE_LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]{2,63}) \(")      # "OSD_DOWN (HEALTH_WARN): 1 osds down"
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{16,64}$")
_MAX_FILE_BYTES = 5 * 1024 * 1024
_RECENT_IDS_SCANNED = 2000
_CLOSED = (IncidentStatus.RESOLVED.value, IncidentStatus.REJECTED.value, IncidentStatus.FAILED.value,
           IncidentStatus.AUTO_FIXED.value)


class IcingaEvent(BaseModel):
    """What deploy/icinga/notify-ceph-ai.sh sends; every field bounded."""
    notification_type: str = Field(max_length=32)
    host: str = Field(min_length=1, max_length=128)
    service: str = Field(min_length=1, max_length=128)
    state: str = Field(max_length=16)
    state_type: str = Field(default="", max_length=8)
    output: str = Field(default="", max_length=4000)
    long_output: str = Field(default="", max_length=16000)
    last_state_change: str = Field(default="", max_length=32)
    author: str = Field(default="", max_length=128)
    comment: str = Field(default="", max_length=2000)
    event_id: str

    @field_validator("notification_type", "state", "state_type")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("event_id")
    @classmethod
    def _event_id(cls, value: str) -> str:
        if not _EVENT_ID_RE.match(value):
            raise ValueError("event_id must be 16-64 lowercase hex characters")
        return value

    def checked(self) -> "IcingaEvent":
        if self.notification_type not in NOTIFICATION_TYPES:
            raise ValueError(f"unknown notification_type {self.notification_type!r}")
        if self.state not in STATES:
            raise ValueError(f"unknown state {self.state!r}")
        return self


def health_codes(event: IcingaEvent) -> list[str]:
    """Ceph health check codes the plugin lists, one per long-output line."""
    codes = []
    for line in event.long_output.splitlines():
        match = _CODE_LINE_RE.match(line.strip())
        if match and match.group(1) not in codes:
            codes.append(match.group(1))
    return codes


def _path() -> Path:
    return Path(settings.icinga_events_file)


def _seen(event_id: str) -> bool:
    try:
        with _path().open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 400 * _RECENT_IDS_SCANNED))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return False
    return f'"event_id": "{event_id}"' in tail


def _append(record: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def record(event: IcingaEvent) -> dict[str, Any]:
    """Record one event; returns what the caller may show (no secrets)."""
    event = event.checked()
    if _seen(event.event_id):
        return {"accepted": True, "duplicate": True, "event_id": event.event_id}
    codes = health_codes(event)
    with db.SessionLocal() as session:
        cluster = session.query(Cluster).filter(Cluster.name == event.host).one_or_none()
        cluster_id = cluster.id if cluster is not None else None
        linked: list[dict[str, str]] = []
        if cluster_id is not None:
            scope = Incident.cluster_id == cluster_id
            if cluster.is_default:  # the default cluster's older incidents have no cluster_id
                scope = scope | Incident.cluster_id.is_(None)
            query = session.query(Incident.id, Incident.ceph_code, Incident.status).filter(
                scope, Incident.status.notin_(_CLOSED))
            for incident_id, code, status in query.all():
                if code and code.split(":", 1)[0] in codes:
                    linked.append({"incident_id": incident_id, "ceph_code": code, "status": status})
    result = {
        "received_at": utc_now().isoformat(timespec="seconds") + "Z",
        "event_id": event.event_id,
        "notification_type": event.notification_type,
        "host": event.host,
        "service": event.service,
        "state": event.state,
        "state_type": event.state_type,
        "output": event.output[:500],
        "health_codes": codes,
        "cluster_id": cluster_id,
        "linked_incidents": linked,
    }
    _append(result)
    return {"accepted": True, "duplicate": False, "event_id": event.event_id, "cluster_known": cluster_id is not None,
            "health_codes": codes, "linked_incidents": [item["incident_id"] for item in linked]}
