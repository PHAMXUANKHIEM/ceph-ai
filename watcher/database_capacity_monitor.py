"""Database capacity monitoring (2026-08-10) — watches the SIZE of
ceph-aiops's OWN operational database (`shared/db.py`'s `settings.database_url`
— Incident/Action/AuditEntry/BackupJob/ChatMessage/TestRunResult history,
NOT anything about the Ceph cluster itself) and alerts if it grows past a
threshold, same role as `watcher/node_health_monitor.py` (a resource
running out, not a Ceph health check) but for this app's own storage
instead of a cluster node's.

Deliberately scoped to "database's own size vs. an absolute threshold"
only — NOT "disk free space on the volume backing it". In this
deployment the DB is a Postgres cluster managed by OpenEverest (Percona
Everest) on Kubernetes, on a separate host from ceph-aiops (same network
segment, reachable over the existing `database_url` connection, but NOT
SSH-reachable the way `watcher/ceph_client.py` reaches Ceph cluster
nodes) with no Prometheus/Everest-API metrics endpoint currently
available to this app — there is no portable way from plain SQL to learn
how much free space is left on the underlying PVC. `pg_database_size()`
(Postgres) is the one thing reliably knowable over the connection the app
already has, so v1 alerts on that alone. If an Everest metrics/API
endpoint becomes available later, add a real disk-free signal as a
SEPARATE check here (own ceph_code, own streak) rather than replacing
this one, same "2 independent signals" reasoning
`watcher/crush_skew_monitor.py` already documents for CRUSH_SKEW_USE/PG.

Supports BOTH backends `shared/db.py::make_engine` supports: for a
`sqlite://` URL, measures the DB file's real size on disk directly (no
SQL needed — the file lives on the SAME host as this Watcher process);
for anything else (Postgres, including the OpenEverest cluster above),
runs `SELECT pg_database_size(current_database())` over the EXISTING
`shared.db.SessionLocal` connection — no new connection/credentials, no
new network path. This is the first raw-SQL (non-ORM) query anywhere in
this codebase; justified because `pg_database_size()` is a Postgres
server-side function with no ORM/model equivalent to query instead.

Same "own in-memory streak, no DB persistence of raw samples" posture as
watcher/node_health_monitor.py/watcher/osd_latency_monitor.py — a single
consecutive-scans-over-threshold counter is all this needs (there is only
ever ONE database to watch, unlike those two modules' per-host/per-osd
dicts).
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from shared import alert_lifecycle, audit, db
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from shared.incident_actions import cancel_pending_actions
from shared.telegram_alerts import send_database_size_alert
from worker.policy import gate

DATABASE_SIZE_HIGH_PREFIX = "DATABASE_SIZE_HIGH"
# No automated remediation exists for "the database is getting large" (could
# be legitimate growth, a table that needs archiving, or a real leak — an
# operator has to look) — same "no Command, operator investigates" posture
# as every other synthetic ceph_code family in this codebase.
DATABASE_SIZE_ACTION_ID = "investigate_manually"

# Internal tuning constants, not config/settings.py fields — same
# "operational tuning, not per-deployment config" convention as every
# other watcher/*_monitor.py module's own thresholds.
#
# 5 GiB is a starting guess, not measured against this deployment's real
# growth curve (no historical size data was available at implementation
# time) — same "dev proposes a concrete number, revisit if wrong"
# precedent as watcher/crush_skew_monitor.py's SKEW_RATIO_THRESHOLD.
DATABASE_SIZE_THRESHOLD_BYTES = 5 * 1024**3
# Lower than node_health_monitor.py's CPU/RAM CONSECUTIVE_SCANS_REQUIRED=2
# would need to be for a noisy metric -- kept at 2 anyway purely for
# consistency with every other monitor's "not on a single scan" posture,
# even though DB size does not spike/fluctuate the way CPU/RAM or OSD
# latency can.
CONSECUTIVE_SCANS_REQUIRED = 2

# Mirrors watcher/main.py::_RECOVERABLE_STATUSES / the identical copies in
# every other watcher/*_monitor.py module — kept as its own copy rather
# than a cross-import, same "independent modules" reasoning all of them
# already document.
_RECOVERABLE_STATUSES = {
    IncidentStatus.NEW.value,
    IncidentStatus.DIAGNOSING.value,
    IncidentStatus.PENDING_APPROVAL.value,
    IncidentStatus.APPROVED.value,
    IncidentStatus.EXECUTING.value,
    # 2026-08-20: lệnh đã chạy nhưng CHƯA xác minh là hết lỗi —
    # vẫn là một sự cố đang mở. Thiếu dòng này, mọi chỗ dùng tập
    # trạng thái này để chống trùng sẽ tưởng Incident đã đóng và
    # tạo thêm một Incident nữa cho cùng vấn đề.
    IncidentStatus.VERIFYING.value,
    IncidentStatus.FAILED.value,
}

# Module-level, process-lifetime state — there is only ever one database to
# watch, so this is a single counter, not a dict keyed by entity like
# node_health_monitor.py's/osd_latency_monitor.py's own streaks.
_consecutive_high_scans = 0


def _sqlite_file_path(database_url: str) -> str | None:
    """Extracts the on-disk path from a `sqlite:///...` URL. Returns `None`
    for `sqlite:///:memory:` (nothing to measure) or a malformed URL."""
    marker = "sqlite:///"
    if not database_url.startswith(marker):
        return None
    path = database_url[len(marker):]
    if not path or path == ":memory:":
        return None
    return path


def get_database_size_bytes() -> int | None:
    """Returns the database's current size in bytes, or `None` if it
    couldn't be determined (missing/unreadable SQLite file, or a query
    failure against a SQL backend) — never raises, same best-effort
    posture as every other Watcher scan module."""
    url = settings.database_url
    if url.startswith("sqlite"):
        path = _sqlite_file_path(url)
        if path is None:
            return None
        try:
            return os.path.getsize(path)
        except OSError:
            return None

    try:
        with db.SessionLocal() as session:
            size = session.execute(text("SELECT pg_database_size(current_database())")).scalar()
    except SQLAlchemyError:
        return None
    return int(size) if isinstance(size, (int, float)) else None


def check_database_size(
    still_over_threshold: set[str] | None = None,
) -> dict[str, dict]:
    """Returns `{ceph_code: detail}` (at most one entry — there is only
    one database) if the size has stayed at/above
    `DATABASE_SIZE_THRESHOLD_BYTES` for `CONSECUTIVE_SCANS_REQUIRED` scans
    in a row. No-op (returns `{}`, streak untouched) if the size can't be
    determined this scan — a transient measurement gap must not fake a
    reset any more than it should fake progress toward the threshold."""
    global _consecutive_high_scans

    size_bytes = get_database_size_bytes()
    if size_bytes is None:
        return {}

    if size_bytes >= DATABASE_SIZE_THRESHOLD_BYTES:
        _consecutive_high_scans += 1
        # 2026-08-20: độc lập với streak — xem
        # create_or_resolve_database_size_incident để biết vì sao "đang
        # vượt ngưỡng ngay bây giờ" phải tách khỏi "đã vượt đủ lâu".
        if still_over_threshold is not None:
            still_over_threshold.add(DATABASE_SIZE_HIGH_PREFIX)
    else:
        _consecutive_high_scans = 0

    if _consecutive_high_scans < CONSECUTIVE_SCANS_REQUIRED:
        return {}

    return {
        DATABASE_SIZE_HIGH_PREFIX: {
            "size_bytes": size_bytes,
            "threshold_bytes": DATABASE_SIZE_THRESHOLD_BYTES,
            "consecutive_scans": _consecutive_high_scans,
        }
    }


def _rationale_for(detail: dict) -> str:
    size_gb = detail["size_bytes"] / 1024**3
    threshold_gb = detail["threshold_bytes"] / 1024**3
    return (
        f"Database ceph-aiops đã đạt {size_gb:.2f} GB, vượt ngưỡng {threshold_gb:.2f} GB, "
        f"lặp lại {detail['consecutive_scans']} lần quét liên tiếp — có thể do dữ liệu lịch sử "
        f"(Incident/Action/Backup/Chat...) tăng trưởng bình thường theo thời gian, hoặc một bảng "
        f"cụ thể đang phình bất thường, cần vận hành viên kiểm tra trực tiếp."
    )


def create_or_resolve_database_size_incident(
    current: dict[str, dict],
    still_over_threshold: set[str] | None = None,
) -> None:
    """Same shape as watcher/osd_latency_monitor.py::
    create_or_resolve_osd_latency_incidents, simplified for a singleton
    entity (there is only ever one `DATABASE_SIZE_HIGH` ceph_code, never a
    dynamic per-entity suffix) — creates a PENDING_APPROVAL Incident+Action
    (investigate_manually) the first time the size is flagged, and
    resolves it once the size (or absence of a `current` entry — a
    measurement gap counts as "we don't know it's still high", same
    posture as every other family's "no data this scan" handling) drops
    it out of `current`. Sends a Telegram alert only for a NEWLY created
    Incident.

    `still_over_threshold` (2026-08-20): `_consecutive_high_scans` là biến
    module, chỉ sống trong RAM, nên sau mỗi lần Watcher restart nó về 0 và
    database vẫn to y như cũ lại vắng mặt khỏi `current` trong
    `CONSECUTIVE_SCANS_REQUIRED` lần quét đầu — đóng nhầm Incident rồi tạo
    lại cái mới ngay sau đó. Cùng một lỗi, cùng một cách vá như
    watcher/crush_skew_monitor.py (xem docstring hàm tương ứng ở đó, kèm số
    liệu đo được). Mặc định None giữ nguyên hành vi cũ."""
    with db.SessionLocal() as session:
        open_incident = (
            session.query(Incident)
            .filter(Incident.ceph_code == DATABASE_SIZE_HIGH_PREFIX)
            .filter(Incident.status.in_(_RECOVERABLE_STATUSES))
            .first()
        )

        still_over = still_over_threshold or set()
        if (
            open_incident is not None
            and DATABASE_SIZE_HIGH_PREFIX not in current
            and DATABASE_SIZE_HIGH_PREFIX not in still_over
        ):
            open_incident.status = IncidentStatus.RESOLVED.value
            cancel_pending_actions(session, open_incident.id)

        if DATABASE_SIZE_HIGH_PREFIX in current and open_incident is None:
            detail = current[DATABASE_SIZE_HIGH_PREFIX]
            rationale = _rationale_for(detail)
            incident = Incident(
                ceph_code=DATABASE_SIZE_HIGH_PREFIX,
                status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(),
                log_excerpt=rationale,
            )
            session.add(incident)
            session.flush()  # assigns incident.id, needed by the Action FK below

            action = Action(
                incident_id=incident.id,
                action_id=DATABASE_SIZE_ACTION_ID,
                classification=gate.classify_action(DATABASE_SIZE_ACTION_ID).value,
                status=ActionStatus.PENDING_APPROVAL.value,
                rationale=rationale,
                # No automated target -- investigate_manually has no
                # Command regardless (has_command() is False for it, same
                # as every other synthetic-Incident family's identical
                # comment).
                target_nodes=json.dumps([]),
                action_params=json.dumps(
                    {"size_bytes": detail["size_bytes"], "threshold_bytes": detail["threshold_bytes"]}
                ),
            )
            session.add(action)
            session.flush()

            audit.record(
                session,
                incident_id=incident.id,
                action_id=action.id,
                event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
                actor=audit.ACTOR_SYSTEM,
            )

            if not alert_lifecycle.inherit_active_mute(session, incident):
                send_database_size_alert(rationale)
        session.commit()
