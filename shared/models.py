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
    mgr/osd/rgw nodes, exec mode, container names, SSH creds. As of Phase 3
    also carries its own RBD backup config (`backup_*` fields below) — one
    backup destination per cluster, deliberately SEPARATE credentials from
    `ssh_user`/`ssh_key_path` above (same PRD FR-4 "backup destination must
    never share creds/network access with the source cluster's admin path"
    reasoning `config/settings.py`'s `backup_target_a/b_*` fields already
    follow). Still does NOT carry patch/upgrade config — that remains
    `.env`-scoped/single-cluster until its own later phase (see
    docs/multi-cluster-deployment.md's sibling plan).

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
    openstack_controller_nodes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    openstack_compute_nodes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    openstack_ceph_config_path: Mapped[str] = mapped_column(Text, nullable=False, default="/etc/ceph")
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
    # 2026-08-11 (multi-tenant remediation Phase 3) -- RBD backup for this
    # ADDITIONAL cluster, off by default (blank/false means "no backup
    # pipeline for this cluster", same opt-in posture as telegram_* above).
    # ONE backup target (not the default cluster's a/b pair) -- additional
    # clusters are new/opt-in, one destination is the pragmatic starting
    # point; see worker/backup/storage/factory.py::get_backend_for_cluster.
    backup_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # CSV of "pool/image" pairs (same tolerant comma-split parsing as
    # ceph_mon_nodes above) -- which RBD images worker/backup/scheduler.py
    # schedules a backup job for. Retention keep_full_count/
    # keep_incremental_count stay the GLOBAL worker/policy/backup_policy.yaml
    # values for every cluster (not overridable per-cluster, Phase 3 scope).
    backup_tracked_images: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Optional per-cluster override of backup_policy.yaml's
    # full_refresh_every_n_days (per-image override there doesn't apply here
    # -- Phase 3 keeps this one flat setting for the whole cluster). NULL
    # means never force a full refresh, same "blank = unbounded chain"
    # default the global policy's own field has.
    backup_full_refresh_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    backup_transport: Mapped[str] = mapped_column(String(16), nullable=False, default="")  # "ssh" | "s3" | ""
    backup_ssh_host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    backup_ssh_user: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    backup_ssh_key_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    backup_ssh_landing_dir: Mapped[str] = mapped_column(Text, nullable=False, default="")
    backup_s3_endpoint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    backup_s3_access_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    backup_s3_secret_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    backup_s3_bucket: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    backup_immutable_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    backup_immutable_lock_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
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
    # Last hourly Telegram reminder. NULL means no reminder has been sent;
    # created_at remains the baseline for the first reminder.
    telegram_reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    """Chat messages isolated by login account. `session_id` groups messages
    into conversations within one account — "the current session" is the
    most recently created row for that actor (dashboard/routes/chat.py),
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
    # Conversation owner. New user AND assistant rows always carry the login
    # username so every read/write can enforce account isolation. Nullable
    # only for compatibility with assistant rows created before this rule.
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
    ceph_chat_restricted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class VitastorUser(Base):
    """Login accounts owned exclusively by the Vitastor control plane.

    Deliberately separate from ``users``: a Ceph account never gains
    Vitastor access implicitly, and Vitastor lifecycle changes cannot affect
    Ceph authentication. The root account from ``.env`` remains available
    to bootstrap the first Vitastor admin and is not stored in either table.
    """

    __tablename__ = "vitastor_users"
    __table_args__ = (UniqueConstraint("username", name="uq_vitastor_users_username"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class VitastorCluster(Base):
    """Connection inventory owned exclusively by the Vitastor product."""

    __tablename__ = "vitastor_clusters"
    __table_args__ = (UniqueConstraint("name", name="uq_vitastor_clusters_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    management_host: Mapped[str] = mapped_column(String(255), nullable=False)
    etcd_address: Mapped[str] = mapped_column(Text, nullable=False)
    etcd_prefix: Mapped[str] = mapped_column(String(255), nullable=False, default="/vitastor")
    config_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ssh_user: Mapped[str] = mapped_column(String(64), nullable=False)
    ssh_key_path: Mapped[str] = mapped_column(Text, nullable=False)
    exec_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    container_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_status_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class VitastorOperation(Base):
    """Deploy/delete workflow state, isolated from Ceph Incident/Action."""

    __tablename__ = "vitastor_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING_APPROVAL")
    cluster_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cluster_name: Mapped[str] = mapped_column(String(128), nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    plan_text: Mapped[str] = mapped_column(Text, nullable=False)
    progress_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class VitastorDiagnosticRun(Base):
    """Read-only AI diagnosis persisted independently from Ceph incidents."""

    __tablename__ = "vitastor_diagnostic_runs"
    __table_args__ = (Index("ix_vitastor_diagnostic_cluster_created", "cluster_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    health: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    diagnosis_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class VitastorMetricSample(Base):
    """Time-series cluster metric captured by the independent Vitastor watcher."""

    __tablename__ = "vitastor_metric_samples"
    __table_args__ = (Index("ix_vitastor_metric_cluster_time", "cluster_id", "collected_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[str] = mapped_column(String(36), nullable=False)
    health: Mapped[str] = mapped_column(String(16), nullable=False)
    osd_up: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    osd_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    free_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    used_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    etcd_up: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    etcd_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    etcd_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    etcd_quorum: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    etcd_leader_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    read_iops: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    write_iops: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    read_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    write_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    read_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    write_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    recovery_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    degraded_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class VitastorOsdMetricSample(Base):
    """Per-OSD time-series metric; no Ceph OSD table or identity is reused."""

    __tablename__ = "vitastor_osd_metric_samples"
    __table_args__ = (Index("ix_vitastor_osd_metric_cluster_osd_time", "cluster_id", "osd_id", "collected_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[str] = mapped_column(String(36), nullable=False)
    osd_id: Mapped[str] = mapped_column(String(64), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_up: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    used_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    read_iops: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    write_iops: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    read_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    write_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    read_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    write_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class VitastorNodeMetricSample(Base):
    __tablename__ = "vitastor_node_metric_samples"
    __table_args__ = (Index("ix_vitastor_node_metric_cluster_host_time", "cluster_id", "host", "collected_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[str] = mapped_column(String(36), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    osd_processes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cpu_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    ram_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    max_temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_wear_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    media_errors: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    smart_failing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class VitastorNetworkMetricSample(Base):
    __tablename__ = "vitastor_network_metric_samples"
    __table_args__ = (Index("ix_vitastor_network_metric_cluster_time", "cluster_id", "collected_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    reachable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    jumbo_9000: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    interface_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class VitastorEntityMetricSample(Base):
    """Generic pool/image/OSD/cluster metrics used by dynamic baselines."""
    __tablename__ = "vitastor_entity_metric_samples"
    __table_args__ = (Index("ix_vita_entity_metric_lookup", "cluster_id", "entity_type", "entity_name", "collected_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_name: Mapped[str] = mapped_column(String(255), nullable=False)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class VitastorAnomalyEvent(Base):
    """Open/resolved anomaly lifecycle generated from robust dynamic baselines."""
    __tablename__ = "vitastor_anomaly_events"
    __table_args__ = (Index("ix_vita_anomaly_cluster_status", "cluster_id", "status", "detected_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_name: Mapped[str] = mapped_column(String(255), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="WARNING")
    current_value: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_value: Mapped[float] = mapped_column(Float, nullable=False)
    deviation_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class VitastorActionClassification(str, enum.Enum):
    """Vitastor-only mirror of shared/models.py::ActionClassification — kept a
    separate enum so a Vitastor policy decision can never be confused with a
    Ceph one, even though the two happen to share the same SAFE/RISKY names."""

    SAFE = "SAFE"
    RISKY = "RISKY"


class VitastorActionStatus(str, enum.Enum):
    """Lifecycle of a VitastorRemediationAction — the Vitastor counterpart of
    ActionStatus. AUTO_EXECUTED is the terminal state for a SAFE action the
    system ran itself; the PENDING_APPROVAL -> APPROVED -> EXECUTING ->
    EXECUTED chain is the operator-gated RISKY path."""

    PENDING_APPROVAL = "PENDING_APPROVAL"  # RISKY, waiting for an operator
    AUTO_EXECUTED = "AUTO_EXECUTED"        # SAFE, executed by the system
    APPROVED = "APPROVED"                  # operator approved, not yet run
    REJECTED = "REJECTED"                  # operator rejected
    EXECUTING = "EXECUTING"                # command in flight (background task)
    EXECUTED = "EXECUTED"                  # approved RISKY action executed
    FAILED = "FAILED"                      # execution raised


class VitastorRemediationAction(Base):
    """AI/telemetry-proposed remediation for a Vitastor cluster — the Vitastor
    equivalent of the Ceph Incident->Action->AuditEntry loop, kept in its own
    tables so a Ceph Action never targets a Vitastor cluster and vice-versa
    (same isolation posture as every other ``vitastor_*`` table above).

    Conservative by default, exactly like worker/policy/gate.py: only an
    action_id on vitastor/remediation.py's explicit SAFE allowlist ever
    auto-executes (status AUTO_EXECUTED); everything else is RISKY and waits
    at PENDING_APPROVAL for an operator, the same posture as the Ceph
    device_health_monitor path that creates Actions straight at
    PENDING_APPROVAL with no AI round trip. ``proposed_command`` is always a
    resolved preview of a CLOSED command builder's output (never free-text
    shell); ``action_params``/``target_host`` carry only what the builder
    needs to reproduce it at execute time, long after the originating poll.

    Like the other Vitastor tables (and unlike Ceph's Incident.cluster_id),
    ``cluster_id`` is a plain String, not a real ForeignKey — the Vitastor
    product manages its own cluster inventory and never joins the Ceph
    ``clusters`` table."""

    __tablename__ = "vitastor_remediation_actions"
    __table_args__ = (
        CheckConstraint(
            "classification IN ('" + "','".join(c.value for c in VitastorActionClassification) + "')",
            name="ck_vita_remediation_classification_valid",
        ),
        CheckConstraint(
            "status IN ('" + "','".join(s.value for s in VitastorActionStatus) + "')",
            name="ck_vita_remediation_status_valid",
        ),
        Index("ix_vita_remediation_cluster_status", "cluster_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # Where this proposal came from: MONITOR (deterministic watcher signal),
    # DIAGNOSIS (operator ran AI diagnosis) or CHAT. Descriptive, not a
    # state machine — plain string like AuditEntry.event_type.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="MONITOR")
    # The VitastorDiagnosticRun / VitastorAnomalyEvent id this came from, when
    # applicable; NULL for a bare telemetry-signal proposal.
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action_id: Mapped[str] = mapped_column(String(64), nullable=False)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=VitastorActionStatus.PENDING_APPROVAL.value)
    # Node the command runs on (an OSD/mon host), validated against the
    # cluster's known-host allowlist both at propose and execute time.
    target_host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # JSON-encoded dict of resolved params the closed command builder needs
    # (e.g. {"osd_id": "3"}). Same JSON-in-Text convention as
    # Action.action_params. NULL for builders that need only target_host.
    action_params: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Resolved preview of the command that will run — NULL for a no-op
    # action_id like investigate_manually (approving it just records the
    # incident as handled, runs nothing).
    proposed_command: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Truncated stdout/stderr captured after execution — an audit record of
    # what ran, not a full log store (same posture as NodeDiagnosticRun).
    result_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stable key identifying the underlying problem so the watcher never
    # stacks duplicate OPEN proposals for the same thing every poll (e.g.
    # "start_osd_service:node-a:3"). Empty for operator-initiated proposals.
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False, default="vitastor-monitor")
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Same JSON-encoded {channel_key: message_id} convention / opt-in posture
    # as Action.telegram_message_ids — populated only once a Telegram approval
    # channel is configured; harmless NULL otherwise.
    telegram_message_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class VitastorAuditEntry(Base):
    """Append-only audit trail for Vitastor remediation — the Vitastor
    equivalent of shared/models.py::AuditEntry, in its own table so a Vitastor
    lifecycle event can never be written into (or confused with) the Ceph
    audit history. ``event_type``/``actor`` are plain descriptive strings, not
    a CheckConstraint enum, same reasoning as AuditEntry: this is history, not
    a safety-critical state machine."""

    __tablename__ = "vitastor_audit_entries"
    __table_args__ = (Index("ix_vita_audit_cluster_created", "cluster_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # The VitastorRemediationAction.id this event is about; NULL for a
    # cluster-level event not tied to one specific action.
    action_pk: Mapped[str | None] = mapped_column(String(36), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class ChatPreference(Base):
    """Per-login chat persona, including the `.env` root admin which has
    no User row. The username is deliberately not a foreign key for that
    reason; chat routes scope every read/write to the authenticated login."""

    __tablename__ = "chat_preferences"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    ai_name: Mapped[str] = mapped_column(String(64), nullable=False, default="AI")
    female_address: Mapped[str] = mapped_column(
        String(128), nullable=False, default="Mình yêu ơi, em là"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


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
        Index("ix_volume_metrics_cluster_id", "cluster_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("clusters.id"), nullable=True)
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
    __table_args__ = (
        Index("ix_backup_jobs_pool_image_created_at", "pool", "image", "created_at"),
        Index("ix_backup_jobs_cluster_id", "cluster_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # 2026-08-11 (multi-tenant remediation Phase 3) -- NULL means the
    # default cluster, same semantics as Incident.cluster_id (no backfill:
    # every row from before this column existed means "the default
    # cluster", same as every row a single-cluster deployment ever
    # inserts). MUST be included in every query filtered by (pool, image)
    # below -- two clusters can otherwise have a same-named pool/image and
    # silently corrupt each other's retention/restore-chain/anomaly-
    # baseline queries.
    cluster_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("clusters.id"), nullable=True)
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
    # "a" | "b" for the default cluster's two fixed slots; "cluster" for any
    # additional cluster (Phase 3) -- it has exactly one backup target, so
    # there's no a/b distinction to make, just a fixed marker.
    backup_target_slot: Mapped[str | None] = mapped_column(String(16), nullable=True)
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
    # convention as Action.execution_progress.
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
