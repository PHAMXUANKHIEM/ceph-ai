import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Exposed as module-level constants (not just inline defaults) so other code
# — e.g. the startup check in dashboard/app.py — can detect "still using the
# dev default" without duplicating these literals.
DEFAULT_DASHBOARD_PASSWORD_HASH = "$2b$12$G9OqbEMaoR6ROfQpQWbNrOgfzlDhBy7Z5fRFGezCr89SqqTkqFWlm"
DEFAULT_SESSION_SECRET_KEY = "dev-only-insecure-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=os.environ.get("CEPH_AI_ENV_FILE", ".env"), extra="forbid")

    database_url: str = "sqlite:///./ceph_aiops.db"
    rabbitmq_url: str = "amqp://guest:guest@localhost/"

    # Dashboard auth (single static account — AD from Architecture, no RBAC in v1).
    # Defaults are a dev-only convenience (username "admin", password "admin")
    # so the demo runs out of the box; override both via .env before any real use.
    dashboard_username: str = "admin"
    dashboard_password_hash: str = DEFAULT_DASHBOARD_PASSWORD_HASH
    session_secret_key: str = DEFAULT_SESSION_SECRET_KEY

    # SSH access to the Ceph cluster nodes (dedicated keypair, no passphrase
    # so the services can run unattended) — shared by BOTH Watcher (read-only
    # health/log queries) and Worker (SSH Executor, remediation commands).
    # One shared key by deliberate choice, not two separate ones — avoids
    # asking the operator to deploy a second public key to every node.
    # Deliberately blank — no cluster is configured out of the box; set
    # these via the Settings page (or .env) for the cluster you're
    # actually connecting to.
    ssh_key_path: str = ""
    ssh_user: str = "root"
    ceph_mon_nodes: str = ""
    ceph_mon_hostnames: str = ""
    ceph_container_name: str = ""
    ceph_keyring_path: str = "/etc/ceph/ceph.client.admin.keyring"
    # How `ceph`/log commands are invoked on a node — clusters aren't all
    # deployed the same way:
    #   "docker"  — `docker exec {container} <cmd>` (a plain `docker run` cluster)
    #   "podman"  — `podman exec {container} <cmd>` (a plain `podman run` cluster
    #               with a fixed, known container name — NOT cephadm; a real
    #               cephadm mon container has no admin keyring mounted and
    #               its name is auto-generated/per-host, so this mode does
    #               not work for cephadm — use "cephadm" below instead)
    #   "cephadm" — `cephadm shell -- <cmd>` — cephadm infers the right
    #               fsid/config/keyring itself, no container name needed at
    #               all (verified against a real cephadm/reef cluster)
    #   "none"    — `<cmd>` run directly, no container (ceph-deploy / package
    #               install with ceph binaries native on the host)
    # ceph_container_name/ceph_osd_container_name are ignored in "cephadm"
    # and "none" modes.
    ceph_exec_mode: str = "docker"
    watcher_poll_interval_seconds: int = 15
    # Repeat Telegram notifications while an Incident remains unresolved.
    telegram_incident_reminder_interval_seconds: int = 3600
    telegram_health_status_interval_seconds: int = 600

    # --- Xác minh sau khắc phục (2026-08-20, watcher/verify.py) ----------
    # Chờ bao lâu sau khi lệnh khắc phục chạy xong rồi mới hỏi lại cụm.
    # KHÔNG kiểm ngay: rất nhiều lỗi cần thời gian mới hết (PG backfill
    # xong, OSD vào lại quorum, mon clock skew hội tụ) nên kiểm tức thì sẽ
    # luôn ra "chưa hết" một cách giả tạo, rồi kéo theo một vòng chẩn đoán
    # lại hoàn toàn vô ích. 5 phút là mức thận trọng cho một cụm lab; cụm
    # lớn nên nới thêm.
    incident_verify_delay_seconds: int = 300
    # Trần số vòng "kiểm chứng -> chưa hết -> nhờ AI chẩn đoán lại". Chạm
    # trần thì Incident chuyển FAILED và Telegram báo cần người vào, thay vì
    # lặp mãi. Có những lỗi không bao giờ tự hết bằng một lệnh (CRUSH skew
    # cần người cân lại weight) — với chúng, mỗi vòng thêm chỉ tốn một lần
    # gọi router và một loạt thông báo.
    incident_verify_max_attempts: int = 2
    # Dedicated safety-critical watcher; intentionally independent from the
    # heavier inventory/RBD/Log Intelligence loop in watcher.main.
    ai_remediation_poll_interval_seconds: int = 10
    # POSIX advisory lock for the dedicated remediation watcher.  The lock
    # lives under the systemd runtime directory and is released by the
    # kernel even when the process exits uncleanly.
    ai_remediation_lock_file: str = "/run/ceph-ai/remediation-watcher.lock"

    # OSD/data node access — used by watcher/collector.py to fetch OSD daemon
    # logs (Story 1.4). Same cluster as ceph_mon_nodes above; also blank by
    # default (OSD log collection is optional — see Settings page AC #1).
    ceph_osd_nodes: str = ""
    ceph_osd_container_name: str = ""

    # MGR node access — used to route log collection for MGR-related health
    # checks (ceph_code starting "MGR_") to the right node(s), the same way
    # OSD_/PG_ codes already route to ceph_osd_nodes. Optional — a cluster
    # with no MGR_-family incidents yet doesn't need this configured.
    ceph_mgr_nodes: str = ""

    # RGW node access — used by the Dashboard's per-node "Log RGW" panel
    # (watcher/rgw_log.py) to tail radosgw daemon logs on demand. Same
    # cluster as ceph_mon_nodes above; optional, like ceph_mgr_nodes — a
    # cluster with no RGW gateway deployed doesn't need this configured.
    # ceph_rgw_container_name is only used in docker/podman exec mode
    # (ignored for "cephadm"/"none", same as ceph_osd_container_name).
    ceph_rgw_nodes: str = ""
    ceph_rgw_container_name: str = ""

    # 2026-07-28: RBD pools to poll for per-image performance (IOPS/latency)
    # and saturation detection (watcher/volume_monitor.py) — comma-separated
    # pool names, e.g. "vms,volumes". Blank by default, and blank does NOT
    # mean "disabled" — watcher/ceph_client.py::configured_rbd_pools() auto-
    # discovers every pool with the RBD application enabled when this is
    # left empty, so the feature works with zero manual setup on a normal
    # cluster. Only set this to RESTRICT polling to an explicit subset (SSH/
    # poll cost control, or a pool you deliberately don't want watched) —
    # once set, it's the ONLY list used, auto-discovery is skipped entirely.
    ceph_rbd_pools: str = ""
    # Minimum recovery window before a soft-deleted RBD image becomes
    # eligible for permanent removal. Restore remains available throughout.
    rbd_trash_retention_days: int = 7

    # 2026-08-01 (Story C, DeviceHealth-driven evacuation proposals) —
    # watcher/device_health_monitor.py's own scan cadence, deliberately
    # separate from watcher_poll_interval_seconds above: a device's
    # predicted life expectancy doesn't change meaningfully every 15s the
    # way cluster health does, so running `ceph device ls`/`ceph osd dump`
    # that often would just be wasted MON load. 1 hour by default.
    device_health_scan_interval_seconds: int = 3600
    # A device whose life_expectancy_min falls within this many days from
    # now is proposed for evacuation (mark_osd_out) — see that module's own
    # docstring for why life_expectancy_min (the EARLIEST possible failure
    # date in Ceph's own [min, max] prediction range) is the conservative
    # choice here, not life_expectancy_max.
    device_health_evacuate_threshold_days: int = 7

    # 2026-08-06: watcher/bluestore_omap_monitor.py's own scan cadence, same
    # reasoning as device_health_scan_interval_seconds above — resolving
    # which configured OSD host runs a given osd_id needs one SSH round trip
    # per configured OSD host (probing its own systemd units, see that
    # module's docstring for why), and BLUESTORE_NO_PER_POOL_OMAP doesn't
    # newly appear/disappear on a sub-minute timescale. 15 minutes by
    # default, matching node_health_scan_interval_seconds below.
    bluestore_omap_scan_interval_seconds: int = 900

    # 2026-07-24: Ceph patch build & deploy pipeline (dashboard/routes/patch.py)
    # — a separate build server, NOT a Ceph cluster node, so deliberately not
    # part of the ceph_mon/osd/mgr/rgw_nodes family above or
    # shared/cluster_nodes.py::configured_nodes()'s SSH SSRF whitelist (see
    # that function's docstring). Reuses the same ssh_user/ssh_key_path as
    # every other SSH target in this app (one shared key, same posture
    # documented above for the Ceph node fields) — the operator additionally
    # needs to place a copy of that same private key file on the build
    # server itself (one-time manual step, since the build server does its
    # OWN scp to each Ceph node — see _patch_build_and_stage_command's
    # docstring in worker/executor/commands.py for why there's no built-in
    # file-transfer mechanism in this codebase to do that instead).
    ceph_patch_build_node: str = ""
    # Path to the Ceph git checkout on the build server — the uploaded patch
    # is applied here (`git apply`) before ceph_patch_build_command runs.
    ceph_patch_source_dir: str = ""
    # Operator-authored shell command(s) that actually produce the .rpm
    # files, run inside ceph_patch_source_dir right after the patch is
    # applied — e.g. whatever build script/rpmbuild invocation the operator
    # already runs by hand today. This codebase has no built-in knowledge of
    # Ceph's build process (unlike upgrade_ceph_cluster_package_download's
    # hardcoded, distro-generic download.ceph.com repo commands) — it's
    # specific to this operator's toolchain, same reasoning ceph_mon_nodes
    # etc. are operator-typed rather than auto-discovered.
    ceph_patch_build_command: str = ""
    # Where the .rpm files land on the build server once
    # ceph_patch_build_command finishes.
    ceph_patch_output_dir: str = ""
    # Fixed scratch directory on EACH Ceph node where the build server copies
    # the built .rpm files to, before patch_install runs `rpm`/`yum
    # localinstall` from this same path — an app-owned convention (unlike
    # the fields above, not something the operator is expected to already
    # have an existing value for), so it ships with a sensible default.
    ceph_patch_node_staging_dir: str = "/opt/ceph-aiops-patch-staging"

    # Worker (Story 2.1): total number of processing attempts (including the
    # first) before an Incident is marked FAILED and its message dead-lettered
    # — NOT the count of retries after the first attempt.
    worker_max_retries: int = 3

    # API AI (2026-07-24: renamed from "9router" on the Settings page) — the
    # single AI backend for both incident diagnosis
    # (worker/llm/router_client.py) and the Chat-with-AI feature
    # (dashboard/chat_client.py). Every call still goes through ONE
    # configured endpoint at a time (see shared/router_client.py::
    # RouterNotConfiguredError) — router_api_key/router_base_url/
    # router_model below are unchanged and provider-agnostic on purpose:
    # shared/router_client.py builds a plain AsyncOpenAI client from
    # whatever base_url+api_key are configured, and Claude/Codex/OpenRouter
    # all speak that same OpenAI-compatible chat-completions shape (Claude
    # via Anthropic's own OpenAI-SDK-compatibility endpoint), same as a
    # self-hosted 9router does. router_provider only remembers which of the
    # Settings page's connection-type presets (see dashboard/routes/
    # settings.py's PROVIDER_PRESETS) was picked, so the page can show the
    # right label/preset base URL again — it has no effect on how the
    # client is built. Empty by default — live tests skip gracefully when
    # unset (see tests/test_router_client_live.py).
    router_provider: str = "9router"
    router_api_key: str = ""
    router_base_url: str = ""
    # Free text, not a closed dropdown — populated on the Settings page from
    # a real GET /v1/models call against router_base_url (whatever ids that
    # specific router happens to expose), not a hardcoded list.
    router_model: str = ""
    # True only after a real verify-then-save round trip on the Settings
    # page has succeeded at least once — lets the Dashboard/chatbox
    # distinguish "not configured yet" from "configured but temporarily
    # unreachable" without treating a blank api_key as the only signal.
    router_enabled: bool = False

    # Optional unit prices used only by the read-only AI Cost dashboard. The
    # telemetry layer stores content-free sizes, so the dashboard labels token
    # and USD values as estimates until providers expose usage metadata.
    ai_cost_input_usd_per_million_tokens: float = 0.0
    ai_cost_output_usd_per_million_tokens: float = 0.0
    # Reference conversion for the read-only cost dashboard. Update when the
    # chosen accounting exchange rate changes; it does not alter USD costs.
    ai_cost_usd_to_vnd: float = Field(default=26290.0, gt=0)
    # Runtime cache populated by scripts.update_ai_pricing. The cache is
    # deliberately outside git because it is refreshed from the network.
    ai_cost_pricing_cache_path: str = ".ai-pricing.json"
    # Optional AI Budget Guard. Zero disables the corresponding limit. Costs
    # are estimates from content-free telemetry and are evaluated in UTC.
    ai_cost_daily_budget_usd: float = Field(default=0.0, ge=0)
    ai_cost_monthly_budget_usd: float = Field(default=0.0, ge=0)
    ai_cost_budget_hard_limit: bool = False
    # Reserved output tokens used before a call, since output usage is not
    # known until the provider returns. This is deliberately conservative.
    ai_cost_budget_reserve_output_tokens: int = Field(default=2048, ge=0, le=100000)

    # Weekly read-only health digest. It is sent only through configured
    # alert channels and never creates or executes an Action.
    ai_ops_weekly_digest_enabled: bool = True
    ai_ops_weekly_digest_day: str = "mon"
    ai_ops_weekly_digest_hour: int = Field(default=8, ge=0, le=23)
    ai_ops_weekly_digest_minute: int = Field(default=0, ge=0, le=59)

    # Vitastor is a separate product workspace, so its chat connection must
    # not silently inherit or overwrite the Ceph AI provider configuration.
    vitastor_router_provider: str = "9router"
    vitastor_router_api_key: str = ""
    vitastor_router_base_url: str = ""
    vitastor_router_model: str = ""
    vitastor_router_enabled: bool = False
    vitastor_codex_chat_enabled: bool = False
    vitastor_claude_chat_enabled: bool = False
    # Optional Vitastor-specific model overrides. Blank preserves the shared
    # Ceph model setting while allowing this product workspace to diverge.
    vitastor_codex_chat_model: str = ""
    vitastor_claude_chat_model: str = ""

    # Optional ChatGPT subscription-backed Codex connection used by the
    # dashboard chat only.  Credentials are owned/refreshed by Codex CLI in
    # codex_home; ceph-ai never copies OAuth tokens into .env or the DB.
    codex_chat_enabled: bool = False
    codex_home: str = ".codex-account"
    # Blank lets Codex choose the account/CLI default from its live catalog.
    codex_chat_model: str = ""
    # Optional Claude subscription/Console-backed CLI connection. OAuth
    # credentials stay in this private Claude-owned directory.
    claude_chat_enabled: bool = False
    claude_config_dir: str = ".claude-account"
    # Claude CLI accepts stable family aliases and explicit version IDs.
    claude_chat_model: str = "default"
    # "auto" leaves the effort choice to the selected model/Claude Code.
    claude_chat_effort: str = "auto"

    # Worker (Story 4.3): how often the Worker checks for Actions an
    # operator just approved on the Dashboard. Separate from RabbitMQ
    # entirely — an approval isn't a queue message, so nothing redelivers
    # it; the Worker has to notice it itself.
    worker_approval_poll_interval_seconds: int = 5

    # Epic 9 (Story 9.2): backup destination credentials — TWO fixed slots
    # (a/b), not a dynamic per-target scheme. `Settings.model_config` above
    # has `extra="forbid"` on this SAME `.env` file — verified directly with
    # pydantic-settings that ANY undeclared key present in `.env` raises
    # ValidationError at `Settings()` construction (module import time),
    # crashing the whole app. A dynamic `BACKUP_TARGET_<name>_*` naming
    # scheme would violate that the moment an operator adds a target, so
    # every field a BackupTarget could need is declared explicitly here
    # instead. Two slots is enough for PRD FR-5's "minimum 2 copies
    # outside the source cluster" — supporting more is Deferred (see
    # Architecture Spine's Deferred section).
    #
    # Deliberately SEPARATE fields from ssh_key_path/ssh_user above (which
    # authenticate to the SOURCE cluster) — PRD FR-4 requires the backup
    # destination to never share credentials/network access with the
    # source cluster's admin path.
    backup_target_a_transport: str = ""  # "ssh" | "s3" | "" (unconfigured)
    backup_target_a_label: str = ""
    backup_target_a_ssh_host: str = ""
    backup_target_a_ssh_user: str = ""
    backup_target_a_ssh_key_path: str = ""
    backup_target_a_ssh_landing_dir: str = ""
    backup_target_a_s3_endpoint: str = ""
    backup_target_a_s3_access_key: str = ""
    backup_target_a_s3_secret_key: str = ""
    backup_target_a_s3_bucket: str = ""
    # Minimum days an S3 Object Lock-protected object (or, for the ssh
    # backend, the out-of-band mechanism on the destination host) must
    # resist deletion — see Architecture AD-10. Only meaningful when this
    # slot is the designated immutable copy (worker/policy/backup_policy.yaml
    # decides which slot that is, not this field itself).
    backup_target_a_immutable_lock_days: int = 7

    backup_target_b_transport: str = ""
    backup_target_b_label: str = ""
    backup_target_b_ssh_host: str = ""
    backup_target_b_ssh_user: str = ""
    backup_target_b_ssh_key_path: str = ""
    backup_target_b_ssh_landing_dir: str = ""
    backup_target_b_s3_endpoint: str = ""
    backup_target_b_s3_access_key: str = ""
    backup_target_b_s3_secret_key: str = ""
    backup_target_b_s3_bucket: str = ""
    backup_target_b_immutable_lock_days: int = 7

    # Epic 9 (Story 9.4): outbound webhook for backup fail/overdue alerts —
    # the FIRST such channel in this project (no Slack/SMTP/Telegram exists
    # anywhere else in the codebase, verified before adding this). Blank
    # (default) = disabled; worker/backup/alerting.py always logs the alert
    # regardless, this only controls whether it ALSO POSTs a JSON payload
    # to an external endpoint.
    backup_alert_webhook_url: str = ""

    # Shared outbound notification channels. Blank endpoints/host disable
    # each channel independently; delivery is best-effort and never blocks
    # the watcher/worker operation that produced the alert.
    alert_webhook_url: str = ""
    alert_slack_webhook_url: str = ""
    alert_email_smtp_host: str = ""
    alert_email_smtp_port: int = 587
    alert_email_smtp_username: str = ""
    alert_email_smtp_password: str = ""
    alert_email_from: str = ""
    alert_email_to: str = ""
    alert_email_starttls: bool = True

    # 2026-08-07: label identifying WHICH Ceph cluster an alert came from —
    # for operators running several ceph-aiops instances (one per cluster)
    # that all point their Telegram channels at the SAME chat(s), so alerts
    # from different clusters don't look identical in that shared chat.
    # Deliberately ONE field shared by all 3 channels below (not per-
    # channel) — one ceph-aiops instance always monitors exactly one
    # cluster, so there's only ever one name to distinguish. Blank by
    # default (single-cluster deployments don't need it) — every message
    # builder in shared/telegram_alerts.py, worker/backup/alerting.py, and
    # dashboard/telegram_approval_bot.py skips the prefix entirely when
    # this is empty, so existing single-cluster message text is unchanged.
    cluster_name: str = ""

    # 2026-08-06: Telegram alert delivery split into 3 fully INDEPENDENT
    # channels — Backup, Lỗi cụm (cluster health), Phần cứng (node CPU/RAM)
    # — each with its OWN Bot Token + Chat ID (previously all 3 shared one
    # pair).
    #
    # 2026-08-07: brought back a SEPARATE per-channel `_enabled` flag
    # (operator request) — the original "configured = both token+chat id
    # non-blank IS the on/off switch, no separate flag" design (see git
    # history) meant pausing a channel required BLANKING its Chat ID, so
    # re-enabling it later meant retyping/re-pasting that Chat ID instead of
    # a single click. Each `_enabled` field defaults to True so an existing
    # deployment's already-configured channels keep behaving exactly as
    # before until the operator explicitly flips one off — a channel is
    # only actually active when BOTH "configured" (token+chat id non-blank)
    # AND `_enabled` are true; see shared/telegram_alerts.py::_send,
    # worker/backup/alerting.py::_send_telegram_alert, and
    # dashboard/telegram_approval_bot.py::_configured_channels for the 3
    # places that check both.
    #
    # Yêu cầu phê duyệt qua Telegram (Duyệt/Từ chối an Action from an
    # inline-keyboard button, dashboard/telegram_approval_bot.py) is no
    # longer its own 4th toggle — it is now a DEFAULT capability of EVERY
    # channel that is BOTH configured AND enabled below: a PENDING_APPROVAL
    # Action's Duyệt/Từ chối request is broadcast to every such channel
    # simultaneously. Anyone in ANY of these chats can approve/reject any
    # pending RISKY action — see that module's own docstring for the full
    # design and TRUST MODEL before configuring a group chat here.
    #
    # Backup alerts here are a SECOND, independent channel alongside
    # backup_alert_webhook_url above, not a replacement for it.
    telegram_backup_bot_token: str = ""
    telegram_backup_chat_id: str = ""
    telegram_backup_enabled: bool = True
    telegram_incident_bot_token: str = ""
    telegram_incident_chat_id: str = ""
    telegram_incident_enabled: bool = True
    # Performance RCA has its own alert switch while reusing the incident
    # channel credentials. This does not affect ordinary incident alerts.
    telegram_performance_rca_enabled: bool = True
    # Dedicated notification-only channel for early RBD forecasts.
    telegram_rbd_forecast_bot_token: str = ""
    telegram_rbd_forecast_chat_id: str = ""
    telegram_rbd_forecast_enabled: bool = True
    telegram_node_bot_token: str = ""
    telegram_node_chat_id: str = ""
    telegram_node_enabled: bool = True
    # Notification-only channel for repairs to the ceph-ai application.
    telegram_code_repair_bot_token: str = ""
    telegram_code_repair_chat_id: str = ""
    telegram_code_repair_enabled: bool = True
    # Dashboard two-agent chat. Independent from log-triggered repair.
    dual_ai_fallback_enabled: bool = False
    dual_ai_planner_provider: str = "auto"
    dual_ai_planner_model: str = ""
    # Comma-separated fallback entries: provider[:model], tried only after
    # quota/rate-limit/token exhaustion (for example claude:claude-sonnet-4-6).
    dual_ai_planner_fallbacks: str = ""
    dual_ai_implementer_provider: str = "auto"
    dual_ai_implementer_model: str = ""
    dual_ai_implementer_fallbacks: str = ""
    # External supervisor for application self-repair. Disabled by default;
    # staging may explicitly enable the full test/deploy/promote pipeline.
    code_repair_auto_enabled: bool = False
    code_repair_poll_interval_seconds: int = 30
    code_repair_provider: str = "auto"
    # Two-agent supervisor roles. Planner/Reviewer is read-only and produces
    # the plan plus independent review; Implementer edits the isolated repair
    # worktree. Keep the legacy provider above for backward compatibility.
    code_repair_planner_provider: str = "auto"
    code_repair_planner_model: str = ""
    code_repair_planner_account_source: str = "configured"
    code_repair_planner_account_profile: str = ""
    code_repair_implementer_provider: str = "auto"
    code_repair_implementer_model: str = ""
    code_repair_implementer_account_source: str = "configured"
    code_repair_implementer_account_profile: str = ""
    code_repair_max_review_rounds: int = Field(default=2, ge=0, le=5)
    code_repair_test_command: str = (
        "PYTHONPATH=. .venv/bin/pytest -q "
        "--ignore=tests/test_migrations.py --ignore=tests/test_mq.py "
        "--deselect=tests/test_dashboard_settings.py::test_require_admin_privilege_rejects_unknown_username "
        "--deselect=tests/test_dashboard_settings.py::test_migrate_database_route_adds_missing_table"
    )
    code_repair_timeout_seconds: int = 1800
    code_repair_max_attempts: int = 3
    code_repair_running_stale_seconds: int = 3600
    code_repair_push: bool = False
    code_repair_deploy_staging: bool = False
    code_repair_promote_main: bool = False
    code_repair_cursor_file: str = "/var/lib/ceph-ai/code-repair-cursors.json"
    code_repair_lock_file: str = "/var/lib/ceph-ai/code-repair.lock"
    # Serializes every repair pipeline, including the independent nightly
    # systemd job, while the supervisor lock remains process-scoped.
    code_repair_run_lock_file: str = "/var/lib/ceph-ai/code-repair-run.lock"
    # One proactive, bounded two-agent review per Asia/Ho_Chi_Minh day.
    # The systemd timer owns the clock; the state file makes retries idempotent.
    ai_nightly_improvement_enabled: bool = False
    ai_nightly_improvement_hour: int = Field(default=0, ge=0, le=23)
    ai_nightly_improvement_minute: int = Field(default=0, ge=0, le=59)
    ai_nightly_improvement_state_file: str = "/var/lib/ceph-ai/nightly-ai-improvement.json"
    # Dashboard may set a one-day override. The date makes this expire
    # automatically instead of silently affecting later nightly runs.
    ai_nightly_improvement_override_date: str = ""
    ai_nightly_improvement_override_enabled: bool = False
    ai_task_retention_days: int = Field(default=30, ge=1, le=3650)
    ai_task_max_records: int = Field(default=500, ge=10, le=10000)
    ceph_capability_learning_enabled: bool = False
    ceph_capability_learning_include_existing: bool = False
    ceph_capability_learning_state_file: str = "/var/lib/ceph-ai/ceph-capability-learning.json"
    # Dedicated notification-only channel for AI-analyzed RADOS Gateway
    # findings. Members receive RGW alerts but cannot approve Actions.
    telegram_rgw_bot_token: str = ""
    telegram_rgw_chat_id: str = ""
    telegram_rgw_enabled: bool = True
    # Dedicated two-way Telegram channel for the Dashboard Chatbox AI.
    # This channel is intentionally separate from alert/approval channels;
    # its chat id is the allow-list for incoming operator messages.
    telegram_chatbox_bot_token: str = ""
    telegram_chatbox_chat_id: str = ""
    telegram_chatbox_enabled: bool = True
    # The legacy all-in-one Dashboard starts the Telegram polling threads in
    # its lifespan. Container deployments run those threads in the dedicated
    # `telegram-ai` service instead, so exactly one process owns getUpdates.
    telegram_listener_enabled: bool = True
    # Required for dual execution from a group chat. Private chats are already
    # single-user by Telegram's chat model; group chats must explicitly list
    # the sender IDs allowed to invoke the full-permission Implementer.
    telegram_chatbox_allowed_user_ids: str = ""
    # Separate, mandatory allow-list for the unrestricted Telegram
    # /single-full mode. Empty means the mode is disabled for everyone.
    telegram_chatbox_full_access_user_ids: str = ""
    # Deprecated compatibility field. Each Ceph-AI installation is local-only;
    # Telegram routing never opens another deployment's database, even if an
    # old environment file still contains this setting.
    telegram_federated_database_urls: str = ""
    # Container deployments route Single Full to a separate privileged
    # executor. Empty keeps the legacy in-process behavior for systemd/dev.
    single_full_executor_url: str = ""
    single_full_executor_token: str = ""
    # Per-request RGW audit collector.  It is deliberately independent of
    # Log Intelligence: CRUD access events are facts, not AI findings, and
    # must not be suppressed or grouped by the anomaly/noise pipeline.
    rgw_access_audit_enabled: bool = True
    rgw_access_audit_interval_seconds: int = 15
    # "Lịch sử IP thao tác Bucket/Object" (dashboard/templates/bucket_access_log.html's
    # #bah-* panel) is a per-request audit table, not a bounded log tail — left
    # unbounded it grows with every S3 request the cluster ever serves. Pruned
    # on the same collection tick as ingestion (worker/rgw_access_audit.py::
    # collect_once), same reasoning as log_intel_*_retention_days above.
    rgw_access_audit_retention_days: int = 7
    # dashboard/telegram_approval_bot.py's own DB-scan cadence for newly
    # PENDING_APPROVAL Actions not yet broadcast to every configured
    # channel above — short by design (unlike device_health/node_health's
    # scan intervals above, this is a cheap DB query, not a real SSH round
    # trip, so there's no reason to space it out).
    telegram_approval_scan_interval_seconds: int = 10
    # Independent read-only Vitastor health poll used for Telegram alerts.
    vitastor_poll_interval_seconds: int = 60
    vitastor_metric_retention_days: int = 30
    vitastor_capacity_warning_percent: float = 85.0
    vitastor_capacity_critical_percent: float = 90.0
    vitastor_anomaly_min_samples: int = 20
    vitastor_anomaly_history_samples: int = 500
    vitastor_anomaly_mad_multiplier: float = 6.0
    vitastor_anomaly_relative_multiplier: float = 2.5
    vitastor_etcd_latency_warning_ms: float = 100.0
    vitastor_etcd_latency_critical_ms: float = 500.0
    vitastor_recovery_warning_mbps: float = 500.0
    vitastor_recovery_critical_mbps: float = 1000.0
    vitastor_slow_osd_latency_ms: float = 20.0
    vitastor_slow_osd_median_multiplier: float = 3.0
    vitastor_slow_osd_consecutive_scans: int = 3
    vitastor_disk_temperature_warning_c: float = 65.0
    vitastor_disk_temperature_critical_c: float = 75.0
    vitastor_disk_wear_warning_percent: float = 80.0
    vitastor_disk_wear_critical_percent: float = 95.0
    vitastor_network_rtt_warning_ms: float = 5.0
    vitastor_network_rtt_critical_ms: float = 20.0
    vitastor_expect_jumbo_frames: bool = False
    vitastor_network_max_nodes: int = 32

    # watcher/node_health_monitor.py's own scan cadence — same reasoning as
    # device_health_scan_interval_seconds above: collecting CPU/RAM needs a
    # fresh SSH round trip PER NODE (watcher/node_metrics.py samples /proc
    # twice, ~1s apart, per host), so running that on every
    # watcher_poll_interval_seconds tick (15s default) would be a
    # meaningfully heavier SSH load than the single `ceph health detail`
    # query the main loop already does. 15 minutes by default.
    node_health_scan_interval_seconds: int = 900

    # CPU/RAM forecasting is deliberately backed by Loki rather than the
    # application database.  Watcher pushes each node-health sample as a
    # structured Loki log and reads the history back before calculating a
    # trend.  Disabled by default so an existing SSH-only deployment does
    # not unexpectedly start writing to Loki.
    node_resource_forecast_enabled: bool = False
    # Optional self-contained ingestion path for deployments without Alloy:
    # Watcher samples /proc over its existing read-only SSH path and pushes
    # the fresh CPU/RAM sample to Loki before analysing the node. Keep this
    # opt-in because Alloy remains the preferred lower-overhead source.
    node_resource_live_ingest_enabled: bool = False
    node_resource_forecast_history_days: int = 30
    node_resource_forecast_horizon_hours: int = 168
    node_resource_forecast_min_samples: int = 24
    node_resource_forecast_min_confidence: float = 0.5
    # A forecast must cover enough of its requested training window and may
    # not bridge an excessively long Loki/Alloy outage.
    node_resource_forecast_min_coverage: float = 0.6
    node_resource_forecast_max_gap_hours: float = 6.0
    # Candidate history windows are evaluated against their later outcomes.
    # The lowest-MAE candidate with enough evaluated runs is selected per
    # cluster/host/metric; until then the longest available window wins.
    node_resource_learning_evaluation_hours: int = 24
    # A late scan may only score a forecast against a sample close to its
    # target time; otherwise an outage would corrupt MAE with a later value.
    node_resource_learning_max_outcome_gap_hours: float = 3.0
    node_resource_learning_min_outcomes: int = 3
    node_resource_learning_candidate_hours: str = "24,72,168,720"

    # LARGE_OMAP_OBJECTS auto-remediation is opt-in and bucket-scoped.
    # test-* remains the built-in lab-only path; production buckets must be
    # explicitly allowlisted after verified evidence and outcomes exist.
    large_omap_autoremediation_enabled: bool = False
    large_omap_autoremediation_buckets: str = ""
    large_omap_evidence_max_age_hours: int = 24
    # The first bounded RGW reshard must be operator-approved so its
    # verified post-check can bootstrap trust without an unobserved write.
    large_omap_bootstrap_requires_approval: bool = True
    node_resource_forecast_alert_cooldown_seconds: int = 86400

    # Per-RBD-volume seasonal baseline learning. Predictions are audit-only:
    # they may change the selected baseline, never policy or action rights.
    volume_learning_enabled: bool = True
    volume_learning_history_days: int = 30
    volume_learning_evaluation_hours: int = 1
    volume_learning_min_samples: int = 24
    volume_learning_min_outcomes: int = 10
    volume_learning_candidate_hours: str = "24,72,168,720"
    # Read-only early warning generated from the selected seasonal baseline.
    volume_forecast_enabled: bool = True
    volume_forecast_horizons: str = "1,6,24"
    volume_forecast_min_confidence: float = 0.5
    volume_forecast_max_staleness_minutes: int = 30
    volume_forecast_latency_slo_ms: float = 20.0
    volume_forecast_knee_warning_ratio: float = 0.9

    # watcher/osd_latency_monitor.py's own scan cadence — much SHORTER than
    # device_health/node_health above because `ceph osd perf` is a single
    # cheap JSON-RPC query through a MON (no SSH round trip per node/OSD at
    # all, unlike those two), and a latency spike is far more transient
    # than a slowly-climbing CPU/RAM trend or a predicted disk failure — a
    # 15-60 minute cadence would routinely miss it entirely. 1 minute by
    # default, independent of watcher_poll_interval_seconds (15s) so a
    # transient MON hiccup on this query never affects the main health-check
    # cadence and vice versa.
    osd_latency_scan_interval_seconds: int = 60

    # watcher/crush_structure_monitor.py + watcher/crush_distribution_monitor.py's
    # shared scan cadence (Epic 12, AD-25b) -- both `ceph osd crush dump` and
    # `ceph osd df` are single cheap JSON-RPC queries through a MON, same
    # cost class as `ceph osd perf` above (no SSH round trip, no dependency
    # on the cluster's PG count) -- originally scoped as 2 separate
    # cadences (a slow one for a planned `ceph pg dump` call), collapsed to
    # ONE after confirming `ceph osd df` already reports per-OSD PG count
    # (the `pgs` column) in the same call. 1 minute by default, same as
    # osd_latency_scan_interval_seconds above.
    crush_scan_interval_seconds: int = 60

    # Append-only Ceph capacity history. Forecasts fail closed until the
    # configured minimum time span and sample count are both available.
    capacity_forecast_enabled: bool = True
    capacity_forecast_scan_interval_seconds: int = 3600
    capacity_forecast_history_days: int = 90
    capacity_forecast_min_history_days: int = 30
    capacity_forecast_min_samples: int = 30
    capacity_forecast_horizon_days: int = 365
    capacity_forecast_min_confidence: float = 0.5

    # Minimum model-reported confidence required before Incident diagnosis
    # may create an executable Action. Missing/invalid confidence is rejected;
    # lower confidence remains visible to operators but cannot trigger work.
    ai_min_diagnosis_confidence: float = Field(default=0.6, gt=0, le=1)
    ai_low_confidence_retry_cooldown_seconds: int = Field(default=3600, ge=60)

    # watcher/database_capacity_monitor.py's own cadence -- deliberately
    # much slower than every other scan above: this app's own DB size
    # grows on a scale of days/weeks, not seconds, and checking it every
    # tick would be pure overhead (a file stat for SQLite, or a real
    # round trip to the separate OpenEverest-managed Postgres cluster for
    # everything else) for no operational benefit. 1 hour by default.
    database_size_scan_interval_seconds: int = 3600

    # watcher/capability_inventory.py's own cadence (AI roadmap Pha 0.1) --
    # `ceph versions` is a single cheap JSON-RPC query through a MON (same
    # cost class as osd_latency_scan_interval_seconds/crush_scan_interval_
    # seconds above), but a cluster's version/deployment mode changes on
    # the scale of an upgrade maintenance window, not seconds -- scanning
    # every tick would be pure overhead. 5 minutes by default: frequent
    # enough to catch a mixed-version window while an upgrade is actually
    # in progress (see ClusterCapabilityInventory's own docstring for why
    # that window matters), far slower than the 60s scans above.
    capability_inventory_scan_interval_seconds: int = 300

    # shared/capability_matrix.py's staleness threshold (AI roadmap Pha
    # 0.2) -- a `CapabilityMatrixEntry` older than this many days still
    # returns SUPPORTED (Ceph's official docs for an already-released
    # version don't change), but `is_stale=True` is surfaced so the admin
    # page can prompt an operator to re-verify it against the live docs
    # rather than trusting it forever unexamined. 180 days by default.
    capability_matrix_max_age_days: int = 180

    # worker/preflight.py's staleness threshold for Pha 0.1's own
    # ClusterCapabilityInventory snapshot (AI roadmap Pha 0.5 -- found
    # while writing that phase's own "stale evidence" test: run_preflight
    # originally only checked that the LATEST snapshot's status was
    # SUPPORTED, never how OLD that snapshot was -- a cluster whose
    # Watcher stopped scanning hours/days ago would keep passing preflight
    # forever on a last-known-good snapshot that no longer reflects
    # reality, e.g. an operator could downgrade/reinstall the cluster with
    # Watcher offline and every subsequent AI proposal would still trust
    # the old "supported" verdict). 1 hour by default -- generous relative
    # to capability_inventory_scan_interval_seconds (300s) above, so a
    # couple of missed ticks (a transient MON blip) never cause a false
    # positive, but a genuinely stopped/unreachable-for-a-while Watcher
    # does trip INSUFFICIENT_EVIDENCE (roadmap section 3.1) instead of
    # silently trusting stale data.
    capability_inventory_max_age_seconds: int = 3600

    # Autonomous Operations roadmap Pha 0: every proposal fails closed on
    # stale/unknown capability evidence. Existing installations that have
    # not populated the matrix safely stop proposing executable Actions.
    ai_preflight_enforcement_enabled: bool = True

    # Global Autopilot kill switch. False is the safe baseline: SAFE actions
    # are still diagnosed and rendered from the closed command catalogue,
    # but are parked at PENDING_APPROVAL rather than executed over SSH.
    # Later phases may expose governed per-cluster/playbook overrides; an
    # LLM must never be able to change this setting.
    autopilot_enabled: bool = False
    # Separate server-side commissioning gate. The Dashboard cannot lift it;
    # lab rollout is explicitly unlocked in deployment config only after
    # shadow/maturity prerequisites are met.
    autopilot_activation_unlocked: bool = False
    # Zero means auto-execution is allowed only when no recovery traffic is
    # present. Operators may raise this after measuring a cluster-specific
    # safe ceiling; the runtime gate still blocks inactive/incomplete PGs.
    autopilot_max_recovery_bytes_per_sec: float = 0
    autopilot_max_actions_per_hour: int = 2
    autopilot_max_actions_per_day: int = 5
    autopilot_target_cooldown_seconds: int = 1800
    autopilot_lease_ttl_seconds: int = 900
    autopilot_grace_period_seconds: int = 0

    # worker/llm/router_client.py's Action.expires_at (AI roadmap Pha 0.4,
    # section 3.3's "stale-evidence check") -- how long an Incident-
    # diagnosis proposal stays approvable before dashboard/routes/
    # actions.py::approve_action_core refuses to approve it and asks the
    # operator to let Worker re-diagnose instead. 24h by default: long
    # enough to survive a normal overnight gap before an operator reviews
    # the Dashboard, short enough that approving a RISKY/DESTRUCTIVE action
    # days later — against evidence that may no longer reflect the
    # cluster's real state — requires a fresh proposal instead of blindly
    # trusting a stale one.
    action_approval_expiry_hours: int = 24

    # --- Log Intelligence & AI RCA, bước L0 (Plan/log-intelligence-rca-plan.md) --
    #
    # watcher/log_intel.py's own scan: pulls a WINDOW of mon/mgr/osd/rgw log
    # from every configured node, fingerprints each line into a normalized
    # template, and counts those templates per hour. Deliberately OFF by
    # default -- unlike every other Watcher scan block, this one reads a
    # much larger slice of each node's log per tick (see
    # log_intel_max_lines_per_daemon below), so an operator opts in once
    # they actually want the RCA evidence base being built.
    log_intel_enabled: bool = False
    # "ssh" (no new infrastructure -- reuses the same SSH access
    # watcher/ceph_log.py already has) or "loki" (the log store chosen for
    # this feature, see the plan's section 11.1). The analysis layer
    # (fingerprint/triage/AI) is identical either way -- only this adapter
    # changes, which is the whole point of watcher/log_source/'s Protocol.
    log_intel_source: str = "ssh"
    # 15 minutes: far slower than osd_latency/crush's 60s scans (a fresh
    # SSH round trip PER daemon type PER node is the heaviest collection in
    # this codebase), fast enough that one window still lands inside the
    # log-retention of a busy node.
    log_intel_scan_interval_seconds: int = 900
    # How far back each scan looks. Deliberately LARGER than the scan
    # interval above so a late/slow tick overlaps rather than leaving a
    # hole -- fingerprint counting is idempotent per (pattern, hour bucket)
    # only for the CURRENT bucket, so a small overlap is the safe direction
    # to err (see log_intel.py::_upsert_observation).
    log_intel_window_minutes: int = 60
    # Hard ceiling on lines pulled per (node, daemon type) per scan -- the
    # cost bound for the SSH path. 5000 lines x 4 daemon types x N nodes is
    # the worst case per tick.
    log_intel_max_lines_per_daemon: int = 5000
    # Retention. Findings/patterns are small and worth keeping; the
    # per-hour observation counts are the only table here that can really
    # grow (patterns x hours x hosts), so it gets a much shorter window --
    # see the plan's constraint R1 and watcher/database_capacity_monitor.py
    # for why this codebase treats its OWN database size as a real limit.
    log_intel_pattern_retention_days: int = 180
    log_intel_observation_retention_days: int = 30
    # Findings đã RESOLVED giữ lại bao lâu. Finding còn OPEN/ACKNOWLEDGED
    # KHÔNG BAO GIỜ bị xoá vì già -- nó vẫn là việc chưa xong của người
    # trực (xem watcher/log_intel.py::prune_old_rows).
    log_intel_finding_retention_days: int = 90
    # --- Triage L1 (watcher/log_triage.py) ---
    #
    # Đây là tầng quyết định "cái gì đáng nhìn", chạy hoàn toàn tất định
    # (không AI, không tốn token) và là chốt chặn chi phí cho L2: chỉ mẫu
    # được gắn cờ ở đây mới bao giờ được đưa lên model.
    #
    # Số ngày lịch sử dùng làm baseline khi so sánh đột biến. So sánh theo
    # CÙNG KHUNG GIỜ TRONG NGÀY (xem log_triage.py::_baseline_for) nên 7
    # ngày = 7 mẫu cho mỗi khung giờ -- đủ để phân biệt "3h sáng lúc nào
    # cũng nhiều log scrub" với "3h sáng nay đột nhiên nhiều gấp 5 lần".
    log_intel_baseline_days: int = 7
    # Một mẫu mới xuất hiện phải đạt tối thiểu ngần này lần trong cửa sổ
    # mới bị coi là đáng chú ý -- một dòng log lạ xuất hiện đúng 1 lần
    # thường là nhiễu, không phải tín hiệu.
    log_intel_novelty_min_count: int = 3
    # Gấp bao nhiêu lần baseline thì tính là đột biến.
    log_intel_burst_ratio: float = 5.0
    # Số mẫu lịch sử tối thiểu trước khi phép so sánh đột biến được coi là
    # có ý nghĩa. Dưới ngưỡng này thì KHÔNG gắn cờ -- cùng nguyên tắc
    # "không đủ mẫu thì không kết luận" mà roadmap mục 1.2/3.1 đã đặt ra,
    # và là thứ giữ cho tuần đầu chạy (baseline còn rỗng) không biến thành
    # một trận mưa cảnh báo giả.
    log_intel_burst_min_baseline_samples: int = 3
    # Mỗi lời gọi AI chỉ nhận một batch nhỏ để prompt có trọng tâm. Một cửa
    # sổ 28 pattern sẽ thành 10+10+8 thay vì bị bỏ toàn bộ.
    log_intel_ai_batch_size: int = 10
    # Circuit breaker tổng vẫn cần cho onboarding/backfill hoặc collector
    # lỗi thực sự. Chỉ bỏ AI khi số pattern vượt trần lớn này; L0/L1 vẫn lưu.
    log_intel_ai_max_flagged_patterns: int = 100
    # --- Phân tích AI L2 (watcher/log_analysis.py) ---
    #
    # TÁCH RIÊNG khỏi log_intel_enabled một cách có chủ ý: bật thu thập
    # không đồng nghĩa với bật chi tiêu token. Operator nên chạy L0+L1 vài
    # ngày trước, xem `log_ingest_runs.patterns_flagged` mỗi tick là bao
    # nhiêu, gắn BENIGN cho nhiễu, rồi mới bật cái này -- vì chi phí AI tỉ
    # lệ thuận với đúng con số mà tầng triage thả qua.
    log_intel_ai_enabled: bool = False
    # Trần kích thước phần evidence ghép vào prompt. Vượt trần thì bị cắt và
    # model được báo rõ là đã cắt (để nó trả INSUFFICIENT_EVIDENCE thay vì
    # kết luận trên dữ liệu thiếu).
    log_intel_max_evidence_chars: int = 20000
    # Loki adapter (used only when log_intel_source == "loki"). Base URL of
    # the Loki HTTP API, e.g. "http://loki.observability:3100" -- no
    # trailing /loki/api/v1 (the adapter appends its own path). Empty means
    # unconfigured, which fails the scan loudly rather than silently
    # collecting nothing.
    log_intel_loki_url: str = ""
    # Optional multi-tenancy header (X-Scope-OrgID). Empty = single-tenant.
    log_intel_loki_tenant: str = ""
    log_intel_loki_timeout_seconds: int = 30


def refresh_cluster_settings_from_env() -> dict[str, str]:
    """Refresh live cluster settings from the shared .env file.

    podman-compose copies values from env_file into a container when that
    container is created. A later edit of the mounted file therefore leaves
    a stale CEPH_EXEC_MODE in os.environ; pydantic-settings would normally
    let that stale process environment override the new file. The cluster
    form and lifecycle are file-backed, so the file is authoritative here.

    An absent file is deliberately a no-op so deployments that provide only
    environment variables continue to work.
    """
    from shared import env_config

    fresh = env_config.read_env_values(list(env_config.CLUSTER_ENV_NAMES.values()))
    for field, env_name in env_config.CLUSTER_ENV_NAMES.items():
        if env_name in fresh:
            setattr(settings, field, fresh[env_name])
    return fresh


settings = Settings()
# Apply the shared-file overlay once during import. Long-lived Dashboard
# routes can call the same helper before rendering cluster-sensitive pages.
refresh_cluster_settings_from_env()
