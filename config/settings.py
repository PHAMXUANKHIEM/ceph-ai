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

    # Worker (Story 2.1): total number of processing attempts (including the
    # first) before an Incident is marked FAILED and its message dead-lettered
    # — NOT the count of retries after the first attempt.
    worker_max_retries: int = 3

    # 9router (self-hosted, OpenAI-compatible LLM proxy) — the single AI
    # backend for both incident diagnosis (worker/llm/router_client.py) and
    # the Chat-with-AI feature (dashboard/chat_client.py). This app never
    # calls a vendor's AI API directly — always through this router (see
    # shared/router_client.py::RouterNotConfiguredError) — because 9router's
    # own model catalog (gc/gemini-*, alicode/*, ...) is the whole point:
    # the operator picks whichever underlying model 9router fronts, not a
    # fixed vendor. Empty by default — live tests skip gracefully when
    # unset (see tests/test_router_client_live.py).
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

    # Worker (Story 4.3): how often the Worker checks for Actions an
    # operator just approved on the Dashboard. Separate from RabbitMQ
    # entirely — an approval isn't a queue message, so nothing redelivers
    # it; the Worker has to notice it itself.
    worker_approval_poll_interval_seconds: int = 5


settings = Settings()
