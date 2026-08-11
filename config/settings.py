from pydantic_settings import BaseSettings, SettingsConfigDict

# Exposed as module-level constants (not just inline defaults) so other code
# — e.g. the startup check in dashboard/app.py — can detect "still using the
# dev default" without duplicating these literals.
DEFAULT_DASHBOARD_PASSWORD_HASH = "$2b$12$G9OqbEMaoR6ROfQpQWbNrOgfzlDhBy7Z5fRFGezCr89SqqTkqFWlm"
DEFAULT_SESSION_SECRET_KEY = "dev-only-insecure-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")

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

    # Code review fix (2026-08-04, Story 7.2): where the operator has
    # actually deployed the separate Epic 10 React app
    # (ceph-upgrade-test-runner-frontend/) — dashboard/routes/upgrade.py
    # only ever links out to it (never makes an HTTP call to it itself, see
    # that module's own TEST_RUNNER_FRONTEND_URL comment), but hardcoding
    # "http://localhost:5173" there was broken by construction for the
    # normal deployment topology, where the operator's browser isn't on the
    # same machine as the Dashboard backend. Deliberately blank — same
    # "app falls back to its own dev-server default when unset" posture as
    # leaving this unset entirely; set via .env/Settings page once deployed
    # somewhere real.
    test_runner_frontend_url: str = ""

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

    # Optional ChatGPT subscription-backed Codex connection used by the
    # dashboard chat only.  Credentials are owned/refreshed by Codex CLI in
    # codex_home; ceph-ai never copies OAuth tokens into .env or the DB.
    codex_chat_enabled: bool = False
    codex_home: str = ".codex-account"

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
    telegram_node_bot_token: str = ""
    telegram_node_chat_id: str = ""
    telegram_node_enabled: bool = True
    # dashboard/telegram_approval_bot.py's own DB-scan cadence for newly
    # PENDING_APPROVAL Actions not yet broadcast to every configured
    # channel above — short by design (unlike device_health/node_health's
    # scan intervals above, this is a cheap DB query, not a real SSH round
    # trip, so there's no reason to space it out).
    telegram_approval_scan_interval_seconds: int = 10

    # watcher/node_health_monitor.py's own scan cadence — same reasoning as
    # device_health_scan_interval_seconds above: collecting CPU/RAM needs a
    # fresh SSH round trip PER NODE (watcher/node_metrics.py samples /proc
    # twice, ~1s apart, per host), so running that on every
    # watcher_poll_interval_seconds tick (15s default) would be a
    # meaningfully heavier SSH load than the single `ceph health detail`
    # query the main loop already does. 15 minutes by default.
    node_health_scan_interval_seconds: int = 900

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

    # watcher/database_capacity_monitor.py's own cadence -- deliberately
    # much slower than every other scan above: this app's own DB size
    # grows on a scale of days/weeks, not seconds, and checking it every
    # tick would be pure overhead (a file stat for SQLite, or a real
    # round trip to the separate OpenEverest-managed Postgres cluster for
    # everything else) for no operational benefit. 1 hour by default.
    database_size_scan_interval_seconds: int = 3600

settings = Settings()
