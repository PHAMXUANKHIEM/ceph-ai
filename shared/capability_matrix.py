"""AI roadmap Pha 0.2 (Plan/ai-missing-features-roadmap.md) -- lookup and
CRUD/audit logic over `CapabilityMatrixEntry`. See that model's own
docstring for why this table is operator-maintained (never auto-populated
from a model's own guess at what upstream Ceph docs say) and why an empty
table is the correct, safe starting state.

`check_capability` is the function Pha 0.3's preflight validator (not yet
built) will call before ever letting a proposal reach the executor --
fail-closed by construction: no matching ACTIVE row means UNKNOWN, never a
silent SUPPORTED default.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from config.settings import settings
from shared import db
from shared.models import (
    CapabilityMatrixChange,
    CapabilityMatrixEntry,
    CapabilityMatrixEntryStatus,
    CapabilityStatus,
)


class CapabilityCheckResult:
    """Plain result object (not a DB row) so callers get a stable shape
    regardless of whether a matching entry exists."""

    def __init__(
        self,
        status: CapabilityStatus,
        entry: CapabilityMatrixEntry | None = None,
        is_stale: bool | None = None,
        reason: str | None = None,
    ):
        self.status = status
        self.entry = entry
        self.is_stale = is_stale
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid only
        return f"CapabilityCheckResult(status={self.status!r}, is_stale={self.is_stale!r}, reason={self.reason!r})"


def _entry_to_dict(entry: CapabilityMatrixEntry) -> dict:
    return {
        "id": entry.id,
        "command_id": entry.command_id,
        "inner_command": entry.inner_command,
        "flag": entry.flag,
        "module": entry.module,
        "backend": entry.backend,
        "min_major": entry.min_major,
        "max_major": entry.max_major,
        "doc_url": entry.doc_url,
        "verified_at": entry.verified_at.isoformat(),
        "verified_by": entry.verified_by,
        "status": entry.status,
        "notes": entry.notes,
    }


def check_capability(command_id: str, ceph_major: int | None, session=None) -> CapabilityCheckResult:
    """Fail-closed capability lookup (roadmap section 3.2).

    - `ceph_major is None` (version itself unknown, e.g. Pha 0.1's own
      inventory hasn't succeeded yet for this cluster) -> UNKNOWN. Never
      guess a version to check against.
    - No ACTIVE entry for `command_id` at all -> UNKNOWN. The matrix
      simply doesn't know about this command yet -- distinct from knowing
      it and rejecting it (next case).
    - ACTIVE entries exist for `command_id` but none covers `ceph_major`
      -> UNSUPPORTED_VERSION. The matrix has real, sourced knowledge that
      this command isn't documented as working on this version.
    - A covering ACTIVE entry exists -> SUPPORTED, with `is_stale=True` if
      `verified_at` is older than `settings.capability_matrix_max_age_days`
      (a stale-but-supported result is still returned -- Ceph docs don't
      change fast enough to warrant fail-closed on staleness alone, but
      callers/UI should surface the flag rather than hide it).
    """
    if ceph_major is None:
        return CapabilityCheckResult(
            CapabilityStatus.UNKNOWN, reason="Chưa xác định được phiên bản Ceph của cụm"
        )

    def _lookup(active_session) -> CapabilityCheckResult:
        entries = (
            active_session.query(CapabilityMatrixEntry)
            .filter(
                CapabilityMatrixEntry.command_id == command_id,
                CapabilityMatrixEntry.status == CapabilityMatrixEntryStatus.ACTIVE.value,
            )
            .all()
        )
        if not entries:
            return CapabilityCheckResult(
                CapabilityStatus.UNKNOWN,
                reason=f"Chưa có capability matrix entry nào cho command_id={command_id!r}",
            )
        matching = [
            e for e in entries
            if e.min_major <= ceph_major and (e.max_major is None or ceph_major <= e.max_major)
        ]
        if not matching:
            return CapabilityCheckResult(
                CapabilityStatus.UNSUPPORTED_VERSION,
                reason=(
                    f"command_id={command_id!r} không có entry hỗ trợ Ceph major {ceph_major} "
                    f"(các range đã biết: {[(e.min_major, e.max_major) for e in entries]})"
                ),
            )
        # Prefer the most specific (narrowest / most recently verified)
        # match if more than one range happens to cover this major --
        # shouldn't normally happen for a well-maintained matrix, but pick
        # deterministically rather than arbitrarily.
        entry = sorted(matching, key=lambda e: (e.max_major is None, -e.verified_at.timestamp()))[0]
        is_stale = (
            datetime.utcnow() - entry.verified_at
        ).days > settings.capability_matrix_max_age_days
        return CapabilityCheckResult(CapabilityStatus.SUPPORTED, entry=entry, is_stale=is_stale)

    if session is not None:
        return _lookup(session)
    with db.SessionLocal() as owned_session:
        return _lookup(owned_session)


def create_entry(
    *,
    command_id: str,
    inner_command: str,
    doc_url: str,
    verified_by: str,
    min_major: int,
    max_major: int | None = None,
    flag: str | None = None,
    module: str | None = None,
    backend: str | None = None,
    notes: str | None = None,
    verified_at: datetime | None = None,
) -> CapabilityMatrixEntry:
    """Adds a new ACTIVE entry + a CREATED audit row. Does NOT deprecate
    any existing overlapping entry for the same command_id -- an operator
    reviewing the admin page decides that explicitly via `deprecate_entry`
    (a silent auto-deprecate here could hide a real conflict the operator
    should see and resolve themselves)."""
    with db.SessionLocal() as session:
        entry = CapabilityMatrixEntry(
            command_id=command_id,
            inner_command=inner_command,
            doc_url=doc_url,
            verified_by=verified_by,
            verified_at=verified_at or datetime.utcnow(),
            min_major=min_major,
            max_major=max_major,
            flag=flag,
            module=module,
            backend=backend,
            notes=notes,
            status=CapabilityMatrixEntryStatus.ACTIVE.value,
        )
        session.add(entry)
        session.flush()
        session.add(
            CapabilityMatrixChange(
                entry_id=entry.id,
                actor=verified_by,
                change_type="CREATED",
                entry_snapshot_json=json.dumps(_entry_to_dict(entry)),
            )
        )
        session.commit()
        session.refresh(entry)
        session.expunge(entry)
        return entry


def deprecate_entry(entry_id: str, actor: str) -> CapabilityMatrixEntry | None:
    """Marks an entry DEPRECATED + writes a DEPRECATED audit row. Returns
    None if `entry_id` doesn't exist (caller's problem to report, not this
    function's)."""
    with db.SessionLocal() as session:
        entry = session.get(CapabilityMatrixEntry, entry_id)
        if entry is None:
            return None
        entry.status = CapabilityMatrixEntryStatus.DEPRECATED.value
        session.flush()
        session.add(
            CapabilityMatrixChange(
                entry_id=entry.id,
                actor=actor,
                change_type="DEPRECATED",
                entry_snapshot_json=json.dumps(_entry_to_dict(entry)),
            )
        )
        session.commit()
        session.refresh(entry)
        session.expunge(entry)
        return entry


def list_entries(include_deprecated: bool = False) -> list[CapabilityMatrixEntry]:
    with db.SessionLocal() as session:
        query = session.query(CapabilityMatrixEntry)
        if not include_deprecated:
            query = query.filter(CapabilityMatrixEntry.status == CapabilityMatrixEntryStatus.ACTIVE.value)
        rows = query.order_by(CapabilityMatrixEntry.command_id, CapabilityMatrixEntry.min_major).all()
        session.expunge_all()
        return rows


def list_changes(entry_id: str) -> list[CapabilityMatrixChange]:
    with db.SessionLocal() as session:
        rows = (
            session.query(CapabilityMatrixChange)
            .filter(CapabilityMatrixChange.entry_id == entry_id)
            .order_by(CapabilityMatrixChange.created_at.desc())
            .all()
        )
        session.expunge_all()
        return rows
