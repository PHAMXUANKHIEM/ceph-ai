import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class IncidentStatus(str, enum.Enum):
    NEW = "NEW"
    DIAGNOSING = "DIAGNOSING"
    AUTO_FIXED = "AUTO_FIXED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "','".join(s.value for s in IncidentStatus) + "')",
            name="ck_incidents_status_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ceph_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=IncidentStatus.NEW.value)
    # Ceph's own per-check severity (HEALTH_WARN/HEALTH_ERR) from
    # `ceph health detail --format json`'s checks[code]["severity"] —
    # distinct from Incident.status (this codebase's own lifecycle state)
    # and from the cluster-wide status. Nullable: rows created before this
    # column existed have no value to backfill.
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    log_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ActionClassification(str, enum.Enum):
    SAFE = "SAFE"
    RISKY = "RISKY"


class ActionStatus(str, enum.Enum):
    PENDING = "PENDING"  # just classified (Story 3.1), not yet processed
    AUTO_EXECUTED = "AUTO_EXECUTED"  # Safe action executed (Story 3.2)
    PENDING_APPROVAL = "PENDING_APPROVAL"  # Risky action awaiting operator (Story 4.2)
    APPROVED = "APPROVED"  # operator approved (Story 4.3)
    REJECTED = "REJECTED"  # operator rejected (Story 4.3)
    EXECUTED = "EXECUTED"  # approved Risky action executed (Story 4.3)
    FAILED = "FAILED"  # execution failed (Story 3.2 / 4.3)


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (
        CheckConstraint(
            "classification IN ('" + "','".join(c.value for c in ActionClassification) + "')",
            name="ck_actions_classification_valid",
        ),
        CheckConstraint(
            "status IN ('" + "','".join(s.value for s in ActionStatus) + "')",
            name="ck_actions_status_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    incident_id: Mapped[str] = mapped_column(String(36), ForeignKey("incidents.id"), nullable=False)
    action_id: Mapped[str] = mapped_column(String(64), nullable=False)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ActionStatus.PENDING.value)
    # Not populated by this story — Story 3.2 builds the real Command object
    # per action_id and fills this in when it exists.
    proposed_command: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Story 4.2: Claude's own explanation of why action_id was chosen
    # (worker/llm/router_client.py's `rationale` field) — persisted so a
    # RISKY Action's approval screen (Story 4.2/4.3) can show it; SAFE
    # Actions get it too for consistency, though nothing displays it yet.
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Story 4.2: JSON-encoded list[str] of the Incident envelope's `nodes[]`
    # at classification time. The SAFE path (Story 3.2) still executes
    # immediately from the in-memory envelope and never reads this column —
    # it exists so the approved-RISKY path (Story 4.3), which runs from a
    # DB poll long after the originating RabbitMQ message and its envelope
    # are gone, still knows which host(s) to run the command on.
    target_nodes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 2026-07-23: JSON-encoded dict of the extra parameters a management
    # action_id needs beyond action_id/target_nodes (pool_name, pg_num,
    # size, osd_id — see worker/executor/commands.py's
    # _MANAGEMENT_COMMAND_BUILDERS). NULL for every pre-existing action_id
    # (restart_osd_daemon/resync_ntp/pg_repair_force), which are still fully
    # determined by action_id + host alone.
    action_params: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class SystemFlag(Base):
    """v1 (Story 3.2): minimal schema for AD-4's kill-switch — just enough
    for Worker to read `kill_switch_enabled` before every remediation
    command. Story 4.1 only needs to UPDATE the existing row from a
    Dashboard button; no new schema."""

    __tablename__ = "system_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class AuditEntry(Base):
    """Append-only (AD-7) — `shared/audit.py::record()` is the only place
    that ever inserts a row here, and nothing in this codebase ever
    updates/deletes one.

    `event_type`/`actor` are deliberately plain strings, NOT a
    CheckConstraint-backed enum like Incident.status/Action.status — those
    are safety-critical state machines that control system behavior;
    event_type/actor are purely descriptive history that shouldn't need a
    schema migration every time Epic 4 adds a new kind of event.
    """

    __tablename__ = "audit_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    incident_id: Mapped[str] = mapped_column(String(36), ForeignKey("incidents.id"), nullable=False)
    # Nullable — not every audit event is tied to a specific Action (e.g. a
    # future Incident-level event); Story 3.3's own writes always set it.
    action_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("actions.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class NodeDiagnosticRun(Base):
    """Audit trail for the Dashboard's read-only diagnostic CLI (Nodes page).

    Deliberately NOT an AuditEntry row: AuditEntry.incident_id is a required
    FK (every existing writer has a real Incident), but a diagnostic run is
    an ad-hoc operator query with no Incident behind it. Every row here is a
    WHITELISTED command only — command_id is the whitelist key, never
    free-form text (see watcher/ceph_client.py::DIAGNOSTIC_COMMANDS) — this
    table's own existence is not a place where arbitrary shell can leak in.
    """

    __tablename__ = "node_diagnostic_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    host: Mapped[str] = mapped_column(String(64), nullable=False)
    command_id: Mapped[str] = mapped_column(String(64), nullable=False)
    command_label: Mapped[str] = mapped_column(String(128), nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Truncated (see watcher/ceph_client.py) — this is an audit record of
    # what an operator saw, not a full log store.
    output_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class ChatMessage(Base):
    """One global chat log (Dashboard has a single static account — AD:
    no RBAC/multi-tenant concept anywhere else in this codebase either, e.g.
    the kill-switch and auto-approve flags are single global rows too), not
    a per-user table. `session_id` groups messages into conversations
    within that single log — "the current session" is just whichever
    session_id the most recently created row has (dashboard/routes/chat.py),
    no separate session table needed. Starting a new session
    (`POST /api/chat/sessions`) doesn't write anything by itself; it only
    hands the frontend a fresh id to tag its next message with — a session
    only "exists" once a message actually uses it.

    `role` is "user" or "assistant" — no CheckConstraint like
    Incident.status/Action.status: this is a chat transcript, not a
    safety-critical state machine.

    The `proposed_*` columns hold AT MOST one pending remediation proposal
    per assistant message (dashboard/chat_client.py's `propose_action` tool
    is only ever called once per turn — see its docstring). They stay NULL
    for every plain-answer assistant message and for every user message.
    Deliberately NOT a free-text command or shell string anywhere in this
    row — `proposed_action_id` is always one of
    worker/llm/router_client.py's VALID_ACTION_IDS (same closed enum AD-5
    already requires for the Incident-triggered path), enforced again in
    dashboard/routes/chat.py before executing the confirm.
    """

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Nullable in the schema (not NOT NULL) purely so rows created before
    # this column existed don't need a schema-level backfill guarantee — the
    # migration DOES backfill them (one shared id, grouping all pre-existing
    # history into a single legacy session) and every application code path
    # that inserts a ChatMessage always sets this; NULL should not occur for
    # any row created going forward.
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Who sent it (a "user" row) — None for "assistant" rows, same nullable
    # convention as AuditEntry.action_id for "not applicable to this row".
    actor: Mapped[str | None] = mapped_column(String(32), nullable=True)

    proposed_action_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # JSON-encoded list[str] — same encoding as Action.target_nodes.
    proposed_target_nodes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 2026-07-23: JSON-encoded dict — same encoding/purpose as
    # Action.action_params, carried on the chat proposal until confirm-action
    # copies it onto the real Action row. NULL for every non-management
    # proposal (unchanged behavior) and for every row created before this
    # column existed.
    proposed_action_params: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Best-effort preview of the command this would run, resolved the same
    # way Action.proposed_command is (worker/executor/commands.py::get_command)
    # — shown to the operator BEFORE they confirm, not just after (Story
    # 4.2's RISKY approval screen only shows it after PENDING_APPROVAL; this
    # shows it a step earlier, at proposal time, for both classifications).
    proposed_command_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NULL = no proposal on this message. "PENDING" = proposed, not yet
    # confirmed. "CONFIRMED" = operator clicked Execute — an Incident/Action
    # row now exists (dashboard/routes/chat.py) and the normal SAFE/RISKY
    # pipeline (kill-switch, approval, audit) owns it from here on; this
    # column is not updated again after that.
    proposed_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    proposed_incident_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("incidents.id"), nullable=True
    )

    # JSON-encoded list[str] of tool names dashboard/chat_client.py's
    # run_chat_turn() actually invoked (successfully) while producing THIS
    # assistant message — e.g. ["get_pool_list", "get_df"]. NULL for "no
    # tools were called" and for every "user" row. Powers the frontend's
    # "🔧 Đã truy vấn: ..." badge — persisted (not just returned from the
    # live API response) so it still renders after a page reload, same as
    # every other field on this row.
    tools_used: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class WatcherHeartbeat(Base):
    """Singleton row (Story 5.2) — `id` is always `1`, upserted by
    `shared/heartbeat.py::record()` after EVERY Watcher poll cycle (success
    or failure), not just when cluster health transitions. Unlike
    `AuditEntry` (append-only history), this table only ever answers "what
    happened on the LAST poll" — no history is kept here, so it can't grow
    unbounded even though polls happen every `watcher_poll_interval_seconds`
    (default 15s).
    """

    __tablename__ = "watcher_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Nullable — no MON node answered when success=False.
    mon_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    polled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
