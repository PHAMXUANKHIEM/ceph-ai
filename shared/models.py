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
    ceph_keyring_path: Mapped[str] = mapped_column(
        Text, nullable=False, default="/etc/ceph/ceph.client.admin.keyring"
    )
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
    openstack_openrc_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
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
    # Maximum acceptable age of a successful recovery point for every
    # tracked image in this additional cluster.
    backup_rpo_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
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
    autonomy_environment: Mapped[str] = mapped_column(
        String(16), nullable=False, default="production", server_default="production",
    )
    autopilot_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class IncidentStatus(str, enum.Enum):
    NEW = "NEW"
    DIAGNOSING = "DIAGNOSING"
    AUTO_FIXED = "AUTO_FIXED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    GRACE_PENDING = "GRACE_PENDING"
    # 2026-08-20: lệnh khắc phục đã chạy xong exit 0, NHƯNG chưa ai hỏi lại
    # cụm xem lỗi đã thật sự hết chưa. Trước trạng thái này, "SSH trả về 0"
    # bị coi thẳng là RESOLVED (worker/llm/router_client.py::
    # _record_approved_execution_result) — một lệnh chạy trót lọt mà không
    # sửa được gì vẫn khép Incident lại, và không có gì báo cho operator
    # biết sự khác nhau. VERIFYING là khoảng giữa: watcher/verify.py đối
    # chiếu ceph_code với `ceph health detail` sau một khoảng chờ rồi mới
    # quyết RESOLVED (kèm Telegram báo OK) hay quay lại chẩn đoán.
    VERIFYING = "VERIFYING"
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
    # Snapshot JSON của detector (latency/device health/metric...), không
    # chứa credential; dùng làm provenance cho correlation/postmortem.
    signal_evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    postmortem_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    postmortem_generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    postmortem_prompt_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Last hourly Telegram reminder. NULL means no reminder has been sent;
    # created_at remains the baseline for the first reminder.
    telegram_reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 2026-08-20 (xác minh sau khắc phục): thời điểm SỚM NHẤT được phép kiểm
    # chứng. Không kiểm ngay sau khi lệnh chạy xong vì rất nhiều lỗi cần
    # thời gian mới hết (PG backfill xong, OSD vào lại quorum, mon clock
    # skew hội tụ) — kiểm ngay sẽ luôn ra "chưa hết" một cách giả tạo.
    # NULL nghĩa là Incident này không nằm trong luồng xác minh.
    verify_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Số vòng đã kiểm chứng-rồi-chẩn đoán lại. Chặn ở
    # settings.incident_verify_max_attempts để một lỗi không bao giờ tự hết
    # (ví dụ CRUSH skew cần người cân lại weight) không thành vòng lặp vô
    # tận gọi router tốn phí và spam Telegram.
    verify_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ActionClassification(str, enum.Enum):
    # AI roadmap Pha 0.4 (Plan/ai-missing-features-roadmap.md, section
    # 3.3) -- 4-tier safety policy. READ_ONLY/SAFE both auto-execute
    # (worker/llm/router_client.py::diagnose_incident,
    # dashboard/routes/chat.py's confirm-immediately path) — READ_ONLY
    # exists for a future action_id that provably touches no cluster state
    # at all (nothing reclassified into it yet, see worker/policy/
    # action_policy.yaml's own comment), kept distinct from SAFE only so a
    # later capability check can tell "definitely read-only" apart from
    # "mutates but judged low-risk". RISKY/DESTRUCTIVE both require
    # explicit Dashboard/Telegram approval (dashboard/routes/actions.py::
    # approve_action_core) and can NEVER auto-execute — enforced not just
    # by policy but in code (worker/llm/router_client.py::
    # _maybe_execute_safe_action and dashboard/routes/chat.py's SAFE-path
    # both hard-assert the classification isn't DESTRUCTIVE before running
    # anything, so a future policy-loading bug can't silently auto-run one).
    # DESTRUCTIVE additionally marks an action_id as irreversible/data-
    # destroying (pool purge, cluster teardown, overwrite-production
    # restore) for worker/policy/gate.py::classify_action's own
    # conservative-override precedence (DESTRUCTIVE beats every other list
    # an action_id might mistakenly also appear in) — see that function's
    # own docstring.
    READ_ONLY = "READ_ONLY"
    SAFE = "SAFE"
    RISKY = "RISKY"
    DESTRUCTIVE = "DESTRUCTIVE"


