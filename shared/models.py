import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class Cluster(Base):
    """A Ceph cluster Watcher can poll for health status, and (as of the
    Phase 2 multi-tenant remediation work) that Worker can diagnose/execute
    remediation for. Holds every field `shared/cluster_nodes.py::
    configured_nodes()`/`resolve_ssh_creds()` need to fully stand in for
    `config/settings.py`'s Settings singleton on a per-cluster basis: mon/
    mgr/osd/rgw nodes, exec mode, container names, SSH creds. Still does NOT
    carry RBD pools, backup targets, patch/upgrade config, or per-cluster
    Telegram routing — those remain `.env`-scoped/single-cluster until their
    own later phase (see docs/multi-cluster-deployment.md's sibling plan).

    Exactly one row has `is_default=True` — that row MIRRORS the
    `.env`-configured cluster (shared/clusters.py::ensure_default_cluster
    seeds it at startup) and is NOT editable via the Cluster CRUD UI; the
    `.env`/Settings-page "Kết nối cụm Ceph" form stays its one source of
    truth, unchanged. Every OTHER row is an additional cluster added purely
    for observation, editable via dashboard/routes/clusters.py — Worker
    never acts on these (worker/main.py's cluster-scope guard skips any
    Incident whose cluster_id isn't the default cluster's).
    """

    __tablename__ = "clusters"
    __table_args__ = (
        # Partial unique index — at most one is_default=True row, ever.
        # Declared here (not just in the Alembic migration) so it also
        # exists on every `Base.metadata.create_all()`-built test DB, not
        # only a real `alembic upgrade head`-migrated one — the migration's
        # own `op.create_index(...)` call is invisible to SQLAlchemy's ORM
        # metadata, and shared/clusters.py::ensure_default_cluster's
        # race-safety depends on this constraint actually existing wherever
        # the app runs, tests included.
        Index(
            "uq_clusters_single_default",
            "is_default",
            unique=True,
            sqlite_where=text("is_default"),
            postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    ceph_mon_nodes: Mapped[str] = mapped_column(Text, nullable=False)
    ceph_container_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # 2026-08-10 (multi-tenant remediation Phase 1) -- same optional-CSV
    # posture as config/settings.py's equivalent fields: blank means "no
    # nodes with this role for this cluster", not an error.
    ceph_mgr_nodes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ceph_osd_nodes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ceph_rgw_nodes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ceph_rgw_container_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # watcher/collector.py's log-collection needs these two: MON hostnames
    # (parses a mon NAME out of `ceph health detail` text, then maps it back
    # to an IP via this list — same positional pairing with ceph_mon_nodes
    # settings.ceph_mon_hostnames already uses) and the OSD daemon's own
    # container name (separate from ceph_container_name, which is MON's).
    ceph_mon_hostnames: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ceph_osd_container_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    ssh_user: Mapped[str] = mapped_column(String(64), nullable=False)
    ssh_key_path: Mapped[str] = mapped_column(Text, nullable=False)
    ceph_exec_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="docker")
    # 2026-08-10 (multi-tenant remediation Phase 2) -- ONE Telegram channel
    # per ADDITIONAL cluster (not the default cluster's own 3 categories):
    # a non-default cluster can only ever produce "Lỗi cụm" health-check
    # Incidents/RISKY approvals today (node/osd-latency/crush-skew monitors
    # and the backup pipeline don't run for observed clusters yet — see
    # watcher/main.py::run_observed_cluster_loop's own docstring), so a
    # 3-category split here would be dead configuration. Blank/disabled
    # means "no channel of its own" — dashboard/telegram_approval_bot.py::
    # channels_for_incident() then falls back to the 3 GLOBAL channels for
    # the default cluster only; a configured non-default cluster's own
    # channel REPLACES (narrows to) the global ones for its own Incidents,
    # never adds to them (see that function's own docstring for why).
    telegram_bot_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    telegram_chat_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    telegram_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


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
    # Nullable, no backfill (multi-cluster observability Phase 1) — every
    # row from before this column existed means "the default cluster",
    # same as every row a single-cluster deployment ever inserts; read
    # paths treat NULL as the default cluster via COALESCE, not a real
    # unknown-cluster state. See shared/models.py::Cluster's docstring.
    cluster_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("clusters.id"), nullable=True)
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
    # 2026-07-24: JSON-encoded list[{"host": str, "status": "pending"|
    # "running"|"done"|"failed"}], written by
    # worker/llm/router_client.py::_execute_approved_action as it works
    # through target_nodes one host at a time. A package-based cluster
    # upgrade has no orchestrator to ask "how far along is this" (unlike
    # cephadm's `ceph orch upgrade status`) and a single host's install can
    # run for minutes — without this, the Upgrade page had nothing to show
    # between "Đã duyệt" and the final EXECUTED/FAILED result. NULL for
    # every action_id that doesn't opt into writing it.
    execution_progress: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 2026-08-05, reworked 2026-08-06 for 3 independent Telegram channels
    # (dashboard/telegram_approval_bot.py) — a Duyệt/Từ chối request for
    # THIS Action is now BROADCAST to every configured channel (Backup/Lỗi
    # cụm/Phần cứng) at once, so a single Integer message id no longer
    # suffices. telegram_message_ids is a JSON-encoded dict
    # {channel_key: message_id} (channel_key in "backup"/"incident"/"node"),
    # one entry per channel this Action's Duyệt/Từ chối message was actually
    # sent to — needed later to edit that channel's own copy (remove the
    # buttons, show the outcome) once approved/rejected from EITHER
    # Telegram or the Dashboard. Same JSON-encoded-Text convention as
    # target_nodes/action_params/execution_progress above. NULL/empty dict
    # for every Action created before this feature/while no channel is
    # configured — same "opt-in column, harmless when unused" posture as
    # every other *_progress/*_at column on this model. telegram_notified_at
    # marks the last time a broadcast attempt ran for this Action (a
    # channel added/fixed later still gets picked up on the next scan —
    # see that module's own docstring).
    telegram_message_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


class UpgradeProcedureDocument(Base):
    """Singleton (id always 1, upserted) — the operator's own upgrade
    runbook for THIS cluster, uploaded via the Upgrade Cluster page
    (dashboard/routes/upgrade.py). Re-uploading replaces the previous row
    entirely; there is no history of past uploads kept here (this is a
    live reference document, not an audit trail — the upload/re-summarize
    events themselves are NOT written to AuditEntry either, since nothing
    here executes or changes cluster state).

    `summary_text`/`summary_error` are mutually exclusive in practice (only
    one is ever non-NULL after an upload or a re-summarize attempt) — kept
    as two separate nullable columns rather than one "status" enum because
    this is descriptive state, not a safety-critical machine like
    Incident.status/Action.status.
    """

    __tablename__ = "upgrade_procedure_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(32), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class WatcherHeartbeat(Base):
    """One row per cluster (Story 5.2, extended for multi-cluster
    observability Phase 1) — `id` was originally always `1` (a true
    singleton, single-cluster only); now `cluster_id` is the effective
    upsert key (`shared/heartbeat.py::record()`/`get_latest()` look up by
    `cluster_id`, not by the old fixed id), so each cluster gets its own
    "what happened on the LAST poll" row. `id` stays a plain autoincrement
    PK rather than becoming `cluster_id` itself — avoids an ALTER-COLUMN-TYPE
    migration on a table SQLite can't cheaply retype in place. The original
    id=1 row (from before this column existed) keeps `cluster_id` NULL
    forever — harmless: the default cluster's watcher loop writes a fresh,
    real row (with `cluster_id` set) within one poll interval of startup,
    so nothing ever reads that stale row again. Unlike `AuditEntry`
    (append-only history), this table only ever answers "what happened on
    the last poll for this cluster" — no history kept, so it can't grow
    unbounded even though polls happen every `watcher_poll_interval_seconds`
    (default 15s) per cluster.
    """

    __tablename__ = "watcher_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("clusters.id"), nullable=True, unique=True
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Nullable — no MON node answered when success=False.
    mon_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    polled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class User(Base):
    """Additional login accounts an admin creates via the Settings page's
    "Người dùng" card — separate from the single `.env`-configured account
    (config/settings.py's dashboard_username/dashboard_password_hash), which
    stays the always-available root admin and never gets a row here (see
    dashboard/routes/auth.py::is_admin_user/_check_password).

    is_active is a soft-disable (AD-consistent with the rest of this
    codebase preferring reversible state over deletion, e.g. Incident/Action
    status transitions) — a deactivated user simply fails login like a wrong
    password, never hard-deleted."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class PatchDocument(Base):
    """Singleton (id always 1, upserted) — the operator's currently-staged
    Ceph source patch, uploaded via the "Vá lỗi Ceph" page
    (dashboard/routes/patch.py). Re-uploading replaces the previous row
    entirely, same posture as UpgradeProcedureDocument above — this is a
    scratch staging area, not a history of past patches. `content` feeds
    directly into the patch_build_and_stage Action's action_params (see
    worker/executor/commands.py::_patch_build_and_stage_command) at propose
    time, so its lifetime as a DB row is really just "between upload and
    the next successful propose", though nothing here deletes it
    automatically — re-uploading is the only way it changes."""

    __tablename__ = "patch_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class VolumeMetric(Base):
    """One row per (pool, image) per Watcher poll — the persisted,
    queryable counterpart to watcher/volume_monitor.py's in-memory-only
    rolling window (that module's own docstring explains why THAT state
    stays in-memory: it only needs a few minutes of history to drive the
    saturation heuristic). This table exists for the opposite need —
    keeping every sample so an operator can actually see a volume's history
    (recent peak IOPS, when latency started climbing, etc.) instead of only
    whatever the live iostat tab happens to show right now.

    Written by watcher/volume_monitor.py::persist_last_poll_metrics(),
    called once per Watcher poll for EVERY sample from EVERY auto-discovered/
    configured pool that poll returned — not only volumes that looked
    saturated. `saturated` here mirrors the in-memory streak state
    (consecutive_saturated_polls >= CONSECUTIVE_POLLS_REQUIRED) at the
    moment of this exact sample, which is a close but not always
    literally-identical proxy for "has an OPEN VOLUME_SATURATED: Incident
    right now" (dashboard/routes/volumes.py's iostat API cross-references
    the Incident table directly for that, rather than trusting this column,
    to stay authoritative) — recording the streak-based flag here avoids an
    extra DB round trip inside the per-poll SSH-query hot path.

    Append-only, no automatic pruning (this codebase has no background
    cleanup job for ANY table) — purged the same manual way as every other
    timestamped table, via the Settings page's "Xóa dữ liệu cũ" form
    (dashboard/routes/maintenance.py::purge_old_records), keyed off
    polled_at like NodeDiagnosticRun.created_at. At the default 15s poll
    interval this is genuinely high-volume (pools x images-per-pool rows
    every poll) — an operator monitoring many volumes should expect this
    table to be the fastest-growing one and prune it on a real schedule.
    """

    __tablename__ = "volume_metrics"
    __table_args__ = (
        Index("ix_volume_metrics_pool_image_polled_at", "pool", "image", "polled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pool: Mapped[str] = mapped_column(String(64), nullable=False)
    image: Mapped[str] = mapped_column(String(128), nullable=False)
    iops: Mapped[float] = mapped_column(Float, nullable=False)
    read_latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    write_latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    saturated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    polled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class VolumePerfSweep(Base):
    """Result of an on-demand "Đo hiệu năng tối đa" (load sweep) benchmark
    — dashboard/routes/volumes.py's propose route, worker/executor/
    volume_perf.py's `run()`. The operator-requested, ACTIVE-load
    counterpart to VolumeMetric above: VolumeMetric's own "peak" is only
    the highest sample a real workload happened to produce, which says
    more about that workload than about the volume's actual ceiling. This
    table instead stores the real SATURATION POINT — found by sweeping
    fio iodepth 1->256 and locating the knee where IOPS growth stalls
    while latency spikes — against a dedicated SCRATCH image in the pool,
    never the operator's real volume (an explicit, confirmed scope
    decision: this must never contend with real traffic on real data).

    One row per completed (or failed) sweep run, most-recent-first per
    (pool, scratch_image) is what the Volumes page's chart actually
    queries — Action.execution_progress (same mechanism every other
    multi-step worker action already uses) covers the LIVE view of a
    sweep while it's still running; this table is the durable result
    left behind afterward.
    """

    __tablename__ = "volume_perf_sweeps"
    __table_args__ = (
        Index("ix_volume_perf_sweeps_pool_scratch_image_created_at", "pool", "scratch_image", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    action_id: Mapped[str] = mapped_column(String(36), nullable=False)
    pool: Mapped[str] = mapped_column(String(64), nullable=False)
    scratch_image: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # RUNNING/DONE/FAILED
    # JSON list[{"iodepth", "iops", "latency_avg_ms", "latency_p99_ms"}] —
    # every step actually run, in order; the source _detect_knee derived
    # knee_* below from, kept alongside it so the operator can see the
    # whole curve, not just the single point this table highlights.
    steps_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # NULL knee_* together means the sweep never saturated within the
    # tested iodepth range (steps_json's last entry is a lower-bound
    # floor, not a ceiling) — see volume_perf.py::_detect_knee.
    knee_iodepth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    knee_iops: Mapped[float | None] = mapped_column(Float, nullable=True)
    knee_latency_avg_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    knee_latency_p99_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Best-effort supplementary evidence (raw text, not parsed/interpreted)
    # for the operator to read alongside the knee — same "surface the
    # signal, don't overclaim a diagnosis" posture as the rest of this
    # app's read-only diagnostic tooling (NodeDiagnosticRun above).
    qos_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    bottleneck_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 2026-07-29: on-demand "Phân tích bằng AI" (dashboard/volume_perf_
    # analysis.py) — the knee_* columns above are a fixed heuristic
    # (_detect_knee's own growth-ratio thresholds); this is an operator-
    # requested SECOND read of the same steps_json/qos_notes/
    # bottleneck_notes evidence, via the app's configured router (same one
    # Chat-with-AI/Incident-diagnosis use), producing a plain-language
    # final conclusion. NULL until an operator actually clicks that
    # button — never computed automatically, since the app never calls
    # any AI provider without the operator's own configured router
    # (shared/router_client.py::RouterNotConfiguredError's own docstring).
    # JSON dict: {"max_iops", "max_iops_basis", "confidence",
    # "conclusion_vi", "caveats_vi"} — see that module's _tool_schema.
    ai_conclusion: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BackupJob(Base):
    """Durable, after-the-fact record of one RBD backup/metadata/restore-drill
    run (Epic 9, Story 9.1) — worker/backup/engine.py. Same relationship to
    Action.execution_progress as VolumePerfSweep above: execution_progress
    (JSON on the Action row) is the LIVE view while a job is still running;
    this table is what's left behind afterward, for retention (FR-2 — never
    delete a full export an incremental chain still depends on, tracked via
    `base_job_id`), anomaly-baseline comparison (Story 9.5 — duration/size
    vs. this image's last ~30 rows), and history display (Story 9.6).

    No separate `BackupTarget`/`RetentionPolicy` DB tables — Story 9.2
    settled that as two fixed Settings slots (`backup_target_a_*`/
    `backup_target_b_*`, config/settings.py) plus non-secret shape in
    `worker/policy/backup_policy.yaml`, not dynamic DB rows (see that
    story's Dev Notes on why `Settings.extra="forbid"` ruled out a dynamic
    per-target scheme). `backup_target_slot` here is just which of those
    two fixed slots this particular upload went to ("a"/"b") — a job that
    writes to both configured targets (FR-5) gets one BackupJob row per
    slot, linked by `run_id` (not a DB FK — just a shared UUID stamped by
    the engine for the same logical export, so Story 9.6 can group them).
    """

    __tablename__ = "backup_jobs"
    __table_args__ = (Index("ix_backup_jobs_pool_image_created_at", "pool", "image", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Shared by every BackupJob row produced from the same single `rbd
    # export`/`export-diff` invocation (one per configured backup_target
    # slot) — NOT a FK, just a grouping key.
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    pool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # "full" | "incremental" | "metadata" | "restore_drill"
    job_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # "RUNNING" | "SUCCESS" | "FAILED"
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Self-FK: for an "incremental" row, the id of the "full" BackupJob its
    # export-diff chain is based on — retention (FR-2) must never delete a
    # full export that's still some kept incremental's base_job_id.
    base_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("backup_jobs.id"), nullable=True
    )
    backup_target_slot: Mapped[str | None] = mapped_column(String(1), nullable=True)  # "a" | "b"
    remote_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BackupAnomaly(Base):
    """A BackupJob that reported `SUCCESS` (exit code 0) but deviated from
    its own image's history by more than `anomaly_threshold_stddev`
    (Story 9.5, PRD FR-15) — `worker/backup/anomaly.py::check_anomaly()`
    detects it, `worker/backup/ai_analysis.py` fills `ai_summary`. Exists
    precisely because a healthy exit code alone can't catch "backup
    finished suspiciously fast with suspiciously little data" (source data
    silently lost/corrupted) or "backup taking 10x longer than usual"
    (early warning of a cluster problem)."""

    __tablename__ = "backup_anomalies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    backup_job_id: Mapped[str] = mapped_column(String(36), ForeignKey("backup_jobs.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # "duration" | "size"
    severity: Mapped[str] = mapped_column(String(16), nullable=False)  # "critical" | "warning" | "info"
    # NULL only if AI analysis itself failed (worker/backup/ai_analysis.py
    # falls back to a generic message in that case, still non-NULL) — never
    # left blank by design, unlike VolumePerfSweep.ai_conclusion above
    # which is NULL until an operator opts in; this fires automatically.
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class BackupDigestLog(Base):
    """One row per periodic BackupDigest run (Story 9.5, PRD FR-14,
    `worker/backup/digest.py`) — the AI-summarized natural-language digest
    text itself, plus the raw counts it was built from. Story 9.4's Task
    said Dashboard display for the digest could be "a new route OR extend
    Story 9.6's dashboard/routes/backups.py" — Story 9.6 (real-time/
    historical backup visibility) didn't exist yet when this was written,
    so this story only persists the digest; Story 9.6 is expected to be
    the one that queries and renders it, not this one."""

    __tablename__ = "backup_digest_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    succeeded_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    anomaly_count: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class TestRunnerConfig(Base):
    """Epic 10 (Ceph Upgrade Test Runner) Story 10.2 -- the config the
    future 63-test-case engine (Stories 10.3-10.5) reads from: RGW test
    endpoints, which test groups (A/B/C/D)/priorities (P1/P2/P3) to run,
    and which of the 7 fixed baseline files have been uploaded.

    Deliberately a SINGLETON row (this app manages one cluster at a time,
    same posture config/settings.py already established for cluster/SSH
    config -- see shared/cluster_nodes.py::configured_nodes()) -- NOT one
    row per test run. Story 10.8 (SQLite persistence/auto-save) is the one
    that introduces per-run history/results, not this story.

    Node list, roles, and SSH user/key are deliberately NOT duplicated
    here -- they live in config/settings.py and are read live via
    configured_nodes(); this table only holds what's genuinely new for
    Epic 10 and has no existing home.
    """

    __tablename__ = "test_runner_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    rgw_endpoint_zone_a: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rgw_endpoint_zone_b: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rgw_endpoint_vip: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # JSON-encoded list of strings, e.g. '["A", "B", "C", "D"]' -- same
    # JSON-as-Text pattern as Action.execution_progress elsewhere in this
    # module (project convention for flexible structured data without a
    # dedicated child table). Nullable/absent means "nothing selected yet",
    # distinct from an empty list (explicitly unchecked-all-and-saved).
    test_groups: Mapped[str | None] = mapped_column(Text, nullable=True)
    priorities: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-encoded dict mapping each of the 7 fixed baseline keys (see
    # dashboard/routes/test_runner.py::BASELINE_FILE_KEYS) to the relative
    # path it was stored at under test_runner_baselines/ (or absent/None
    # if never uploaded) -- only the path is stored here, never file bytes.
    baseline_files: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Story 10.3 addition: SSH target for Group A's client-side I/O test
    # cases (TC-RUN-001/010, docs/ceph-upgrade-test-cases.md) -- a machine
    # with an already-mapped RBD device / mounted CephFS / configured aws
    # cli, distinct from the MON/MGR/OSD/RGW cluster nodes configured_nodes()
    # returns. Not anticipated by Story 10.2's frozen spec (which only
    # covers cluster-node config); added here since Group A's test cases are
    # the first to actually need it, keeping the same "extend the one
    # singleton config row" posture rather than a new table.
    client_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class TestRunResult(Base):
    """Epic 10 Story 10.8 -- durable projection of
    `dashboard/routes/test_runner.py`'s in-memory `_run_states[test_id]`,
    auto-saved on every write (run completion, poll reaching a terminal
    status, manual override, cancel) so a Dashboard restart mid-campaign
    doesn't wipe results back to a blank slate. One row per `test_id`
    (upserted, latest-known-only -- same posture as
    shared/models.py::CrushOsdDistribution, not an append-only history).

    Deliberately NEVER stores a background TestCase's opaque
    `background_state` -- for most background tests that dict's own
    "handle" key wraps a live `BackgroundCommandHandle`
    (worker/executor/ssh_executor.py), itself wrapping an open
    paramiko.Channel/SSH socket. There is no operation anywhere in this
    codebase that can serialize a live network connection and reconstruct
    it after a restart, so a test that was RUNNING at the exact moment the
    process died is NOT resumable -- dashboard/routes/test_runner.py's
    `_load_persisted_run_states()` normalizes any such row to ERROR with an
    explanatory note on load rather than pretending it can still be polled.
    This is a deliberate, disclosed scope boundary, not an oversight -- see
    that story's own Dev Notes for the full reasoning.
    """

    __tablename__ = "test_run_results"

    test_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    criteria_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    overridden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    override_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class NodeUpgradeGateState(str, enum.Enum):
    PREPARING = "PREPARING"
    PREPARED = "PREPARED"
    RECOVERING = "RECOVERING"
    ABORTING = "ABORTING"
    DONE = "DONE"
    FAILED = "FAILED"


class NodeUpgradeGate(Base):
    """Epic 11 (OS Upgrade Gate + Node OS Reinstall/Ceph Recovery) Story 11.2
    -- AD-18's durable, restart-surviving mid-flight state for a single
    node's Prepare -> Confirm -> Recover (or Abort) arc. This spans
    multiple separate `Action` rows plus an unbounded human-paced gap
    between them (waiting for the operator to reinstall the OS by hand) --
    `Action.execution_progress` alone (fine-grained, within one Action's
    phase sequence) cannot represent that; this table is the coarse-grained
    "which stage of the overall arc" view. The two views are complementary,
    not redundant (AD-18).

    `state` moves strictly forward: PREPARING -> PREPARED -> RECOVERING ->
    DONE, or PREPARING -> PREPARED -> ABORTING -> DONE, or into terminal
    FAILED from PREPARING/RECOVERING. This model only validates `state` is
    one of the 6 known values (CheckConstraint below) -- it does not
    enforce transition order; that is up to the Stories that perform each
    transition (11.3/11.4).

    Dashboard writes prepare_action_id/confirm_action_id/abort_action_id
    (each set in the same request that creates the corresponding synthetic
    Action, before calling approve_action_core) -- Worker never writes
    these FK columns, only `state` and the descriptive fields.
    """

    __tablename__ = "node_upgrade_gates"
    __table_args__ = (
        CheckConstraint(
            "state IN ('" + "','".join(s.value for s in NodeUpgradeGateState) + "')",
            name="ck_node_upgrade_gates_state_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    host: Mapped[str] = mapped_column(String(64), nullable=False)
    target_version: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=NodeUpgradeGateState.PREPARING.value)
    # JSON-encoded list, e.g. '["mon", "osd"]' -- same JSON-as-Text
    # convention as Action.execution_progress/TestRunnerConfig.test_groups.
    # Populated by node_os_gate_prepare (Story 11.3) from the node's actual
    # roles at Prepare time, so Recovery (Story 11.4) knows what to restore.
    roles_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-encoded list of {osd_id, osd_fsid}, same JSON-as-Text convention.
    # Populated by node_os_gate_prepare's OSD backup phase (Story 11.3).
    osd_backup: Mapped[str | None] = mapped_column(Text, nullable=True)
    prepare_action_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("actions.id"), nullable=True)
    confirm_action_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("actions.id"), nullable=True)
    abort_action_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("actions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class NodeUpgradeGateLock(Base):
    """Epic 11 Story 11.2 -- AD-24's real fix for the TOCTOU race that a
    plain SELECT-then-check (is_node_upgrade_gate_pending, AD-19) cannot
    close on its own: a singleton row claimed via one atomic conditional
    UPDATE (compare-and-swap), the same singleton-row idiom this codebase
    already uses for SystemFlag/kill-switch (shared/kill_switch.py).

    `active_gate_id` is deliberately NOT a ForeignKey to
    NodeUpgradeGate.id: the claim (this row's UPDATE) happens BEFORE the
    corresponding NodeUpgradeGate row is inserted (both within the same
    request/transaction -- the gate id is generated client-side and used
    for both writes) -- a non-deferred FK would reject the claim outright
    since the referenced row wouldn't exist yet. See
    shared/node_upgrade_gate.py for the claim/release functions.
    """

    __tablename__ = "node_upgrade_gate_locks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    active_gate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class CrushStructureSnapshot(Base):
    """Epic 12 Story 12.1 (AD-26) -- one row per DISTINCT CRUSH structure
    ever observed (Root/Rack/Host/OSD tree + Weight, from `ceph osd crush
    dump`) -- `watcher/crush_structure_monitor.py` only inserts a new row
    when `tree_json` (canonicalized -- object keys AND array element order
    both sorted, see that module's own docstring for why plain
    `json.dumps(..., sort_keys=True)` alone is not enough) differs from the
    single most-recent row. `diff_json` is NULL for the very first snapshot
    ever taken (no prior row to diff against) and otherwise holds the
    Bucket/OSD add/remove/reweight delta versus the row immediately before
    it -- an OSD merely flipping up/down (no Weight/position change) is
    NEVER represented here (that is `OSD_DOWN`'s Incident family, unrelated
    to this table).

    Whole-tree JSON blob (not a normalized Bucket/OSD table) mirrors this
    codebase's established convention for nested structured data
    (`Action.execution_progress`/`action_params`/`target_nodes`) -- see
    AD-26's own reasoning.
    """

    __tablename__ = "crush_structure_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tree_json: Mapped[str] = mapped_column(Text, nullable=False)
    diff_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class CrushOsdDistribution(Base):
    """Epic 12 Story 12.1 (AD-27) -- latest-known-only (UPSERT, not
    append-only) actual data distribution per OSD, from `ceph osd df` --
    `watcher/crush_distribution_monitor.py` is the sole writer. Deliberately
    stores RAW bytes (`bytes_used`/`bytes_total`), never a precomputed
    percentage: a percentage cannot be summed across OSDs of different
    capacity, so Story 12.2's Host/Rack-level skew calculation needs the
    raw numbers to derive a correct ratio by summing bytes up the CRUSH
    tree -- see AD-27's own reasoning for the bug this avoids.

    A row is DELETED (not left stale) the moment its `osd_id` is confirmed
    absent from a SUCCESSFUL `ceph osd df` scan -- distinct from a FAILED
    scan attempt, which must leave every existing row untouched (see
    `watcher/crush_distribution_monitor.py::sync_distribution`'s own
    docstring). `pgs` shares this same table/row/`updated_at` because both
    numbers come from the ONE `ceph osd df` call (AD-25b) -- there is no
    separate slower scan for PG count as originally scoped in the PRD draft.
    """

    __tablename__ = "crush_osd_distribution"

    # autoincrement=False: this is the REAL Ceph osd_id (caller-assigned),
    # never a synthetic surrogate key — an Integer primary key defaults to
    # autoincrement=True otherwise, which would silently create an unused
    # Postgres SERIAL sequence and invite the wrong mental model.
    osd_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    host: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bytes_used: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bytes_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pgs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class TelegramChannelConfigChange(Base):
    """Append-only audit trail of Bot Token/Chat ID saves on the
    "Alert Telegram" page (`dashboard/routes/telegram_alerts.py::
    telegram_channel_submit`, the ONLY writer) -- answers "kênh này ai từng
    cấu hình, đổi lúc nào" for an admin, since `config/settings.py`'s live
    fields only ever hold the CURRENT value, never history.

    Same "ad-hoc operator action, no Incident FK" shape as
    NodeDiagnosticRun above, not an AuditEntry row (that table requires a
    real incident_id). `bot_token_masked` stores the SAME masked form the
    page itself renders (`dashboard/routes/settings.py::_mask_key`) --
    never the full token -- so this history table is never a second place
    a leaked DB backup could recover a live secret from. `chat_id` is
    stored in full: it's already rendered unmasked on the page today (see
    telegram_alerts.py::_context) and identifies a chat, not a credential.
    """

    __tablename__ = "telegram_channel_config_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bot_token_masked: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