class ActionStatus(str, enum.Enum):
    PENDING = "PENDING"  # just classified (Story 3.1), not yet processed
    AUTO_EXECUTED = "AUTO_EXECUTED"  # Safe action executed (Story 3.2)
    PENDING_APPROVAL = "PENDING_APPROVAL"  # Risky action awaiting operator (Story 4.2)
    APPROVED = "APPROVED"  # operator approved (Story 4.3)
    EXECUTING = "EXECUTING"  # autonomous executor holds the cluster lease
    GRACE_PENDING = "GRACE_PENDING"  # lab L3 countdown; operator may cancel
    INCONCLUSIVE = "INCONCLUSIVE"  # side effect may have happened; never auto-retry
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
        # AI roadmap Pha 0.4 -- partial unique index (only enforced when
        # idempotency_key IS NOT NULL AND status is still in-flight (same
        # sqlite_where/postgresql_where pattern Cluster.is_default's own
        # uq_clusters_single_default index already uses above). Scoped to
        # in-flight statuses ONLY, deliberately NOT a permanent global
        # uniqueness guarantee — idempotency_key is computed from
        # (action_id, target nodes, params), not incident_id (see
        # router_client.py's own comment at its call site), so a
        # permanent constraint would forever block a legitimate FUTURE
        # incident from proposing the exact same command against the same
        # target again after an earlier one already finished (e.g.
        # resync_ntp on the same node recurring days later) — this index
        # only ever needs to catch a SECOND proposal for the same
        # not-yet-resolved command while the first one is still PENDING/
        # PENDING_APPROVAL/APPROVED. Every pre-Pha-0.4 Action row (NULL
        # idempotency_key) and every action family that never opts in stay
        # completely unaffected either way.
        Index(
            "uq_actions_idempotency_key_inflight",
            "idempotency_key",
            unique=True,
            sqlite_where=text(
                "idempotency_key IS NOT NULL AND status IN ('PENDING','PENDING_APPROVAL','APPROVED','GRACE_PENDING')"
            ),
            postgresql_where=text(
                "idempotency_key IS NOT NULL AND status IN ('PENDING','PENDING_APPROVAL','APPROVED','GRACE_PENDING')"
            ),
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
    # AI roadmap Pha 0.4 -- stale-evidence check for the approval flow
    # (roadmap section 3.3's "expiry"/"stale-evidence check"). Set only by
    # worker/llm/router_client.py::diagnose_incident's own Action-creation
    # call site (settings.action_approval_expiry_hours after proposal
    # time) — every OTHER action-creation call site in this codebase
    # (Chat-with-AI, DeviceHealth, CRUSH skew, cluster upgrade/patch/
    # deploy, etc.) leaves this NULL, meaning "no expiry check applies",
    # same opt-in-column posture as execution_progress/telegram_* above.
    # NULL is deliberately treated as "never expires" by
    # dashboard/routes/actions.py::approve_action_core, not as "already
    # expired" — an unset expiry must never accidentally block every
    # pre-existing action family that doesn't populate it.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    grace_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # AI roadmap Pha 0.4 -- deterministic hash of
    # (incident_id, action_id, action_params) at classification time, set
    # by the same router_client.py call site as expires_at above. A
    # SECOND layer of duplicate-Action protection on top of the existing
    # `existing_action = session.query(Action).filter_by(incident_id=...)`
    # guard already in diagnose_incident (which only catches a re-run for
    # the SAME incident_id) — this one is enforced by a real DB unique
    # index (see the Pha 0.4 migration) so it also catches a hypothetical
    # future caller that proposes the identical action from a DIFFERENT
    # incident_id (e.g. two near-simultaneous incidents both diagnosing to
    # the same command against the same target). NULL for every action
    # family that doesn't opt in, same posture as expires_at.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
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


class RemediationCase(Base):
    """Immutable-at-source memory for one proposed remediation Action."""

    __tablename__ = "remediation_cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    incident_id: Mapped[str] = mapped_column(String(36), ForeignKey("incidents.id"), nullable=False)
    action_id: Mapped[str] = mapped_column(String(36), ForeignKey("actions.id"), nullable=False, unique=True)
    cluster_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("clusters.id"), nullable=True)
    fault_family: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_keys_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ceph_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployment_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    topology_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    autonomy_decision: Mapped[str] = mapped_column(String(32), nullable=False)
    playbook_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    preflight_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    command_preview_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pre_state_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_state_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    rollback_state_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="PROPOSED")
    side_effects_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recovery_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    regressed_1h: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    regressed_24h: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    regressed_7d: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    operator_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    operator_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Pha 3 shadow evaluation is evidence only.  These fields never feed the
    # execution gate; they preserve what Autopilot *would* have done at the
    # instant this Case was proposed so it can later be compared with truth.
    shadow_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    shadow_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    shadow_trust_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    shadow_sample_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shadow_recorded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class PlaybookStat(Base):
    """Version/scope-separated aggregate; populated by the later evaluator."""

    __tablename__ = "playbook_stats"
    __table_args__ = (
        UniqueConstraint("playbook_id", "playbook_version", "scope_key", name="uq_playbook_stats_scope"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    playbook_id: Mapped[str] = mapped_column(String(64), nullable=False)
    playbook_version: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(256), nullable=False)
    proposed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    executed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    verified_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    inconclusive_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    trust_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    maturity_level: Mapped[str] = mapped_column(String(16), nullable=False, default="L0", server_default="L0")
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto_disabled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    promotion_candidate_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    promotion_blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class AutopilotLease(Base):
    """Crash-expiring cluster-wide mutex for autonomous write execution."""
    __tablename__ = "autopilot_leases"

    cluster_id: Mapped[str] = mapped_column(String(36), ForeignKey("clusters.id"), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(36), ForeignKey("actions.id"), nullable=False, unique=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AutopilotConfigAudit(Base):
    """Append-only audit for admin changes to the global kill switch."""
    __tablename__ = "autopilot_config_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    new_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class AutopilotClusterConfigAudit(Base):
    """Append-only audit for the stricter per-cluster commissioning gate."""
    __tablename__ = "autopilot_cluster_config_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(String(36), ForeignKey("clusters.id"), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_environment: Mapped[str] = mapped_column(String(16), nullable=False)
    new_environment: Mapped[str] = mapped_column(String(16), nullable=False)
    previous_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    new_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class ActionPolicyOverride(Base):
    """Admin-selected SAFE/RISKY/DESTRUCTIVE classification used by the Worker."""
    __tablename__ = "action_policy_overrides"

    action_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ActionPolicyOverrideAudit(Base):
    """Append-only history for every action classification change."""
    __tablename__ = "action_policy_override_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    action_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_classification: Mapped[str] = mapped_column(String(16), nullable=False)
    new_classification: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class IncidentTimelineEvent(Base):
    """Append-only lifecycle ledger with exact event timestamps."""

    __tablename__ = "incident_timeline_events"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", name="uq_incident_timeline_event_source"),
        Index("ix_incident_timeline_event_order", "incident_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    incident_id: Mapped[str] = mapped_column(String(36), ForeignKey("incidents.id"), nullable=False)
    action_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("actions.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class ObjectStorageAuditEntry(Base):
    """Audit trail for direct RGW mutations that have no Incident parent."""

    __tablename__ = "object_storage_audit_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(String(36), ForeignKey("clusters.id"), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(255), nullable=False)
    preview: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BucketLoggingConfig(Base):
    """Version-selected native or compatibility bucket logging config."""

    __tablename__ = "bucket_logging_configs"
    __table_args__ = (UniqueConstraint("cluster_id", "source_bucket", name="uq_bucket_logging_source"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(String(36), ForeignKey("clusters.id"), nullable=False)
    source_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    target_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    prefix: Mapped[str] = mapped_column(String(1024), nullable=False, default="logs/")
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    checkpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


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
    __table_args__ = (Index("ix_chat_messages_actor_cluster_time", "actor", "cluster_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Nullable in the schema (not NOT NULL) purely so rows created before
    # this column existed don't need a schema-level backfill guarantee — the
    # migration DOES backfill them (one shared id, grouping all pre-existing
    # history into a single legacy session) and every application code path
    # that inserts a ChatMessage always sets this; NULL should not occur for
    # any row created going forward.
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Cluster context used for every tool call and copied to a confirmed
    # Incident. NULL denotes legacy rows created before multi-cluster Chat.
    cluster_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("clusters.id"), nullable=True
    )
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
    cluster_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clusters.id"), nullable=False, index=True
    )
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

    # (cluster_id, osd_id) is the identity: every Ceph cluster normally has
    # an osd.0, osd.1, ... of its own. autoincrement=False keeps osd_id as
    # the REAL caller-assigned Ceph id, never a synthetic sequence.
    cluster_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clusters.id"), primary_key=True
    )
    osd_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    host: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bytes_used: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bytes_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pgs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class CephCapacitySample(Base):
    """Append-only cluster/pool/OSD capacity observation used for forecasting."""

    __tablename__ = "ceph_capacity_samples"
    __table_args__ = (
        Index("ix_ceph_capacity_series", "cluster_id", "entity_type", "entity_name", "captured_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(String(36), ForeignKey("clusters.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_name: Mapped[str] = mapped_column(String(128), nullable=False)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_percent: Mapped[float] = mapped_column(Float, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


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


class CapabilityStatus(str, enum.Enum):
    """AI roadmap Pha 0.1 (Plan/ai-missing-features-roadmap.md) -- the
    standardized response every capability-aware AI feature must be able to
    give BEFORE any proposal/action is generated for a cluster (fail-closed
    posture, section 3.2 of that roadmap). This enum only covers the
    coarse "do we have a usable version/deployment-mode inventory for this
    cluster at all" question -- Pha 0.2's later per-command capability
    matrix answers the finer "is THIS specific command/flag supported on
    THIS version" question and is deliberately out of scope here.

    - SUPPORTED: `ceph versions` succeeded, every daemon agrees on one
      version, and that version's major release is one this codebase
      recognizes (shared/ceph_releases.py::RELEASES) -- safe to build
      version-aware capability checks on top of.
    - UNSUPPORTED_VERSION: the query succeeded but the reported version's
      major isn't in shared/ceph_releases.py's table (too old/too new/
      unparseable) -- there is no reference data to check compatibility
      against, so any version-aware feature must refuse rather than guess.
    - UNAVAILABLE: the query itself failed (every MON node unreachable/
      timed out) -- a transient condition, not a verdict on the cluster's
      version; retried on the next scan.
    - UNKNOWN: no snapshot has ever been collected yet for this cluster
      (e.g. just added, watcher hasn't ticked once since).
    """

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class ClusterCapabilityInventory(Base):
    """AI roadmap Pha 0.1 -- one row per scan tick (append-only, mirrors
    `CrushStructureSnapshot`'s history-not-upsert shape above) recording
    what `watcher/capability_inventory.py::collect_and_store` observed for
    one cluster: per-daemon-type Ceph version(s), whether they're mixed
    (an interrupted/partial upgrade), the cluster's deployment mode, and
    the resulting `CapabilityStatus`. Read back by
    `dashboard/routes/clusters.py` (latest row per cluster) to show an
    operator whether THIS cluster currently has a usable capability
    inventory, and by any future version-aware AI feature (Pha 0.3+) as
    the "is it even safe to consider a proposal for this cluster" gate.

    Kept as history (not a single upserted row) on purpose: precisely the
    signal Pha 0's own `[~] 0.1 ... cảnh báo mixed-version` needs is "was
    this cluster mid-upgrade at time T", which a latest-row-only table
    would silently overwrite the moment the upgrade finished.
    """

    __tablename__ = "cluster_capability_inventory"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "','".join(s.value for s in CapabilityStatus) + "')",
            name="ck_cluster_capability_inventory_status_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clusters.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # "cephadm" / "docker" / "podman" / "none" / None (couldn't be
    # determined -- e.g. the version query itself failed).
    deployment_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # JSON: {daemon_type: [version, ...]} from summarize_versions_payload.
    per_type_versions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON list[str] -- every distinct version string seen this scan.
    distinct_versions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_mixed_version: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Set only when every daemon agrees on one version (matches
    # summarize_versions_payload's own "current_version" semantics).
    current_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Set only for SUPPORTED/UNSUPPORTED_VERSION rows -- the current_version's
    # major, e.g. 18 for "18.2.2" (None for UNAVAILABLE/UNKNOWN).
    current_major: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Populated only for UNAVAILABLE rows -- the CephQueryError text, so an
    # operator can see WHY without digging through watcher logs.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class CapabilityMatrixEntryStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"


class CapabilityMatrixEntry(Base):
    """AI roadmap Pha 0.2 (Plan/ai-missing-features-roadmap.md, section
    3.2) -- "is THIS specific command/flag/module/backend supported on
    THIS Ceph major version" reference data, each row traceable to a real
    Ceph documentation source (`doc_url`) and the operator who verified it
    (`verified_by`/`verified_at`) -- section 3.2 explicitly bans deciding
    this from a blog/community answer, and section 3.1 bans any AI
    conclusion without real evidence, so this table is deliberately
    OPERATOR-MAINTAINED (via `dashboard/routes/capability_matrix.py`, an
    admin-only CRUD page), never auto-populated by a model guessing at
    upstream docs -- same "static reference data, extended by hand,
    verified against the real download.ceph.com/docs.ceph.com source, not
    auto-updated" posture as `shared/ceph_releases.py`'s own table.

    A missing/empty table is the CORRECT fail-closed starting state (Pha 0
    section 3.2: "Fail closed khi version hoặc capability chưa xác định")
    -- `shared/capability_matrix.py::check_capability` returns UNKNOWN for
    any `command_id` with no ACTIVE entry, never assumes SUPPORTED just
    because nothing says otherwise.

    History, not upsert (mirrors `ClusterCapabilityInventory`'s own
    append-vs-overwrite reasoning): superseding an entry writes a NEW row
    and DEPRECATES the old one (see
    `shared/capability_matrix.py::deprecate_entry`) rather than editing it
    in place, so `CapabilityMatrixChange` below always has a real diff to
    show, and a capability check made yesterday against the row that was
    active THEN stays reconstructable.
    """

    __tablename__ = "capability_matrix_entries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "','".join(s.value for s in CapabilityMatrixEntryStatus) + "')",
            name="ck_capability_matrix_entries_status_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Matches watcher/ceph_client.py::DIAGNOSTIC_COMMANDS's keys for the
    # commands this app already runs (e.g. "ceph_versions"), OR a future
    # action_id/mgr-module identifier not in that closed dict yet -- this
    # table does not itself constrain command_id to a fixed enum, since
    # Pha 0.2's matrix is meant to grow ahead of which commands the app
    # has wired up an executor for.
    command_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The literal command this row documents, e.g. "ceph orch upgrade
    # start" -- denormalized copy for the admin page/audit trail to read
    # without cross-referencing DIAGNOSTIC_COMMANDS, which may not even
    # have an entry for a not-yet-wired-up command.
    inner_command: Mapped[str] = mapped_column(Text, nullable=False)
    flag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    module: Mapped[str | None] = mapped_column(String(128), nullable=True)
    backend: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Inclusive major-version range this row applies to. max_major=None
    # means "still supported as of the newest release verified_by checked"
    # -- NOT "supported forever" (a later verification that finds it
    # dropped writes a new bounded row + deprecates this one, see
    # deprecate_entry's own docstring).
    min_major: Mapped[int] = mapped_column(Integer, nullable=False)
    max_major: Mapped[int | None] = mapped_column(Integer, nullable=True)
    doc_url: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    verified_by: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CapabilityMatrixEntryStatus.ACTIVE.value
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class CapabilityMatrixChange(Base):
    """Append-only audit trail of every `CapabilityMatrixEntry` create/
    deprecate -- same "who changed what, when" posture as
    `TelegramChannelConfigChange` above, required by Pha 0.2's own DoD
    ("có ... người duyệt và lịch sử thay đổi"). `entry_snapshot_json` holds
    the full entry state AFTER the change (not a diff) so history stays
    readable even if `CapabilityMatrixEntry` gains/loses columns later.
    """

    __tablename__ = "capability_matrix_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entry_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("capability_matrix_entries.id"), nullable=False, index=True
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)  # CREATED / DEPRECATED
    entry_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class LogIngestStatus(str, enum.Enum):
    """Outcome of ONE `watcher/log_intel.py::scan_and_store` tick.

    PARTIAL is a first-class state, not an error: one unreachable node must
    never abort a whole scan (same per-node best-effort posture
    `watcher/collector.py::collect_relevant_logs` already has), but the
    incompleteness has to be RECORDED -- the AI layer (step L2) is required
    to answer INSUFFICIENT_EVIDENCE rather than guess when the window it is
    reasoning over was only partially collected (Plan/log-intelligence-rca-
    plan.md, constraint R3 / roadmap section 3.1).
    """

    OK = "OK"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class LogIngestRun(Base):
    """One row per log-collection tick (append-only) -- the provenance
    record every later finding traces back to for "which window, which
    source, how complete".

    Deliberately stores COUNTS and STATUS only, never the log lines
    themselves: this app's own database size is a monitored, alerting
    resource (`watcher/database_capacity_monitor.py`), so raw log text
    stays at its source (the node's own file, or Loki) and only the
    fingerprints/counts derived from it are persisted here. See the plan's
    constraint R1.
    """

    __tablename__ = "log_ingest_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "','".join(s.value for s in LogIngestStatus) + "')",
            name="ck_log_ingest_runs_status_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clusters.id"), nullable=False, index=True
    )
    # "ssh" / "loki" -- which watcher/log_source/ adapter produced this run.
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    hosts_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hosts_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lines_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    patterns_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    patterns_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # L1: số mẫu tầng triage (watcher/log_triage.py) gắn cờ trong lần quét
    # này. Nullable vì mọi dòng ghi TRƯỚC khi L1 tồn tại không có giá trị
    # này, và 0 ("đã kiểm tra, không có gì bất thường") phải phân biệt được
    # với NULL ("lần quét đó chưa hề có tầng triage") -- cùng nguyên tắc
    # "chưa đo được khác với đo ra 0" mà TriageResult.baseline_mean dùng.
    patterns_flagged: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Why a PARTIAL/FAILED run was incomplete -- the per-host error text,
    # so an operator sees the reason without digging through watcher logs
    # (same role as ClusterCapabilityInventory.error_message above).
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )


class LogPatternTriageLabel(str, enum.Enum):
    """Operator-controlled verdict on a fingerprint, used by step L1's
    triage to decide what is even worth looking at.

    - UNKNOWN: default for a freshly discovered pattern.
    - BENIGN: operator marked it as expected noise -- triage skips it
      forever after, WITHOUT a code change (the plan's own reason for
      making this a data field rather than a hardcoded ignore list).
    - NOTABLE: operator marked it as always worth surfacing, even if its
      rate looks unremarkable.
    """

    UNKNOWN = "UNKNOWN"
    BENIGN = "BENIGN"
    NOTABLE = "NOTABLE"


class LogPattern(Base):
    """One distinct log-line SHAPE (a "fingerprint"), after every variable
    part -- timestamps, thread ids, addresses, osd/pg ids, uuids, numbers --
    has been replaced by a placeholder.

    This is the table that makes AI analysis affordable at all: a scan
    window holding millions of raw lines normally collapses to a few
    hundred rows here, and step L2 sends the AI these templates plus their
    counts rather than the raw log (plan constraint R4). Fingerprinting
    itself is fully deterministic -- no model involved -- so it stays
    correct and free regardless of whether the AI layer is even enabled.
    """

    __tablename__ = "log_patterns"
    __table_args__ = (
        UniqueConstraint("cluster_id", "fingerprint", name="uq_log_patterns_cluster_fingerprint"),
        CheckConstraint(
            "triage_label IN ('" + "','".join(s.value for s in LogPatternTriageLabel) + "')",
            name="ck_log_patterns_triage_label_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clusters.id"), nullable=False, index=True
    )
    # sha1 of (template + daemon_type) -- see log_intel.py::fingerprint_of.
    fingerprint: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # The normalized shape, e.g.
    # "osd.<N> heartbeat_check: no reply from <ADDR> osd.<N> ever on either front or back"
    template: Mapped[str] = mapped_column(Text, nullable=False)
    # mon / mgr / osd / rgw
    daemon_type: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    # Ceph's own numeric priority when parseable (-1 = error), else None.
    severity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # One real line kept verbatim as a human-readable example. Bounded
    # (truncated on write) and REDACTED before storage -- never a full log
    # dump, see constraint R1/R6.
    sample_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    total_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    triage_label: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LogPatternTriageLabel.UNKNOWN.value
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class LogPatternObservation(Base):
    """Per-hour occurrence count of one pattern on one host -- the time
    series step L1's baseline/burst detection reads back ("is this rate
    normal for this hour of the week, or 5x what it usually is").

    Bucketed by HOUR rather than stored per line, on purpose: this is the
    only table in this feature that grows with log VOLUME rather than log
    VARIETY, so it carries its own, much shorter retention
    (`log_intel_observation_retention_days`, default 30) -- see constraint
    R1 and `watcher/database_capacity_monitor.py`.
    """

    __tablename__ = "log_pattern_observations"
    __table_args__ = (
        UniqueConstraint(
            "pattern_id", "bucket_hour", "host", name="uq_log_pattern_observations_bucket"
        ),
        Index("ix_log_pattern_observations_bucket_hour", "bucket_hour"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pattern_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("log_patterns.id"), nullable=False, index=True
    )
    # Truncated to the top of the hour (UTC) the lines fell in.
    bucket_hour: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    host: Mapped[str] = mapped_column(String(64), nullable=False)
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class LogFindingVerdict(str, enum.Enum):
    """Kết luận của tầng phân tích AI (L2) cho một cửa sổ log.

    `INSUFFICIENT_EVIDENCE` là công dân hạng nhất, không phải trường hợp
    lỗi: roadmap mục 3.1 buộc AI phải trả nó khi evidence thiếu/quá cũ thay
    vì đoán nguyên nhân. Nó cũng là chỗ server HẠ CẤP một câu trả lời không
    qua được kiểm tra (bịa evidence id, lần quét PARTIAL...) -- xem
    watcher/log_analysis.py::_validate.
    """

    FINDING = "FINDING"
    NO_FINDING = "NO_FINDING"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class LogFindingSeverity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class LogFindingConfidence(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class LogFindingStatus(str, enum.Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class LogFinding(Base):
    """Một kết luận của AI về một cửa sổ log (Log Intelligence L2).

    Mọi câu khẳng định phải neo được vào evidence thật: `evidence_pattern_ids`
    trỏ ngược về `log_patterns` và được server kiểm tra là CÓ THẬT và thuộc
    đúng cửa sổ này trước khi hàng được ghi -- một finding trích dẫn mẫu
    không tồn tại sẽ bị hạ xuống INSUFFICIENT_EVIDENCE, không được lưu như
    một kết luận bình thường (roadmap mục 6.3: "không bịa timeline").

    `recommended_action_id` đã đi qua allowlist của
    `worker/policy/action_policy.yaml` VÀ bị cấm tuyệt đối nếu rơi vào nhóm
    DESTRUCTIVE -- xem watcher/log_analysis.py::_validated_action_id. Đây
    chỉ là GỢI Ý đọc: không có đường nào từ bảng này chạy thẳng ra cụm, mọi
    hành động thật vẫn phải qua pipeline Incident/Action/Duyệt sẵn có (plan,
    ràng buộc R5).
    """

    __tablename__ = "log_findings"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('" + "','".join(v.value for v in LogFindingVerdict) + "')",
            name="ck_log_findings_verdict_valid",
        ),
        CheckConstraint(
            "status IN ('" + "','".join(s.value for s in LogFindingStatus) + "')",
            name="ck_log_findings_status_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("clusters.id"), nullable=False, index=True
    )
    # Lần quét đã sinh ra finding này -- provenance: từ đó suy ra cửa sổ
    # thời gian, nguồn log, và (quan trọng nhất) lần quét đó có đầy đủ hay
    # chỉ PARTIAL.
    ingest_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("log_ingest_runs.id"), nullable=False, index=True
    )
    verdict: Mapped[str] = mapped_column(String(24), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_cause_hypothesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON list[str] -- id của LogPattern, đã kiểm tra tồn tại thật.
    evidence_pattern_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON list[str] -- host, đã đối chiếu với danh sách node đã cấu hình.
    affected_hosts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    affected_daemons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Identity do catalogue phía server sinh; không lấy trực tiếp từ AI.
    fault_family: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    semantic_entities_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlated_incident_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("incidents.id"), nullable=True, index=True
    )
    correlation_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    correlated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    correlation_evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Chỉ được đặt khi vượt qua allowlist VÀ không thuộc nhóm DESTRUCTIVE.
    recommended_action_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # JSON list[str] -- các bước thủ công, dạng văn bản thuần, không bao giờ
    # là câu lệnh để chạy (AI không được phép sinh lệnh, xem plan R3).
    recommended_manual_steps_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Chống lặp cảnh báo ở L3: cùng bộ mẫu evidence -> cùng khoá.
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LogFindingStatus.OPEN.value
    )
    # Truy vết được model nào/prompt nào đã kết luận -- bắt buộc khi kết
    # luận của AI được đem ra trước người vận hành.
    model_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Lý do server hạ cấp/sửa câu trả lời của model (bịa evidence id, đề
    # xuất action_id không hợp lệ, lần quét PARTIAL...). Có giá trị nghĩa là
    # đã có ít nhất một lần can thiệp -- đọc được ngay trên Dashboard thay
    # vì phải lục log.
    validation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )


class LogLearningSample(Base):
    """Auditable supervised-learning projection of one LogFinding.

    The raw log remains in Loki.  This row stores only server-owned identity,
    hashes/provenance and the later deterministic remediation outcome.
    """

    __tablename__ = "log_learning_samples"
    __table_args__ = (
        UniqueConstraint("log_finding_id", name="uq_log_learning_samples_finding"),
        Index("ix_log_learning_samples_scope", "cluster_id", "daemon_type", "fault_family"),
        Index("ix_log_learning_samples_eligibility", "eligible_for_learning", "label"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(String(36), ForeignKey("clusters.id"), nullable=False)
    log_finding_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("log_findings.id"), nullable=False
    )
    ingest_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("log_ingest_runs.id"), nullable=False
    )
    incident_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("incidents.id"), nullable=True
    )
    remediation_case_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("remediation_cases.id"), nullable=True
    )
    action_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("actions.id"), nullable=True)
    daemon_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    daemon_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fault_family: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    entity_key: Mapped[str] = mapped_column(String(255), nullable=False, default="unknown")
    pattern_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ingest_status: Mapped[str] = mapped_column(String(16), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    semantic_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    diagnosis_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    recommended_playbook_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    playbook_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="CANDIDATE")
    label: Mapped[str] = mapped_column(String(32), nullable=False, default="UNVERIFIED")
    eligible_for_learning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclusion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    regressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class LogFaultStat(Base):
    """Idempotent trust aggregate for verified daemon-log learning samples."""

    __tablename__ = "log_fault_stats"
    __table_args__ = (
        UniqueConstraint(
            "cluster_id", "daemon_type", "fault_family", "playbook_id",
            "playbook_version", name="uq_log_fault_stats_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(String(36), ForeignKey("clusters.id"), nullable=False)
    daemon_type: Mapped[str] = mapped_column(String(32), nullable=False)
    fault_family: Mapped[str] = mapped_column(String(64), nullable=False)
    playbook_id: Mapped[str] = mapped_column(String(64), nullable=False, default="observation_only")
    playbook_version: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inconclusive_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trust_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    promotion_candidate_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    promotion_blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class NodeResourceForecastRun(Base):
    """Auditable forecast plus the later observed outcome from Loki."""

    __tablename__ = "node_resource_forecast_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_node_resource_forecast_idempotency"),
        Index("ix_node_resource_forecast_due", "status", "target_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(8), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False, default="linear")
    window_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    predicted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    target_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    current_percent: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_percent: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    actual_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    absolute_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class NodeResourceModelState(Base):
    """Online MAE state used to choose a forecast window per node/metric."""

    __tablename__ = "node_resource_model_states"
    __table_args__ = (
        UniqueConstraint(
            "cluster_name", "host", "metric", "algorithm", "window_hours",
            name="uq_node_resource_model_identity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(8), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False, default="linear")
    window_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mean_absolute_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_absolute_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class NodeResourceForecastAlert(Base):
    """Durable Telegram lifecycle for a risky CPU/RAM forecast."""

    __tablename__ = "node_resource_forecast_alerts"
    __table_args__ = (
        UniqueConstraint(
            "cluster_name", "host", "metric",
            name="uq_node_resource_forecast_alert_identity",
        ),
        Index("ix_node_resource_forecast_alert_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    first_detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_percent: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_percent: Mapped[float] = mapped_column(Float, nullable=False)
    hours_to_90: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    samples: Mapped[int] = mapped_column(Integer, nullable=False)
    window_hours: Mapped[int] = mapped_column(Integer, nullable=False)
