import asyncio
import ipaddress
import logging
import math
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import openai
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db, env_config, trust_engine
from shared.ai_cost import summary as ai_cost_summary
from shared.codex_app_server import (
    CodexAppServerError,
    codex_app_server,
    codex_executable,
    install_codex_cli,
    refresh_app_server_after_cli_login,
    start_cli_device_login,
)
from shared.claude_cli import (
    ClaudeCLIError,
    claude_executable,
    claude_logout,
    claude_status,
    install_claude_cli,
    start_claude_login,
    submit_claude_authentication_code,
)
from shared.clusters import sync_default_cluster_from_settings
from shared.cluster_nodes import resolve_ssh_creds
from shared.ai_limits import normalize_rate_limits
from shared.models import (
    ActionPolicyOverride, ActionPolicyOverrideAudit,
    AutopilotClusterConfigAudit, AutopilotConfigAudit, Cluster, PlaybookStat,
)
from shared.router_client import list_router_models, readable_exception_message
from watcher.ceph_client import (
    VALID_EXEC_MODES,
    CephQueryError,
    query_cluster_health_with,
    read_public_key,
    ssh_key_path_error,
    validate_ceph_keyring_with,
)
from worker.executor.ssh_executor import ExecutorError, execute_command
from worker.executor import commands as executor_commands
from worker.executor.vm_perf import _vm_ssh_command
from worker.policy.playbook_registry import registry_status_rows
from worker.policy import gate as action_gate

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

MASK_VISIBLE_CHARS = 4

ROUTER_API_KEY_ENV_NAME = "ROUTER_API_KEY"
ROUTER_MODEL_ENV_NAME = "ROUTER_MODEL"
ROUTER_BASE_URL_ENV_NAME = "ROUTER_BASE_URL"
ROUTER_ENABLED_ENV_NAME = "ROUTER_ENABLED"
ROUTER_PROVIDER_ENV_NAME = "ROUTER_PROVIDER"
CODEX_CHAT_ENABLED_ENV_NAME = "CODEX_CHAT_ENABLED"
CLAUDE_CHAT_ENABLED_ENV_NAME = "CLAUDE_CHAT_ENABLED"
CODEX_CHAT_MODEL_ENV_NAME = "CODEX_CHAT_MODEL"
CLAUDE_CHAT_MODEL_ENV_NAME = "CLAUDE_CHAT_MODEL"
CLAUDE_CHAT_EFFORT_ENV_NAME = "CLAUDE_CHAT_EFFORT"
AI_COST_DAILY_BUDGET_ENV_NAME = "AI_COST_DAILY_BUDGET_USD"
AI_COST_MONTHLY_BUDGET_ENV_NAME = "AI_COST_MONTHLY_BUDGET_USD"
AI_COST_BUDGET_HARD_LIMIT_ENV_NAME = "AI_COST_BUDGET_HARD_LIMIT"
AI_COST_BUDGET_RESERVE_OUTPUT_ENV_NAME = "AI_COST_BUDGET_RESERVE_OUTPUT_TOKENS"
CLAUDE_MODELS = [
    {"id": "default", "label": "Mặc định tài khoản", "version": "Tự động"},
    {"id": "fable", "label": "Claude Fable", "version": "5 (mới nhất)"},
    {"id": "opus", "label": "Claude Opus", "version": "5 (mới nhất)"},
    {"id": "sonnet", "label": "Claude Sonnet", "version": "5 (mới nhất)"},
    {"id": "haiku", "label": "Claude Haiku", "version": "4.5 (mới nhất)"},
    {"id": "claude-opus-4-8", "label": "Claude Opus", "version": "4.8"},
    {"id": "claude-opus-4-7", "label": "Claude Opus", "version": "4.7"},
    {"id": "claude-opus-4-6", "label": "Claude Opus", "version": "4.6"},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet", "version": "4.6"},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku", "version": "4.5"},
]
CLAUDE_EFFORTS = [
    {"id": "auto", "label": "Tự động theo model"},
    {"id": "low", "label": "Low — nhanh, ít suy luận"},
    {"id": "medium", "label": "Medium — cân bằng chi phí"},
    {"id": "high", "label": "High — suy luận kỹ"},
    {"id": "xhigh", "label": "XHigh — suy luận sâu"},
    {"id": "max", "label": "Max — suy luận tối đa"},
]

# API AI connection-type presets shown on the Settings page (2026-07-24).
# Every entry still ends up going through the exact same generic
# AsyncOpenAI(api_key=..., base_url=...) client (shared/router_client.py) —
# this dict only drives the UI (label + which preset Base URL to prefill
# when an operator picks that provider), never the actual request logic.
# "base_url" is None for "9router" because that Base URL is operator-
# specific (self-hosted host:port), never a fixed public endpoint like the
# other three.
PROVIDER_PRESETS: dict[str, dict[str, str | None]] = {
    "9router": {
        "label": "9router (tự triển khai)",
        "base_url": None,
        "base_url_placeholder": "http://localhost:20128",
    },
    "anthropic": {
        "label": "Claude (Anthropic)",
        "base_url": "https://api.anthropic.com/v1",
        "base_url_placeholder": "https://api.anthropic.com/v1",
    },
    "openai": {
        "label": "Codex (OpenAI)",
        "base_url": "https://api.openai.com/v1",
        "base_url_placeholder": "https://api.openai.com/v1",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "base_url_placeholder": "https://openrouter.ai/api/v1",
    },
}
DEFAULT_PROVIDER = "9router"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# Multi-cluster deployment (docs/multi-cluster-deployment.md): a 2nd Ceph
# cluster is monitored by a 2nd full checkout of this repo, sometimes on the
# SAME host as this one. LOG_TAG scopes log filenames. Process discovery
# deliberately matches the module independently of the spelling of argv[0]
# (absolute `/root/.../.venv/bin/python`, `.venv/bin/python`, or `python`),
# then verifies `/proc/<pid>/cwd` equals this checkout's PROJECT_ROOT. The
# old absolute-interpreter regex silently missed relative-path processes
# and every Settings save leaked another Worker/Watcher/Codex child. CWD
# scoping still prevents killing an instance from a sibling checkout.
# For the existing single-instance
# deployment (checkout named "ceph-aiops") LOG_TAG reproduces the exact same
# /var/log/ceph-aiops-*.log paths as before.
LOG_TAG = PROJECT_ROOT.name
WORKER_MODULE = "worker.main"
# pgrep uses POSIX ERE, not Python's regex dialect: non-capturing groups
# `(?:...)` and `\s` are invalid on CentOS procps-ng and make restart fail
# with exit 2. Use portable character classes and ordinary groups.
WORKER_PGREP_PATTERN = r"(^|[[:space:]])-m[[:space:]]+worker\.main([[:space:]]|$)"
WORKER_LOG_PATH = Path(f"/var/log/{LOG_TAG}-worker.log")
WORKER_STOP_TIMEOUT_SECONDS = 5.0
WORKER_START_CHECK_DELAY_SECONDS = 1.5
PGREP_TIMEOUT_SECONDS = 5.0

# Story 5.1: same process-management pattern as Worker above, applied to
# Watcher so a cluster-connection config change takes effect immediately.
WATCHER_MODULE = "watcher.main"
WATCHER_PGREP_PATTERN = r"(^|[[:space:]])-m[[:space:]]+watcher\.main([[:space:]]|$)"
WATCHER_LOG_PATH = Path(f"/var/log/{LOG_TAG}-watcher.log")
REMEDIATION_WATCHER_MODULE = "watcher.remediation_main"
REMEDIATION_WATCHER_PGREP_PATTERN = r"(^|[[:space:]])-m[[:space:]]+watcher\.remediation_main([[:space:]]|$)"
REMEDIATION_WATCHER_LOG_PATH = Path(f"/var/log/{LOG_TAG}-remediation-watcher.log")

# These processes are separate systemd units in production. Starting a
# detached child from Dashboard and then discovering/killing matching PIDs
# races systemd and leaves duplicate singleton services behind.
MANAGED_SERVICE_UNITS = {
    "worker": "ceph-ai-worker.service",
    "watcher": "ceph-ai-watcher.service",
    "remediation_watcher": "ceph-ai-remediation-watcher.service",
}
SYSTEMCTL_TIMEOUT_SECONDS = 10

# Restarting the Dashboard itself — unlike Worker/Watcher, this is the very
# process handling the HTTP request that triggers it, so it can't just spawn
# a replacement and kill the old one the same way _start_process() does (see
# restart_dashboard_process below for why).
DASHBOARD_LOG_PATH = Path(f"/var/log/{LOG_TAG}-dashboard.log")
DASHBOARD_RESTART_GRACE_SECONDS = 1.0
DASHBOARD_RESTART_WAIT_SECONDS = 10.0
SYSTEMD_SERVICE_RE = re.compile(r"(?:^|/)([A-Za-z0-9_.@-]+\.service)$")

# 2026-07-24: this app now has real per-account roles (shared/models.py::User,
# created via the "Người dùng" card below) instead of the single hardcoded
# account it used to have — auth.is_admin_user() is the single source of
# truth for "is this account allowed to see/use admin-only controls"
# (restarting Worker/Watcher/Dashboard, switching the database, managing
# users), true for the `.env`-configured account or any active DB-created
# User row with is_admin=True.
def _require_admin_privilege(user: str) -> None:
    if not auth.is_admin_user(user):
        raise HTTPException(
            status_code=403,
            detail="Chỉ tài khoản admin mới được phép thực hiện thao tác này",
        )


def _mask_key(key: str) -> str:
    """Never returns the full key — at most the last MASK_VISIBLE_CHARS
    characters, and fewer than that for very short input, so there's always
    at least one character redacted for any non-empty key."""
    visible_count = min(MASK_VISIBLE_CHARS, max(len(key) - 1, 0))
    visible = key[-visible_count:] if visible_count else ""
    return "..." + visible


async def verify_router_connection(api_key: str, base_url: str) -> tuple[bool, str | None, list[str] | None]:
    """Backs the Settings page's "Xác nhận kết nối" step — a single
    GET /v1/models call (shared/router_client.py::list_router_models) both
    proves the key/base_url actually work AND returns the real model
    catalog in one round trip, per spec: there is no separate "ping" call
    here the way the old Gemini-native flow needed one (a models-list call
    is cheap/fast, unlike a real generate_content call, and 9router's
    /v1/models response IS proof the key authenticated — a bad key 401s
    that same endpoint).

    Returns (valid, message, models):
    - (True, "Kết nối thành công — tìm thấy N model", [...]) on success.
    - (False, "API key không hợp lệ", None) on 401/403.
    - (False, "Không thể kết nối {base_url} — kiểm tra host/port", None) on
      a network-level failure (DNS, connection refused, timeout).
    - (False, <best-effort reason>, None) for anything else (e.g. the
      router responding but with an unexpected shape).
    """
    try:
        models = await list_router_models(api_key, base_url)
    except (openai.AuthenticationError, openai.PermissionDeniedError):
        return False, "API key không hợp lệ", None
    except openai.APIConnectionError:
        return False, f"Không thể kết nối {base_url} — kiểm tra host/port", None
    except Exception as exc:
        return False, readable_exception_message(exc), None
    return True, f"Kết nối thành công — tìm thấy {len(models)} model", models


# Story 8.1: env-file read/write moved to shared/env_config.py so Worker-
# side code can reuse it without importing this Dashboard route module.
# Thin aliases kept so every existing call site below (and every existing
# test) keeps working unchanged.
_update_env_file = env_config.update_env_file
_update_env_file_batch = env_config.update_env_file_batch


def _find_pids(pgrep_pattern: str) -> list[int]:
    result = subprocess.run(
        # "--" ends option parsing — patterns here start with "-m", which
        # pgrep would otherwise try to parse as its own -m/--... flag.
        ["pgrep", "-f", "--", pgrep_pattern],
        capture_output=True,
        text=True,
        timeout=PGREP_TIMEOUT_SECONDS,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pgrep failed (exit {result.returncode}): {result.stderr.strip()}")
    matching: list[int] = []
    for raw_pid in result.stdout.split():
        pid = int(raw_pid)
        try:
            process_cwd = Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        if process_cwd == PROJECT_ROOT.resolve():
            matching.append(pid)
    return matching


def _find_worker_pids() -> list[int]:
    return _find_pids(WORKER_PGREP_PATTERN)


def _find_watcher_pids() -> list[int]:
    return _find_pids(WATCHER_PGREP_PATTERN)


def _find_remediation_watcher_pids() -> list[int]:
    return _find_pids(REMEDIATION_WATCHER_PGREP_PATTERN)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _stop_worker(pids: list[int], timeout: float = WORKER_STOP_TIMEOUT_SECONDS) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already gone

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in pids):
            return
        time.sleep(0.2)

    for pid in pids:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _start_process(module: str, log_path: Path, env: dict) -> int:
    with open(log_path, "a") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", module],
            stdout=log_file,
            stderr=log_file,
            # Detach into its own session — must survive independently of the
            # Dashboard's own process (and not die if Dashboard is later
            # restarted/signaled as a group).
            start_new_session=True,
            cwd=str(PROJECT_ROOT),
            env=env,
        )
    # Popen dup()s the fd for the child; the parent's copy (closed by the
    # `with` block above) is no longer needed once the subprocess has it.

    # The Dashboard remains this child's OS-level parent even though it's
    # detached into its own session — nothing else ever calls .wait() on it,
    # so whenever it exits (natural exit, or SIGTERM/SIGKILL from
    # _stop_worker) it would sit as an unreaped zombie indefinitely, and
    # os.kill(pid, 0) reports zombies as still "alive". A daemon thread that
    # just blocks on wait() reaps it as soon as it exits, with no effect on
    # the caller.
    threading.Thread(target=proc.wait, daemon=True).start()

    return proc.pid


def _sync_cluster_settings_from_env() -> None:
    """Cluster node config can be written to .env by a process OTHER than
    this Dashboard one — worker/executor/cluster_deploy.py runs inside the
    Worker process and writes CLUSTER_ENV_NAMES fields directly to .env right
    after a successful "Dựng cụm" (build cluster) deploy. This Dashboard
    process's own `settings` singleton loaded .env once at import time and
    has no way to notice that write on its own, so without this, the
    "Khởi động lại Worker/Watcher" buttons on the deploy-cluster page would
    restart fresh processes with the explicit CLUSTER_ENV_NAMES override
    below still pointing at whatever cluster was configured when THIS
    Dashboard process last started — not the cluster that was just deployed.
    Called right before _start_worker()/_start_watcher() build child_env."""
    fresh = env_config.read_env_values(list(CLUSTER_ENV_NAMES.values()))
    for field, env_name in CLUSTER_ENV_NAMES.items():
        if env_name in fresh:
            setattr(settings, field, fresh[env_name])


def _start_worker() -> int:
    # Explicit env= override: the freshly-saved key/model must always win
    # over whatever the Dashboard process's own os.environ happens to hold
    # (e.g. a stale ROUTER_API_KEY exported once by hand) — pydantic-settings
    # would otherwise prefer a real env var over .env file contents, and the
    # new Worker would silently keep using an old key/model despite .env
    # being updated.
    #
    # 2026-07-24: same reasoning now extends to the patch-pipeline fields
    # (worker/executor/commands.py::_patch_build_and_stage_command reads
    # them directly) and, since that same command also calls
    # shared.cluster_nodes.configured_nodes(), to the cluster node fields
    # too — a Worker restarted here (e.g. right after saving new patch
    # settings) must see the CURRENT cluster node list, not a stale one
    # from whenever it originally started.
    _sync_cluster_settings_from_env()
    child_env = {
        **os.environ,
        ROUTER_API_KEY_ENV_NAME: settings.router_api_key,
        ROUTER_MODEL_ENV_NAME: settings.router_model,
        ROUTER_BASE_URL_ENV_NAME: settings.router_base_url,
        ROUTER_ENABLED_ENV_NAME: "true" if settings.router_enabled else "false",
        CODEX_CHAT_ENABLED_ENV_NAME: "true" if settings.codex_chat_enabled else "false",
        CLAUDE_CHAT_ENABLED_ENV_NAME: "true" if settings.claude_chat_enabled else "false",
        **{env_name: getattr(settings, field) for field, env_name in PATCH_PIPELINE_ENV_NAMES.items()},
        **{env_name: getattr(settings, field) for field, env_name in CLUSTER_ENV_NAMES.items()},
    }
    return _start_process(WORKER_MODULE, WORKER_LOG_PATH, child_env)


# Story 8.1: moved to shared/env_config.py so worker/executor/cluster_deploy.py
# can reuse the same field->env-var-name mapping without importing this
# Dashboard route module. Thin alias kept so every call site below (and
# every existing test) keeps working unchanged.
CLUSTER_ENV_NAMES = env_config.CLUSTER_ENV_NAMES

# 2026-07-24: Ceph patch build & deploy pipeline (dashboard/routes/patch.py) —
# consumed by WORKER (worker/executor/commands.py's
# _patch_build_and_stage_command/_patch_install_command), not Watcher, so
# saving these restarts Worker instead of Watcher (see _start_worker()'s
# explicit child_env override below, same reasoning as ROUTER_*_ENV_NAME —
# a fresh Worker process must see these values immediately, not whatever
# was exported in the Dashboard's own os.environ once).
LOG_INTEL_ENV_NAMES = {
    # Log Intelligence (Plan/log-intelligence-rca-plan.md). Hai công tắc
    # TÁCH RIÊNG có chủ đích: bật thu thập không đồng nghĩa bật chi tiêu
    # token -- xem docs/runbook-log-intelligence.md mục 2 cho quy trình bật
    # 2 bước được khuyến nghị.
    "log_intel_enabled": "LOG_INTEL_ENABLED",
    "log_intel_ai_enabled": "LOG_INTEL_AI_ENABLED",
    "log_intel_source": "LOG_INTEL_SOURCE",
    "log_intel_scan_interval_seconds": "LOG_INTEL_SCAN_INTERVAL_SECONDS",
    "log_intel_window_minutes": "LOG_INTEL_WINDOW_MINUTES",
    "log_intel_max_lines_per_daemon": "LOG_INTEL_MAX_LINES_PER_DAEMON",
    "log_intel_loki_url": "LOG_INTEL_LOKI_URL",
    "log_intel_loki_tenant": "LOG_INTEL_LOKI_TENANT",
    # Ngưỡng triage để riêng khỏi form này -- chúng chỉ cần chỉnh khi đã
    # chạy thật và thấy nhiễu, không phải lúc cấu hình ban đầu; runbook mục
    # 4 hướng dẫn ưu tiên gắn nhãn BENIGN trên trang /log-intelligence
    # trước khi động tới ngưỡng.
}


PATCH_PIPELINE_ENV_NAMES = {
    "ceph_patch_build_node": "CEPH_PATCH_BUILD_NODE",
    "ceph_patch_source_dir": "CEPH_PATCH_SOURCE_DIR",
    "ceph_patch_build_command": "CEPH_PATCH_BUILD_COMMAND",
    "ceph_patch_output_dir": "CEPH_PATCH_OUTPUT_DIR",
    "ceph_patch_node_staging_dir": "CEPH_PATCH_NODE_STAGING_DIR",
    # ssh_user/ssh_key_path are deliberately NOT here — same shared SSH
    # credential already used for every other cluster/build-server target
    # (see config/settings.py's ceph_patch_build_node docstring), not a
    # separate one for this form to manage.
}

# AI Code Repair supervisor roles are separate from Chat-with-AI/provider
# settings: Planner/Reviewer asks, plans and audits; Implementer edits the
# isolated worktree.
CODE_REPAIR_ENV_NAMES = env_config.CODE_REPAIR_ENV_NAMES
CODE_REPAIR_PROVIDERS = ("auto", "codex", "claude")
CODE_REPAIR_MAX_REVIEW_ROUNDS = 5


def _start_watcher() -> int:
    # Explicit env= override — mirrors _start_worker()'s ROUTER_API_KEY
    # handling exactly (Review Story 5.1: originally omitted here on the
    # wrong assumption that no stale-env-var risk existed for these fields —
    # pydantic-settings prefers a real env var over `.env` file contents,
    # same as the Worker/API-key case, so any of these already exported in
    # the Dashboard's own process environment — systemd unit, parent shell,
    # docker env — would otherwise silently override the freshly saved
    # `.env` values for the new Watcher process, violating AC #3's "apply
    # immediately" guarantee).
    _sync_cluster_settings_from_env()
    child_env = {
        **os.environ,
        **{env_name: getattr(settings, field) for field, env_name in CLUSTER_ENV_NAMES.items()},
    }
    return _start_process(WATCHER_MODULE, WATCHER_LOG_PATH, child_env)


def _start_remediation_watcher() -> int:
    _sync_cluster_settings_from_env()
    child_env = {
        **os.environ,
        **{env_name: getattr(settings, field) for field, env_name in CLUSTER_ENV_NAMES.items()},
    }
    return _start_process(
        REMEDIATION_WATCHER_MODULE, REMEDIATION_WATCHER_LOG_PATH, child_env,
    )


def _restart_managed_service(kind: str) -> dict | None:
    """Restart a managed process through systemd when Dashboard is managed.

    ``None`` means this is a development/no-systemd deployment and callers
    should use the existing detached-process fallback.
    """
    owner = _current_systemd_service_unit()
    if not owner or "dashboard" not in owner:
        return None
    unit = MANAGED_SERVICE_UNITS[kind]
    try:
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            check=False, timeout=SYSTEMCTL_TIMEOUT_SECONDS,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if active.returncode != 0:
            return None
        subprocess.run(
            ["systemctl", "restart", unit],
            check=True, timeout=SYSTEMCTL_TIMEOUT_SECONDS,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(WORKER_START_CHECK_DELAY_SECONDS)
        main_pid = subprocess.run(
            ["systemctl", "show", "--property=MainPID", "--value", unit],
            check=True, timeout=SYSTEMCTL_TIMEOUT_SECONDS,
            capture_output=True, text=True,
        ).stdout.strip()
        if not main_pid.isdigit() or int(main_pid) <= 0:
            raise RuntimeError(f"systemd restarted {unit} without a live MainPID")
        return {"restarted": True, "new_pid": int(main_pid), "error": None}
    except Exception:
        logger.exception("systemd restart failed for %s", unit)
        return {
            "restarted": False, "new_pid": None,
            "error": f"systemd không khởi động lại được {unit} — xem server log",
        }


# --- Database connection (Settings page's "Kết nối Database" section) ---
# The app's storage backend — everything in shared/models.py (Incident,
# Action, AuditEntry, chat history...). Postgres only (not Mongo/MySQL): the
# schema is fully relational (FK constraints, CheckConstraint-backed enums)
# and already managed via Alembic SQL DDL — see the "3 loại db" conversation
# this section answers.
DATABASE_URL_ENV_NAME = "DATABASE_URL"
ALEMBIC_INI_PATH = PROJECT_ROOT / "alembic.ini"
ALEMBIC_SCRIPT_LOCATION = PROJECT_ROOT / "alembic"
DEFAULT_POSTGRES_PORT = 5432
DB_TEST_QUERY = text("SELECT 1")


# Bare drivername values that mean "PostgreSQL, whatever driver's
# available" rather than a specific one — "postgres" is the scheme every
# managed-Postgres provider (Heroku-style DATABASE_URL, most k8s
# pgbouncer/operator services...) actually hands out, but SQLAlchemy only
# ever recognizes "postgresql" as a dialect name, never bare "postgres" (a
# NoSuchModuleError, not a connection error) — and a driver-less
# "postgresql://" defaults to psycopg2, which isn't installed in this
# project (psycopg v3 is — see pyproject.toml).
_BARE_POSTGRES_DRIVERNAMES = frozenset({"postgres", "postgresql"})


def _normalize_postgres_driver(url):
    """Rewrites a pasted "Nhập Database URL" value's drivername to
    `postgresql+psycopg` whenever it's one of _BARE_POSTGRES_DRIVERNAMES —
    every OTHER drivername (e.g. an explicit `+psycopg2` the operator
    deliberately typed) is left untouched, since that's not this app's
    driver to silently override."""
    if url.drivername in _BARE_POSTGRES_DRIVERNAMES:
        return url.set(drivername="postgresql+psycopg")
    return url


def _build_postgres_url(host: str, port: int, dbname: str, username: str, password: str) -> str:
    return URL.create(
        "postgresql+psycopg",
        username=username,
        password=password,
        host=host,
        port=port,
        database=dbname,
    )


def _resolve_database_url(
    db_host: str,
    db_port: str,
    db_name: str,
    db_username: str,
    db_password: str,
    database_url_raw: str,
) -> tuple[object | None, str | None]:
    """Backs BOTH input modes the "Kết nối Database" form offers — "Nhập
    từng trường" (host/port/dbname/username/password, built via
    _build_postgres_url) and "Nhập Database URL" (a single already-complete
    SQLAlchemy URL string, e.g. copy-pasted from another tool/ops runbook).
    A non-blank database_url_raw always wins over the 5 separate fields —
    the caller never has to know which mode the operator actually used.

    Returns (url, None) on success, (None, error_message) otherwise — never
    raises, so both call sites (settings_test_database/settings_save_database)
    can turn a bad input straight into a user-facing message without a
    try/except of their own."""
    raw = database_url_raw.strip()
    if raw:
        try:
            return _normalize_postgres_driver(make_url(raw)), None
        except Exception as exc:
            return None, f"Database URL không hợp lệ: {readable_exception_message(exc)}"

    host, name, username = db_host.strip(), db_name.strip(), db_username.strip()
    if not host or not name or not username:
        return None, "Vui lòng điền đủ Host, Database name, Username (hoặc dùng ô Database URL)."
    try:
        port = int(db_port.strip())
    except ValueError:
        return None, f"Port không hợp lệ: {db_port!r}"
    return _build_postgres_url(host, port, name, username, db_password), None


DB_TEST_CONNECT_TIMEOUT_SECONDS = 5


def _test_database_connection(url) -> tuple[bool, str]:
    """Raw connectivity + auth check only (SELECT 1) — no schema/migration
    involved. Always disposes the probe engine itself; never touches/replaces
    shared.db.engine (that stays bound to whatever's actually configured
    until a real save+restart — see _run_alembic_upgrade_head's docstring).

    Deliberately does NOT go through shared.db.make_engine() here — that
    helper sets no connect timeout at all (fine for the app's one long-lived
    engine, which is only ever pointed at an already-verified database), but
    this probe runs against operator-typed host/port that may simply be
    wrong/unreachable. Verified live: without an explicit connect_timeout,
    psycopg's TCP connect against a silently-dropping host/port hangs for
    minutes before failing — a "Kiểm tra kết nối" click has to fail fast
    instead."""
    connect_args = (
        {"connect_timeout": DB_TEST_CONNECT_TIMEOUT_SECONDS} if url.get_backend_name() == "postgresql" else {}
    )
    engine = create_engine(url, connect_args=connect_args)
    try:
        with engine.connect() as conn:
            conn.execute(DB_TEST_QUERY)
    except SQLAlchemyError as exc:
        return False, readable_exception_message(exc)
    except Exception as exc:  # e.g. driver-level connect refused
        return False, readable_exception_message(exc)
    finally:
        engine.dispose()
    return True, "Kết nối thành công"


def _run_alembic_upgrade_head(url) -> None:
    """Points Alembic's env.py (which always reads config/settings.py's
    `settings.database_url` — AD-8, see alembic/env.py) at the CANDIDATE url
    just long enough to run `upgrade head` against it, so a brand-new
    Postgres database gets today's full schema before anything else ever
    touches it — same in-process technique tests/test_migrations.py already
    uses (Config + command.upgrade, no subprocess). Restores the previous
    `settings.database_url` on any failure so a botched migration attempt
    never leaves the in-memory settings pointed at a schema-less database;
    on success the candidate url is deliberately left in place for the
    caller to persist to .env right after."""
    previous_url = settings.database_url
    settings.database_url = url.render_as_string(hide_password=False)
    try:
        cfg = AlembicConfig(str(ALEMBIC_INI_PATH))
        cfg.set_main_option("script_location", str(ALEMBIC_SCRIPT_LOCATION))
        alembic_command.upgrade(cfg, "head")
    except Exception:
        settings.database_url = previous_url
        raise


def _database_form_values() -> dict:
    """Pre-fills the form with whatever's already configured — parsed back
    out of settings.database_url via SQLAlchemy's own URL parser rather than
    a hand-rolled one. Never the password (write-only field, like the
    9router API key input) — render_as_string(hide_password=True) is
    SQLAlchemy's own masking, not a custom one, so it can't drift out of
    sync with how URL.create()/str() format things."""
    try:
        parsed = make_url(settings.database_url)
    except Exception:
        return {"db_host": "", "db_port": "", "db_name": "", "db_username": ""}
    return {
        "db_host": parsed.host or "",
        "db_port": str(parsed.port) if parsed.port else "",
        "db_name": parsed.database or "",
        "db_username": parsed.username or "",
    }


def _current_database_display() -> str:
    try:
        return make_url(settings.database_url).render_as_string(hide_password=True)
    except Exception:
        return settings.database_url


def restart_worker() -> dict:
    """Best-effort: start a fresh Worker FIRST, confirm it's alive, THEN stop
    any previously-running one. Starting-before-stopping (rather than the
    reverse) means a failure to start never leaves zero Workers running —
    worst case there are briefly two, never zero.

    Never raises — a failure here must not corrupt the HTTP response for the
    (already-successful, independent) API key save. Returns
    {"restarted": bool, "new_pid": int | None, "error": str | None}.
    """
    try:
        managed = _restart_managed_service("worker")
        if managed is not None:
            return managed
        old_pids = _find_worker_pids()
        new_pid = _start_worker()
        time.sleep(WORKER_START_CHECK_DELAY_SECONDS)
        if not _pid_alive(new_pid):
            return {
                "restarted": False,
                "new_pid": None,
                "error": "Worker process exited immediately after restart — check its log",
            }
        if old_pids:
            _stop_worker(old_pids)
        return {"restarted": True, "new_pid": new_pid, "error": None}
    except Exception:
        logger.exception("restart_worker: failed to restart Worker process")
        return {
            "restarted": False,
            "new_pid": None,
            "error": "internal error — see server log",
        }


def restart_watcher() -> dict:
    """Mirrors restart_worker() exactly — start fresh Watcher FIRST, confirm
    alive, THEN stop the old one; never raises. `_stop_worker()` is reused
    as-is (it's already generic over a pid list, nothing Worker-specific in
    its body)."""
    try:
        managed = _restart_managed_service("watcher")
        if managed is not None:
            return managed
        old_pids = _find_watcher_pids()
        new_pid = _start_watcher()
        time.sleep(WORKER_START_CHECK_DELAY_SECONDS)
        if not _pid_alive(new_pid):
            return {
                "restarted": False,
                "new_pid": None,
                "error": "Watcher process exited immediately after restart — check its log",
            }
        if old_pids:
            _stop_worker(old_pids)
        return {"restarted": True, "new_pid": new_pid, "error": None}
    except Exception:
        logger.exception("restart_watcher: failed to restart Watcher process")
        return {
            "restarted": False,
            "new_pid": None,
            "error": "internal error — see server log",
        }


def restart_remediation_watcher() -> dict:
    try:
        managed = _restart_managed_service("remediation_watcher")
        if managed is not None:
            return managed
        old_pids = _find_remediation_watcher_pids()
        new_pid = _start_remediation_watcher()
        time.sleep(WORKER_START_CHECK_DELAY_SECONDS)
        if not _pid_alive(new_pid):
            return {
                "restarted": False, "new_pid": None,
                "error": "AI Remediation Watcher exited immediately — check its log",
            }
        if old_pids:
            _stop_worker(old_pids)
        return {"restarted": True, "new_pid": new_pid, "error": None}
    except Exception:
        logger.exception("restart_remediation_watcher: failed")
        return {"restarted": False, "new_pid": None, "error": "internal error — see server log"}


def _dashboard_restart_script(pid: int, host: str, port: int) -> str:
    poll_iterations = int(DASHBOARD_RESTART_WAIT_SECONDS / 0.2)
    return (
        "#!/bin/sh\n"
        f"sleep {DASHBOARD_RESTART_GRACE_SECONDS}\n"
        f"kill {pid} 2>/dev/null\n"
        f"for i in $(seq 1 {poll_iterations}); do\n"
        f"  kill -0 {pid} 2>/dev/null || break\n"
        "  sleep 0.2\n"
        "done\n"
        f"cd {shlex.quote(str(PROJECT_ROOT))}\n"
        f"exec {shlex.quote(sys.executable)} -m uvicorn dashboard.app:app "
        f"--host {shlex.quote(host)} --port {port} >> {shlex.quote(str(DASHBOARD_LOG_PATH))} 2>&1\n"
    )


def _current_systemd_service_unit(cgroup_text: str | None = None) -> str | None:
    """Return the systemd service owning this process, when there is one.

    A detached child still remains in its parent's systemd cgroup.  Starting
    Uvicorn from that child therefore makes every self-restart another nested
    Dashboard process.  Reading the cgroup lets us ask PID 1 to restart the
    existing unit instead.
    """
    if cgroup_text is None:
        try:
            cgroup_text = Path("/proc/self/cgroup").read_text()
        except OSError:
            return None
    for line in cgroup_text.splitlines():
        path = line.rsplit(":", 1)[-1]
        match = SYSTEMD_SERVICE_RE.search(path)
        if match:
            return match.group(1)
    return None


def _schedule_systemd_dashboard_restart(unit: str, pid: int) -> None:
    """Schedule restart in a separate transient unit owned by PID 1."""
    subprocess.run(
        [
            "systemd-run",
            "--quiet",
            f"--unit=ceph-ai-dashboard-restart-{pid}",
            f"--on-active={DASHBOARD_RESTART_GRACE_SECONDS}s",
            "/bin/systemctl",
            "restart",
            unit,
        ],
        check=True,
        timeout=5,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def restart_dashboard_process(host: str, port: int) -> None:
    """Restarts the CURRENT Dashboard process — the one handling THIS
    request — not a separate child process like Worker/Watcher.

    That rules out the start-new-then-stop-old pattern _start_worker()/
    _start_watcher() use: this process must stay alive long enough to
    actually send the HTTP response, and a replacement can't bind the same
    host:port until this one has released it. So instead of doing the
    restart in-process, this spawns a detached watchdog shell script that
    waits past the response-flush window, kills this process by PID, polls
    until the port is actually free, then execs a fresh `uvicorn` bound to
    the same host:port. The watchdog itself is detached
    (start_new_session=True) so it survives this process's death rather
    than dying alongside it.

    Raises on failure to even launch the watchdog (e.g. can't write the temp
    script) — the caller decides how to report that; unlike restart_worker/
    restart_watcher this never "fails silently" into a dict, since the
    caller's response has to look different in each case (this one drops
    the current connection, those don't).
    """
    pid = os.getpid()
    systemd_unit = _current_systemd_service_unit()
    if systemd_unit:
        _schedule_systemd_dashboard_restart(systemd_unit, pid)
        return

    script = _dashboard_restart_script(pid, host, port)
    script_path = Path(tempfile.gettempdir()) / f"ceph_aiops_dashboard_restart_{pid}.sh"
    script_path.write_text(script)
    os.chmod(script_path, stat.S_IRWXU)
    subprocess.Popen(
        ["/bin/sh", str(script_path)],
        start_new_session=True,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _cluster_form_values() -> dict:
    return {
        "ceph_mon_nodes": settings.ceph_mon_nodes,
        "ceph_mon_hostnames": settings.ceph_mon_hostnames,
        "ceph_container_name": settings.ceph_container_name,
        "ceph_osd_nodes": settings.ceph_osd_nodes,
        "ceph_osd_container_name": settings.ceph_osd_container_name,
        "ceph_mgr_nodes": settings.ceph_mgr_nodes,
        "ceph_rgw_nodes": settings.ceph_rgw_nodes,
        "ceph_rgw_container_name": settings.ceph_rgw_container_name,
        "ceph_exec_mode": settings.ceph_exec_mode,
        "ceph_keyring_path": settings.ceph_keyring_path,
        "ssh_user": settings.ssh_user,
    }


def _backup_target_form_values() -> dict:
    """Epic 9 (Story 9.2/9.7 UI): non-secret current values for both backup
    target slots — s3_secret_key is deliberately NOT included here (same
    "leave blank in the form, blank-on-submit means keep the saved value"
    posture as router_api_key above), so a GET never leaks it back into the
    rendered HTML."""
    values = {}
    for slot in ("a", "b"):
        for suffix in (
            "transport",
            "label",
            "ssh_host",
            "ssh_user",
            "ssh_key_path",
            "ssh_landing_dir",
            "s3_endpoint",
            "s3_access_key",
            "s3_bucket",
            "immutable_lock_days",
        ):
            field = f"backup_target_{slot}_{suffix}"
            values[field] = getattr(settings, field)
    return values


def _log_intel_form_values() -> dict:
    return {field: getattr(settings, field) for field in LOG_INTEL_ENV_NAMES}


def _patch_pipeline_form_values() -> dict:
    return {
        "ceph_patch_build_node": settings.ceph_patch_build_node,
        "ceph_patch_source_dir": settings.ceph_patch_source_dir,
        "ceph_patch_build_command": settings.ceph_patch_build_command,
        "ceph_patch_output_dir": settings.ceph_patch_output_dir,
        "ceph_patch_node_staging_dir": settings.ceph_patch_node_staging_dir,
    }


def _code_repair_form_values() -> dict:
    return {
        field: getattr(settings, field)
        for field in CODE_REPAIR_ENV_NAMES
    }


def _cost_hours(raw: str | None) -> int:
    try:
        return max(1, min(int(raw or 24), 8760))
    except (TypeError, ValueError):
        return 24


def _ai_cost_overview(hours: int = 24) -> dict:
    """Return the compact total shown in Settings → Chi phí."""
    try:
        return ai_cost_summary(hours)
    except Exception:
        # Reporting must not make Settings unavailable; Budget Guard remains usable.
        logger.exception("settings: failed to load AI cost overview")
        return {
            "hours": hours,
            "calls": 0,
            "errors": 0,
            "pricing_configured": False,
            "estimated_cost_usd": None,
            "estimated_cost_vnd": None,
        }


def _settings_context(
    user: str,
    *,
    error: str | None = None,
    success: str | None = None,
    worker_restart_error: str | None = None,
    cluster_error: str | None = None,
    cluster_success: str | None = None,
    watcher_restart_error: str | None = None,
    cluster_worker_restart_error: str | None = None,
    dashboard_restart_error: str | None = None,
    cluster_values: dict | None = None,
    router_model_value: str | None = None,
    router_base_url_value: str | None = None,
    router_provider_value: str | None = None,
    router_models: list[str] | None = None,
    cleanup_error: str | None = None,
    cleanup_success: str | None = None,
    manual_worker_restart_success: str | None = None,
    manual_worker_restart_error: str | None = None,
    manual_watcher_restart_success: str | None = None,
    manual_watcher_restart_error: str | None = None,
    manual_remediation_watcher_restart_success: str | None = None,
    manual_remediation_watcher_restart_error: str | None = None,
    database_error: str | None = None,
    database_success: str | None = None,
    database_values: dict | None = None,
    database_reset_error: str | None = None,
    database_reset_success: str | None = None,
    database_migrate_error: str | None = None,
    database_migrate_success: str | None = None,
    patch_pipeline_error: str | None = None,
    patch_pipeline_success: str | None = None,
    patch_pipeline_values: dict | None = None,
    code_repair_error: str | None = None,
    code_repair_success: str | None = None,
    code_repair_values: dict | None = None,
    log_intel_error: str | None = None,
    log_intel_success: str | None = None,
    log_intel_values: dict | None = None,
    backup_target_error: str | None = None,
    backup_target_success: str | None = None,
    backup_target_values: dict | None = None,
    openstack_error: str | None = None,
    openstack_success: str | None = None,
    openstack_cluster_id: str | None = None,
    autopilot_error: str | None = None,
    autopilot_success: str | None = None,
    action_policy_error: str | None = None,
    action_policy_success: str | None = None,
    ai_budget_error: str | None = None,
    ai_budget_success: str | None = None,
    ai_budget_values: dict | None = None,
    ai_cost_hours: int = 24,
) -> dict:
    """Every form on the Settings page (API AI connection, cluster
    connection, log/data cleanup) renders from this single settings.html —
    every response must carry every form's variables, or Jinja2 silently
    renders the missing ones blank instead of showing the OTHER forms'
    current/submitted values. cluster_error/cluster_success/
    watcher_restart_error/dashboard_restart_error/cleanup_error/
    cleanup_success are deliberately separate variables per form so one
    form's result is never mistakenly shown on another's."""
    masked_key = _mask_key(settings.router_api_key) if settings.router_api_key else None
    context = {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "masked_key": masked_key,
        "router_model": (
            router_model_value if router_model_value is not None else settings.router_model
        ),
        "router_base_url": (
            router_base_url_value if router_base_url_value is not None else settings.router_base_url
        ),
        "router_provider": (
            router_provider_value
            if router_provider_value is not None
            else (settings.router_provider or DEFAULT_PROVIDER)
        ),
        "router_providers": [
            {"id": provider_id, **preset} for provider_id, preset in PROVIDER_PRESETS.items()
        ],
        "router_provider_label": PROVIDER_PRESETS[
            _normalize_provider(router_provider_value or settings.router_provider or DEFAULT_PROVIDER)
        ]["label"],
        # Populated after a successful "Xác nhận kết nối" (Step 1 -> Step 2)
        # — None on a fresh GET /settings load, when the operator hasn't
        # verified anything yet this page view.
        "router_models": router_models,
        # DISPLAY STATE vs the Step 1/2 form — connected only once every
        # piece is actually in place, not just "a key is typed somewhere".
        "router_connected": bool(
            settings.router_enabled
            and settings.router_api_key
            and settings.router_base_url
            and settings.router_model
        ),
        "codex_chat_enabled": settings.codex_chat_enabled,
        "claude_chat_enabled": settings.claude_chat_enabled,
        "error": error,
        "success": success,
        "worker_restart_error": worker_restart_error,
        "cluster_error": cluster_error,
        "cluster_success": cluster_success,
        "watcher_restart_error": watcher_restart_error,
        "cluster_worker_restart_error": cluster_worker_restart_error,
        "dashboard_restart_error": dashboard_restart_error,
        "cleanup_error": cleanup_error,
        "cleanup_success": cleanup_success,
        "manual_worker_restart_success": manual_worker_restart_success,
        "manual_worker_restart_error": manual_worker_restart_error,
        "manual_watcher_restart_success": manual_watcher_restart_success,
        "manual_watcher_restart_error": manual_watcher_restart_error,
        "manual_remediation_watcher_restart_success": manual_remediation_watcher_restart_success,
        "manual_remediation_watcher_restart_error": manual_remediation_watcher_restart_error,
        "database_error": database_error,
        "database_success": database_success,
        "database_reset_error": database_reset_error,
        "database_reset_success": database_reset_success,
        "database_migrate_error": database_migrate_error,
        "database_migrate_success": database_migrate_success,
        "current_database_display": _current_database_display(),
        "patch_pipeline_error": patch_pipeline_error,
        "patch_pipeline_success": patch_pipeline_success,
        "code_repair_error": code_repair_error,
        "code_repair_success": code_repair_success,
        "code_repair_providers": CODE_REPAIR_PROVIDERS,
        "code_repair_values": (
            code_repair_values if code_repair_values is not None else _code_repair_form_values()
        ),
        "log_intel_error": log_intel_error,
        "log_intel_success": log_intel_success,
        "log_intel_values": (
            log_intel_values if log_intel_values is not None else _log_intel_form_values()
        ),
        "backup_target_error": backup_target_error,
        "backup_target_success": backup_target_success,
        "openstack_error": openstack_error,
        "openstack_success": openstack_success,
        "autopilot_enabled": settings.autopilot_enabled,
        "autopilot_activation_unlocked": settings.autopilot_activation_unlocked,
        "autopilot_error": autopilot_error,
        "autopilot_success": autopilot_success,
        "action_policy_error": action_policy_error,
        "action_policy_success": action_policy_success,
        "ai_budget_error": ai_budget_error,
        "ai_budget_success": ai_budget_success,
        "ai_budget_values": ai_budget_values or {
            "daily": settings.ai_cost_daily_budget_usd,
            "monthly": settings.ai_cost_monthly_budget_usd,
            "hard_limit": settings.ai_cost_budget_hard_limit,
            "reserve_output": settings.ai_cost_budget_reserve_output_tokens,
        },
        "ai_cost_overview": _ai_cost_overview(ai_cost_hours),
        "playbook_registry": registry_status_rows(
            command_available=executor_commands.has_command,
        ),
    }
    with db.SessionLocal() as session:
        context["shadow_report"] = trust_engine.shadow_evaluation_report(session)
        playbook_stats = session.query(PlaybookStat).order_by(
            PlaybookStat.playbook_id, PlaybookStat.scope_key,
        ).all()
        context["playbook_stats"] = playbook_stats
        stats_by_action: dict[str, list[PlaybookStat]] = {}
        for stat in playbook_stats:
            # Old zeroed scopes remain in the DB for provenance but add no
            # useful operator signal next to the live policy row.
            if stat.proposed_count == 0 and stat.verified_count == 0:
                continue
            stats_by_action.setdefault(stat.playbook_id, []).append(stat)
        context["autopilot_clusters"] = session.query(Cluster).filter_by(is_active=True).order_by(
            Cluster.is_default.desc(), Cluster.name,
        ).all()
        overrides = (
            {row.action_id: row for row in session.query(ActionPolicyOverride).all()}
            if context["is_admin"] else {}
        )
        context["action_policy_rows"] = [
            {
                "action_id": action_id,
                "default": action_gate.classify_action(action_id).value,
                "effective": action_gate.classify_action(action_id, session=session).value,
                "override": overrides.get(action_id),
                "immutable": action_id in action_gate.DESTRUCTIVE_ACTION_IDS,
                "trust_scopes": stats_by_action.get(action_id, []),
            }
            for action_id in sorted(
                action_gate.SAFE_ACTION_IDS | action_gate.RISKY_ACTION_IDS
                | action_gate.DESTRUCTIVE_ACTION_IDS,
                key=str.casefold,
            )
        ] if context["is_admin"] else []
        openstack_clusters = session.query(Cluster).filter_by(is_active=True).order_by(Cluster.is_default.desc(), Cluster.name).all()
        default_cluster = next((row for row in openstack_clusters if row.is_default), None)
        openstack_cluster = next((row for row in openstack_clusters if row.id == openstack_cluster_id), None) or default_cluster
        context["openstack_clusters"] = openstack_clusters
        context["openstack_cluster"] = openstack_cluster
        context["openstack_controller_nodes"] = openstack_cluster.openstack_controller_nodes if openstack_cluster else ""
        context["openstack_compute_nodes"] = openstack_cluster.openstack_compute_nodes if openstack_cluster else ""
        context["openstack_ceph_config_path"] = (
            openstack_cluster.openstack_ceph_config_path if openstack_cluster else "/etc/ceph"
        )
        context["openstack_openrc_path"] = openstack_cluster.openstack_openrc_path if openstack_cluster else ""
    context.update(database_values if database_values is not None else _database_form_values())
    context.update(cluster_values if cluster_values is not None else _cluster_form_values())
    context.update(
        patch_pipeline_values if patch_pipeline_values is not None else _patch_pipeline_form_values()
    )
    context.update(
        code_repair_values if code_repair_values is not None else _code_repair_form_values()
    )
    context.update(
        backup_target_values if backup_target_values is not None else _backup_target_form_values()
    )
    # ssh_key_path is no longer an editable field on the cluster form (see
    # CLUSTER_ENV_NAMES) — always show the actual configured value here,
    # never a `cluster_values`/submitted one, since submitted dicts no
    # longer carry this key at all.
    context["ssh_key_path"] = settings.ssh_key_path
    # Shown as a read-only reference so the operator can copy it straight
    # into a NEW cluster's `~/.ssh/authorized_keys`.
    context["ssh_public_key"] = read_public_key(settings.ssh_key_path)
    context["active_section"] = _compute_active_section(context, is_admin=context["is_admin"])
    return context


def _compute_active_section(context: dict, *, is_admin: bool) -> str:
    """Which of the sidebar sections (settings.html's left nav) should be
    open when this response renders — 2026-07-24: the page used to be one
    long scroll of every card at once, so every form's own error/success
    message was always visible right where the operator just submitted it.
    Now that only one section shows at a time, whichever form actually
    produced THIS response's error/success must win over the default
    landing tab, or the operator would submit a form and see no feedback
    at all (the message would render into a hidden panel)."""
    if any(
        context.get(k)
        for k in (
            "dashboard_restart_error",
            "manual_worker_restart_success",
            "manual_worker_restart_error",
            "manual_watcher_restart_success",
            "manual_watcher_restart_error",
            "manual_remediation_watcher_restart_success",
            "manual_remediation_watcher_restart_error",
        )
    ):
        return "restart-controls"
    if any(context.get(k) for k in ("autopilot_error", "autopilot_success")):
        return "restart-controls"
    if any(context.get(k) for k in ("action_policy_error", "action_policy_success")):
        return "action-policy"
    if any(
        context.get(k)
        for k in (
            "database_error",
            "database_success",
            "database_reset_error",
            "database_reset_success",
            "database_migrate_error",
            "database_migrate_success",
        )
    ):
        return "database"
    if any(context.get(k) for k in ("error", "success", "worker_restart_error")):
        return "router"
    if any(context.get(k) for k in ("ai_budget_error", "ai_budget_success")):
        return "cost"
    if any(
        context.get(k)
        for k in (
            "cluster_error",
            "cluster_success",
            "watcher_restart_error",
            "cluster_worker_restart_error",
        )
    ):
        return "cluster"
    if any(context.get(k) for k in ("cleanup_error", "cleanup_success")):
        return "cleanup"
    if any(context.get(k) for k in ("log_intel_error", "log_intel_success")):
        return "log-intel"
    if any(context.get(k) for k in ("patch_pipeline_error", "patch_pipeline_success")):
        return "patch-pipeline"
    if any(context.get(k) for k in ("backup_target_error", "backup_target_success")):
        return "backup-targets"
    if any(context.get(k) for k in ("openstack_error", "openstack_success")):
        return "openstack"
    # Fresh GET /settings, nothing to react to yet — land on the first
    # section this account can actually see.
    if any(context.get(k) for k in ("code_repair_error", "code_repair_success")):
        return "code-repair"
    return "restart-controls" if is_admin else "router"


def _parse_node_list(raw: str) -> list[str]:
    return [h.strip() for h in raw.split(",") if h.strip()]


def _normalize_provider(raw: str) -> str:
    """Falls back to DEFAULT_PROVIDER for anything not in PROVIDER_PRESETS
    (blank, or a stale/unknown id from an old .env) — router_provider is
    purely a UI label/preset picker (see config/settings.py's comment), so
    an unrecognized value must never block saving or verifying a
    connection, unlike router_api_key/router_base_url which are load-
    bearing."""
    return raw if raw in PROVIDER_PRESETS else DEFAULT_PROVIDER


@router.get("/settings", response_class=HTMLResponse)
async def settings_form(
    request: Request,
    user: str = Depends(require_login),
    cost_hours: str | None = None,
):
    return templates.TemplateResponse(
        request, "settings.html",
        _settings_context(
            user,
            openstack_cluster_id=request.query_params.get("cluster"),
            ai_cost_hours=_cost_hours(cost_hours),
        ),
    )


@router.post("/settings/ai-budget", response_class=HTMLResponse)
async def settings_ai_budget_submit(
    request: Request,
    user: str = Depends(require_login),
    daily_budget: str = Form("0"),
    monthly_budget: str = Form("0"),
    hard_limit: str | None = Form(None),
    reserve_output_tokens: str = Form("2048"),
):
    """Persist Budget Guard settings and restart every AI worker process."""
    _require_admin_privilege(user)
    hard_limit_enabled = (
        settings.ai_cost_budget_hard_limit if hard_limit is None else hard_limit == "1"
    )
    values = {
        "daily": daily_budget.strip(), "monthly": monthly_budget.strip(),
        "hard_limit": hard_limit_enabled, "reserve_output": reserve_output_tokens.strip(),
    }

    def parse_budget(raw: str, label: str) -> float:
        try:
            value = float(raw or 0)
        except ValueError as exc:
            raise ValueError(f"{label} phải là số không âm") from exc
        if not math.isfinite(value) or value < 0 or value > 1_000_000_000:
            raise ValueError(f"{label} phải trong khoảng 0 đến 1.000.000.000 USD")
        return value

    try:
        daily = parse_budget(values["daily"], "Budget ngày")
        monthly = parse_budget(values["monthly"], "Budget tháng")
        reserve = int(values["reserve_output"] or 0)
        if reserve < 0 or reserve > 100000:
            raise ValueError("Số token output dự phòng phải trong khoảng 0 đến 100000")
        if values["hard_limit"] and not (daily or monthly):
            raise ValueError("Chặn cứng cần ít nhất Budget ngày hoặc Budget tháng")
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "settings.html", _settings_context(user, ai_budget_error=str(exc), ai_budget_values=values),
        )

    try:
        _update_env_file_batch({
            AI_COST_DAILY_BUDGET_ENV_NAME: str(daily),
            AI_COST_MONTHLY_BUDGET_ENV_NAME: str(monthly),
            AI_COST_BUDGET_HARD_LIMIT_ENV_NAME: "true" if values["hard_limit"] else "false",
            AI_COST_BUDGET_RESERVE_OUTPUT_ENV_NAME: str(reserve),
        })
        settings.ai_cost_daily_budget_usd = daily
        settings.ai_cost_monthly_budget_usd = monthly
        settings.ai_cost_budget_hard_limit = values["hard_limit"]
        settings.ai_cost_budget_reserve_output_tokens = reserve
    except Exception:
        logger.exception("settings_ai_budget_submit: failed to persist budget settings")
        return templates.TemplateResponse(
            request, "settings.html",
            _settings_context(user, ai_budget_error="Không ghi được cấu hình Budget Guard", ai_budget_values=values),
        )

    restart_errors = []
    for label, restart in (
        ("Worker", restart_worker), ("Watcher", restart_watcher),
        ("AI Remediation Watcher", restart_remediation_watcher),
    ):
        try:
            result = await asyncio.to_thread(restart)
        except Exception as exc:
            restart_errors.append(f"{label}: {exc}")
            continue
        if not result.get("restarted"):
            restart_errors.append(f"{label}: {result.get('error') or 'không khởi động lại được'}")
    success = "Đã lưu cấu hình Budget Guard và áp dụng cho các tiến trình AI."
    if restart_errors:
        success += " Cần kiểm tra/restart thủ công: " + "; ".join(restart_errors)
    return templates.TemplateResponse(
        request, "settings.html", _settings_context(user, ai_budget_success=success),
    )


@router.post("/settings/autopilot", response_class=HTMLResponse)
async def settings_autopilot_submit(
    request: Request, user: str = Depends(require_login), enabled: str = Form("0"),
):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được thay đổi Autopilot")
    desired = enabled == "1"
    reason = "Bật Autopilot từ Settings" if desired else "Tắt Autopilot từ Settings"
    previous = bool(settings.autopilot_enabled)
    if desired == previous:
        return templates.TemplateResponse(request, "settings.html", _settings_context(
            user, autopilot_success="Autopilot đã ở đúng trạng thái yêu cầu."
        ))
    try:
        _update_env_file("AUTOPILOT_ENABLED", "true" if desired else "false")
        settings.autopilot_enabled = desired
        with db.SessionLocal() as session:
            session.add(AutopilotConfigAudit(
                actor=user, previous_enabled=previous, new_enabled=desired, reason=reason,
            ))
            # The global button is the operator's primary control: keep all
            # active cluster gates in sync so "Global ENABLED" cannot look
            # active while every SAFE action is silently blocked below it.
            for cluster in session.query(Cluster).filter_by(is_active=True).all():
                cluster_previous = bool(cluster.autopilot_enabled)
                if cluster_previous == desired:
                    continue
                cluster.autopilot_enabled = desired
                session.add(AutopilotClusterConfigAudit(
                    cluster_id=cluster.id, actor=user,
                    previous_environment=cluster.autonomy_environment,
                    new_environment=cluster.autonomy_environment,
                    previous_enabled=cluster_previous, new_enabled=desired,
                    reason=f"Đồng bộ theo Global Autopilot: {reason}",
                ))
            session.commit()
    except Exception:
        logger.exception("settings_autopilot_submit: persist/audit failed")
        settings.autopilot_enabled = previous
        try:
            _update_env_file("AUTOPILOT_ENABLED", "true" if previous else "false")
        except Exception:
            logger.exception("settings_autopilot_submit: failed to restore env")
        return templates.TemplateResponse(request, "settings.html", _settings_context(
            user, autopilot_error="Không lưu/audit được thay đổi; trạng thái cũ đã được giữ."
        ))
    restart_result = await asyncio.to_thread(restart_worker)
    if not restart_result["restarted"]:
        if not desired:
            await asyncio.to_thread(_stop_worker, _find_worker_pids())
        return templates.TemplateResponse(request, "settings.html", _settings_context(
            user, autopilot_error=(
                "Đã lưu trạng thái nhưng Worker không khởi động lại được. "
                + ("Worker cũ đã bị dừng để fail-safe." if not desired else "Autopilot chưa có hiệu lực trên Worker cũ.")
            ),
        ))
    return templates.TemplateResponse(request, "settings.html", _settings_context(
        user, autopilot_success=(
            f"Đã {'bật' if desired else 'tắt'} Global + toàn bộ cluster Autopilot và khởi động lại Worker "
            f"(PID {restart_result['new_pid']})."
        ),
    ))


@router.post("/settings/autopilot/clusters/{cluster_id}", response_class=HTMLResponse)
async def settings_cluster_autopilot_submit(
    request: Request, cluster_id: str, user: str = Depends(require_login),
    environment: str = Form("production"), enabled: str = Form("0"),
    reason: str = Form(""), confirmation: str = Form(""),
):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được thay đổi Autopilot")
    environment, reason = environment.strip().lower(), reason.strip()
    desired = enabled == "1"
    if environment not in {"production", "lab"}:
        raise HTTPException(status_code=400, detail="Commissioning environment không hợp lệ")
    if len(reason) < 8:
        return templates.TemplateResponse(request, "settings.html", _settings_context(
            user, autopilot_error="Lý do thay đổi cluster gate phải có ít nhất 8 ký tự."
        ))
    expected_confirmation = (
        "ENABLE PRODUCTION AUTOPILOT" if environment == "production"
        else "ENABLE LAB AUTOPILOT"
    )
    if desired and confirmation.strip() != expected_confirmation:
        return templates.TemplateResponse(request, "settings.html", _settings_context(
            user, autopilot_error=f"Chuỗi xác nhận không chính xác; cần nhập {expected_confirmation}."
        ))
    with db.SessionLocal() as session:
        cluster = session.get(Cluster, cluster_id)
        if cluster is None or not cluster.is_active:
            raise HTTPException(status_code=404, detail="Không tìm thấy cluster đang hoạt động")
        previous_environment, previous_enabled = cluster.autonomy_environment, cluster.autopilot_enabled
        cluster.autonomy_environment, cluster.autopilot_enabled = environment, desired
        session.add(AutopilotClusterConfigAudit(
            cluster_id=cluster.id, actor=user,
            previous_environment=previous_environment, new_environment=environment,
            previous_enabled=previous_enabled, new_enabled=desired, reason=reason,
        ))
        session.commit()
    restart_result = await asyncio.to_thread(restart_worker)
    if not restart_result["restarted"]:
        if not desired:
            await asyncio.to_thread(_stop_worker, _find_worker_pids())
        return templates.TemplateResponse(request, "settings.html", _settings_context(
            user, autopilot_error="Đã lưu cluster gate nhưng Worker không restart được."
        ))
    return templates.TemplateResponse(request, "settings.html", _settings_context(
        user, autopilot_success=f"Đã cập nhật cluster gate và restart Worker (PID {restart_result['new_pid']})."
    ))


@router.post("/settings/autopilot/action-policy", response_class=HTMLResponse)
async def settings_action_policy_submit(
    request: Request, user: str = Depends(require_login),
    action_id: str = Form(""), classification: str = Form(""),
    confirmation: str = Form(""),
):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được đổi Action Policy")
    action_id, classification = action_id.strip(), classification.strip()
    known = action_gate.SAFE_ACTION_IDS | action_gate.RISKY_ACTION_IDS | action_gate.DESTRUCTIVE_ACTION_IDS
    if action_id not in known or classification not in {"SAFE", "RISKY", "DESTRUCTIVE"}:
        raise HTTPException(status_code=400, detail="Action hoặc classification không hợp lệ")
    if confirmation.strip() != "OK":
        return templates.TemplateResponse(request, "settings.html", _settings_context(
            user, action_policy_error="Cần nhập chính xác OK để áp dụng policy."
        ))
    reason = "Confirmed with OK in System Administration"
    with db.SessionLocal() as session:
        previous = action_gate.classify_action(action_id, session=session).value
        row = session.get(ActionPolicyOverride, action_id)
        if row is None:
            row = ActionPolicyOverride(action_id=action_id)
            session.add(row)
        row.classification = classification
        row.updated_by = user
        row.reason = reason
        session.add(ActionPolicyOverrideAudit(
            action_id=action_id, actor=user, previous_classification=previous,
            new_classification=classification, reason=reason,
        ))
        session.commit()
    restart_result = await asyncio.to_thread(restart_worker)
    if not restart_result["restarted"]:
        return templates.TemplateResponse(request, "settings.html", _settings_context(
            user, action_policy_error="Đã lưu policy nhưng Worker không restart được."
        ))
    return templates.TemplateResponse(request, "settings.html", _settings_context(
        user, action_policy_success=f"Đã đặt {action_id} thành {classification} và restart Worker."
    ))


@router.post("/settings/autopilot/promote-playbook", response_class=HTMLResponse)
async def settings_promote_playbook_submit(
    request: Request, user: str = Depends(require_login),
    stat_id: str = Form(""), confirmation: str = Form(""),
):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được duyệt promotion")
    if confirmation.strip() != "OK":
        return templates.TemplateResponse(request, "settings.html", _settings_context(
            user, action_policy_error="Cần nhập chính xác OK để duyệt promotion."
        ))
    with db.SessionLocal() as session:
        stat = session.get(PlaybookStat, stat_id.strip())
        if stat is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy Playbook Trust scope")
        if stat.promotion_candidate_at is None or stat.promotion_blocked_reason:
            raise HTTPException(status_code=409, detail="Playbook chưa đủ điều kiện promotion")
        stat.maturity_level = "L3"
        stat.promotion_candidate_at = None
        stat.promotion_blocked_reason = "already promoted to L3"
        playbook_id = stat.playbook_id
        session.commit()
    return templates.TemplateResponse(request, "settings.html", _settings_context(
        user, action_policy_success=f"Đã duyệt {playbook_id} lên L3."
    ))


@router.post("/settings/openstack", response_class=HTMLResponse)
async def openstack_settings_submit(
    request: Request,
    user: str = Depends(require_login),
    controller_nodes: str = Form(""),
    compute_nodes: str = Form(""),
    ceph_config_path: str = Form("/etc/ceph"),
    openrc_path: str = Form(""),
    cluster_id: str = Form(""),
):
    _require_admin_privilege(user)
    controllers = ",".join(_parse_node_list(controller_nodes))
    computes = ",".join(_parse_node_list(compute_nodes))
    destination = ceph_config_path.strip().rstrip("/") or "/etc/ceph"
    openrc = openrc_path.strip()
    if not controllers:
        return templates.TemplateResponse(request, "settings.html", _settings_context(
            user, openstack_error="Vui lòng cấu hình ít nhất một OpenStack Controller node."
        ))
    if not destination.startswith("/") or "\x00" in destination:
        return templates.TemplateResponse(request, "settings.html", _settings_context(
            user, openstack_error="Đường dẫn nhận file Ceph phải là đường dẫn tuyệt đối."
        ))
    if openrc and (not openrc.startswith("/") or "\x00" in openrc):
        return templates.TemplateResponse(request, "settings.html", _settings_context(
            user, openstack_error="Đường dẫn openrc phải là đường dẫn tuyệt đối trên Controller."
        ))
    with db.SessionLocal() as session:
        cluster = session.get(Cluster, cluster_id) if cluster_id else session.query(Cluster).filter_by(is_default=True).one_or_none()
        if cluster is None:
            return templates.TemplateResponse(request, "settings.html", _settings_context(
                user, openstack_error="Chưa có cụm Ceph mặc định để gắn cấu hình OpenStack."
            ))
        cluster.openstack_controller_nodes = controllers
        cluster.openstack_compute_nodes = computes
        cluster.openstack_ceph_config_path = destination
        cluster.openstack_openrc_path = openrc
        session.commit()
    return templates.TemplateResponse(request, "settings.html", _settings_context(
        user, openstack_success="Đã lưu cấu hình OpenStack và đường dẫn nhận file Ceph.",
        openstack_cluster_id=cluster_id,
    ))


def _test_openstack_node(host: str, ssh_user: str, ssh_key_path: str) -> tuple[str, str | None]:
    """Run a read-only SSH probe and return ``(host, error)``."""
    try:
        execute_command(host, "printf CEPH_AIOPS_OPENSTACK_OK", user=ssh_user, key_path=ssh_key_path)
        return host, None
    except ExecutorError as exc:
        return host, str(exc)


@router.post("/settings/openstack/test")
async def openstack_settings_test(
    user: str = Depends(require_login),
    controller_nodes: str = Form(""),
    compute_nodes: str = Form(""),
    cluster_id: str = Form(""),
):
    """Test SSH access to the entered OpenStack nodes without saving them."""
    _require_admin_privilege(user)
    controllers = _parse_node_list(controller_nodes)
    computes = _parse_node_list(compute_nodes)
    nodes = list(dict.fromkeys(controllers + computes))
    if not controllers:
        return {"valid": False, "message": "Vui lòng nhập ít nhất một OpenStack Controller node."}

    with db.SessionLocal() as session:
        cluster = session.get(Cluster, cluster_id) if cluster_id else session.query(Cluster).filter_by(is_default=True).one_or_none()
        if cluster is None:
            return {"valid": False, "message": "Chưa có cụm Ceph mặc định để lấy thông tin SSH."}
        ssh_user, ssh_key_path, _exec_mode, _container_name = resolve_ssh_creds(cluster)

    results = await asyncio.gather(*(
        asyncio.to_thread(_test_openstack_node, host, ssh_user, ssh_key_path) for host in nodes
    ))
    failures = [(host, error) for host, error in results if error]
    if failures:
        detail = "; ".join(f"{host}: {error}" for host, error in failures)
        return {
            "valid": False,
            "message": f"Kết nối thất bại {len(failures)}/{len(nodes)} node. {detail}",
        }
    return {
        "valid": True,
        "message": f"Kết nối SSH tới OpenStack thành công trên {len(nodes)}/{len(nodes)} node.",
    }


@router.post("/settings/openstack/vm/test")
async def openstack_vm_ssh_test(
    user: str = Depends(require_login),
    controller_nodes: str = Form(""),
    vm_ip: str = Form(""),
    vm_ssh_user: str = Form(""),
    vm_ssh_key_path: str = Form(""),
    cluster_id: str = Form(""),
):
    """Test the real Controller -> VM second SSH hop without saving settings."""
    _require_admin_privilege(user)
    controllers = _parse_node_list(controller_nodes)
    if not controllers:
        return {"valid": False, "message": "Vui lòng nhập ít nhất một OpenStack Controller node."}
    try:
        ipaddress.ip_address(vm_ip.strip())
    except ValueError:
        return {"valid": False, "message": "IP của VM không hợp lệ."}
    vm_user = vm_ssh_user.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", vm_user):
        return {"valid": False, "message": "SSH user của VM không hợp lệ."}
    vm_key = vm_ssh_key_path.strip()
    if not vm_key.startswith("/") or "\x00" in vm_key or "\n" in vm_key:
        return {
            "valid": False,
            "message": "SSH private key phải là đường dẫn tuyệt đối trên OpenStack Controller.",
        }

    with db.SessionLocal() as session:
        cluster = session.get(Cluster, cluster_id) if cluster_id else session.query(Cluster).filter_by(is_default=True).one_or_none()
        if cluster is None:
            return {"valid": False, "message": "Chưa có cụm Ceph mặc định để lấy thông tin SSH."}
        controller_user, controller_key, _exec_mode, _container = resolve_ssh_creds(cluster)

    second_hop = _vm_ssh_command(
        vm_ip.strip(), vm_user, vm_key, "printf CEPH_AIOPS_VM_SSH_OK"
    )
    failures = []
    for controller in controllers:
        try:
            await asyncio.to_thread(
                execute_command, controller, second_hop,
                user=controller_user, key_path=controller_key,
            )
            return {
                "valid": True,
                "message": f"SSH tới VM {vm_ip.strip()} thành công qua Controller {controller}.",
            }
        except ExecutorError as exc:
            failures.append(f"{controller}: {exc}")
    return {
        "valid": False,
        "message": "Không thể SSH tới VM qua các Controller. " + "; ".join(failures),
    }


@router.get("/settings/codex/status")
async def settings_codex_status(user: str = Depends(require_login)):
    executable = codex_executable()
    if executable is None:
        return {"installed": False, "authenticated": False, "enabled": False}
    try:
        await refresh_app_server_after_cli_login()
        account = await codex_app_server.account()
        model_catalog = await codex_app_server.models() if account else []
        limits = normalize_rate_limits(await codex_app_server.rate_limits()) if account else []
    except CodexAppServerError as exc:
        return {"installed": True, "authenticated": False, "enabled": settings.codex_chat_enabled, "error": str(exc)}
    authenticated = bool(account)
    return {
        "installed": True,
        "authenticated": authenticated,
        "enabled": settings.codex_chat_enabled,
        "email": account.get("email"),
        "plan_type": account.get("planType") or account.get("plan_type"),
        "model": settings.codex_chat_model,
        "models": [
            {"id": item.get("model") or item.get("id"), "label": item.get("displayName") or item.get("model") or item.get("id"), "is_default": bool(item.get("isDefault"))}
            for item in model_catalog if item.get("model") or item.get("id")
        ],
        "limits": limits,
    }


@router.post("/settings/codex/model")
async def settings_codex_model(model: str = Form(""), user: str = Depends(require_login)):
    _require_admin_privilege(user)
    submitted = model.strip()
    try:
        if not await codex_app_server.account():
            raise HTTPException(status_code=409, detail="Cần đăng nhập Codex trước khi chọn model")
        catalog = await codex_app_server.models()
    except HTTPException:
        raise
    except CodexAppServerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    available = {item.get("model") or item.get("id") for item in catalog}
    if submitted and submitted not in available:
        raise HTTPException(status_code=400, detail="Model Codex không khả dụng với tài khoản này")
    _update_env_file(CODEX_CHAT_MODEL_ENV_NAME, submitted)
    settings.codex_chat_model = submitted
    return {"saved": True, "model": submitted}


@router.post("/settings/codex/install")
async def settings_codex_install(user: str = Depends(require_login)):
    _require_admin_privilege(user)
    try:
        result = await install_codex_cli()
    except CodexAppServerError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@router.post("/settings/codex/login/start")
async def settings_codex_login_start(user: str = Depends(require_login)):
    _require_admin_privilege(user)
    try:
        result = await start_cli_device_login()
    except CodexAppServerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "login_id": result.get("loginId"),
        "verification_url": result.get("verificationUrl"),
        "user_code": result.get("userCode"),
    }


@router.post("/settings/codex/activate")
async def settings_codex_activate(user: str = Depends(require_login)):
    _require_admin_privilege(user)
    try:
        account = await codex_app_server.account()
        if not account:
            raise HTTPException(status_code=409, detail="Đăng nhập Codex chưa hoàn tất")
        _update_env_file(CODEX_CHAT_ENABLED_ENV_NAME, "true")
        _update_env_file(CLAUDE_CHAT_ENABLED_ENV_NAME, "false")
        settings.codex_chat_enabled = True
        settings.claude_chat_enabled = False
        restart_result = await asyncio.to_thread(restart_worker)
    except HTTPException:
        raise
    except CodexAppServerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("settings_codex_activate: cannot persist setting")
        raise HTTPException(status_code=500, detail="Không ghi được file cấu hình") from exc
    return {"enabled": True, "worker_restarted": restart_result["restarted"]}


@router.post("/settings/codex/logout")
async def settings_codex_logout(user: str = Depends(require_login)):
    _require_admin_privilege(user)
    try:
        await codex_app_server.logout()
        _update_env_file(CODEX_CHAT_ENABLED_ENV_NAME, "false")
        settings.codex_chat_enabled = False
    except CodexAppServerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"enabled": False}


@router.get("/settings/claude/status")
async def settings_claude_status(user: str = Depends(require_login)):
    try:
        result = await claude_status()
    except ClaudeCLIError as exc:
        return {"installed": claude_executable() is not None, "authenticated": False,
                "enabled": settings.claude_chat_enabled, "error": str(exc)}
    result["enabled"] = settings.claude_chat_enabled
    result["model"] = settings.claude_chat_model
    result["models"] = CLAUDE_MODELS
    result["effort"] = settings.claude_chat_effort
    result["efforts"] = CLAUDE_EFFORTS
    result["limits"] = normalize_rate_limits(result.pop("rate_limits", None))
    return result


@router.post("/settings/claude/model")
async def settings_claude_model(
    model: str = Form("default"), effort: str = Form("auto"),
    user: str = Depends(require_login),
):
    _require_admin_privilege(user)
    submitted = model.strip() or "default"
    submitted_effort = effort.strip().lower() or "auto"
    try:
        status = await claude_status()
    except ClaudeCLIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not status.get("authenticated"):
        raise HTTPException(status_code=409, detail="Cần đăng nhập Claude trước khi chọn model")
    if submitted not in {item["id"] for item in CLAUDE_MODELS}:
        raise HTTPException(status_code=400, detail="Model Claude không hợp lệ")
    if submitted_effort not in {item["id"] for item in CLAUDE_EFFORTS}:
        raise HTTPException(status_code=400, detail="Effort Claude không hợp lệ")
    _update_env_file(CLAUDE_CHAT_MODEL_ENV_NAME, submitted)
    _update_env_file(CLAUDE_CHAT_EFFORT_ENV_NAME, submitted_effort)
    settings.claude_chat_model = submitted
    settings.claude_chat_effort = submitted_effort
    return {"saved": True, "model": submitted, "effort": submitted_effort}


@router.post("/settings/claude/install")
async def settings_claude_install(user: str = Depends(require_login)):
    _require_admin_privilege(user)
    try:
        return await install_claude_cli()
    except ClaudeCLIError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/settings/claude/login/start")
async def settings_claude_login_start(user: str = Depends(require_login)):
    _require_admin_privilege(user)
    try:
        return await start_claude_login()
    except ClaudeCLIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/settings/claude/activate")
async def settings_claude_activate(user: str = Depends(require_login)):
    _require_admin_privilege(user)
    try:
        status = await claude_status()
        if not status.get("authenticated"):
            raise HTTPException(status_code=409, detail="Đăng nhập Claude chưa hoàn tất")
        _update_env_file(CLAUDE_CHAT_ENABLED_ENV_NAME, "true")
        _update_env_file(CODEX_CHAT_ENABLED_ENV_NAME, "false")
        settings.claude_chat_enabled = True
        settings.codex_chat_enabled = False
        restart_result = await asyncio.to_thread(restart_worker)
    except HTTPException:
        raise
    except ClaudeCLIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"enabled": True, "worker_restarted": restart_result["restarted"]}


@router.post("/settings/claude/login/complete")
async def settings_claude_login_complete(
    authentication_code: str = Form(""), user: str = Depends(require_login)
):
    _require_admin_privilege(user)
    try:
        status = await submit_claude_authentication_code(authentication_code)
    except ClaudeCLIError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return status


@router.post("/settings/claude/logout")
async def settings_claude_logout(user: str = Depends(require_login)):
    _require_admin_privilege(user)
    try:
        await claude_logout()
        _update_env_file(CLAUDE_CHAT_ENABLED_ENV_NAME, "false")
        settings.claude_chat_enabled = False
    except ClaudeCLIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"enabled": False}


@router.post("/settings/9router/verify")
async def settings_verify_router(
    user: str = Depends(require_login),
    router_api_key: str = Form(""),
    router_base_url: str = Form(""),
    router_provider: str = Form(DEFAULT_PROVIDER),
):
    """Backs the Settings page's Step 1 "[Xác nhận kết nối]" button — a
    single GET /v1/models round trip (verify_router_connection above) both
    proves the just-typed (not-yet-saved) key/base_url work AND returns the
    real model catalog for Step 2, per spec. JSON in/out since this is only
    ever called from JS, never a full page load.

    A blank router_api_key/router_base_url falls back to whatever is already
    saved — the API key is a password field the browser never pre-fills with
    the real (masked) value, so without this fallback the "[Đổi model]" flow
    (re-verify against an already-connected AI API to refresh the model
    list) could never work without forcing the operator to retype a key
    they're not actually changing.

    router_provider (Claude/Codex/OpenRouter/9router — see PROVIDER_PRESETS)
    is only echoed back for the error message below; it never changes how
    the connection itself is verified — shared/router_client.py builds the
    same generic OpenAI-compatible client for every provider."""
    submitted_key = router_api_key.strip() or settings.router_api_key
    submitted_base_url = router_base_url.strip() or settings.router_base_url
    provider = _normalize_provider(router_provider.strip())
    provider_label = PROVIDER_PRESETS[provider]["label"]
    if not submitted_key:
        raise HTTPException(status_code=400, detail="Cần nhập API key trước khi kiểm tra kết nối")
    if not submitted_base_url:
        raise HTTPException(
            status_code=400, detail=f"Cần nhập Base URL ({provider_label}) trước khi kiểm tra kết nối"
        )

    is_valid, message, models = await verify_router_connection(submitted_key, submitted_base_url)
    return {"valid": is_valid, "message": message, "models": models}


@router.post("/settings/9router/save", response_class=HTMLResponse)
async def settings_save_router(
    request: Request,
    user: str = Depends(require_login),
    router_api_key: str = Form(""),
    router_base_url: str = Form(""),
    router_model_id: str = Form(""),
    router_provider: str = Form(DEFAULT_PROVIDER),
):
    # Same blank-falls-back-to-saved-value semantics as the verify route
    # above — Step 2's "[Lưu cấu hình]" submit and the "[Đổi model]" re-save
    # both go through here, and the latter never has a retyped key/base_url.
    submitted_key = router_api_key.strip() or settings.router_api_key
    submitted_base_url = router_base_url.strip() or settings.router_base_url
    submitted_model = router_model_id.strip()
    provider = _normalize_provider(router_provider.strip())
    provider_label = PROVIDER_PRESETS[provider]["label"]

    if not submitted_key:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(user, error="API key không được để trống", router_provider_value=provider),
        )
    if not submitted_base_url:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                error=f"Base URL ({provider_label}) không được để trống",
                router_base_url_value=submitted_base_url,
                router_provider_value=provider,
            ),
        )
    if not submitted_model:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                error="Chưa chọn model — hãy xác nhận kết nối rồi chọn một model trước khi lưu",
                router_base_url_value=submitted_base_url,
                router_provider_value=provider,
            ),
        )

    # Re-verify server-side with the values about to be persisted — the
    # operator's browser already saw this succeed via /settings/9router/verify,
    # but that response is not trusted input; this is the same cheap
    # GET /v1/models call, and it also gives us the authoritative model list
    # to check submitted_model against below.
    is_valid, reason, models = await verify_router_connection(submitted_key, submitted_base_url)
    if not is_valid:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                error=f"Không thể lưu — kết nối {provider_label} không còn hợp lệ"
                + (f": {reason}" if reason else ""),
                router_base_url_value=submitted_base_url,
                router_provider_value=provider,
            ),
        )
    if models is not None and submitted_model not in models:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                error=f"Model '{submitted_model}' không khả dụng trên {provider_label}",
                router_base_url_value=submitted_base_url,
                router_provider_value=provider,
                router_models=models,
            ),
        )

    # Save is independent of, and must complete before, any attempt to
    # restart Worker — a restart failure below must never undo or obscure
    # the fact that the config itself was saved successfully. A write
    # failure here (permissions, disk full) must not fall through to a raw
    # 500.
    try:
        _update_env_file_batch(
            {
                ROUTER_API_KEY_ENV_NAME: submitted_key,
                ROUTER_MODEL_ENV_NAME: submitted_model,
                ROUTER_BASE_URL_ENV_NAME: submitted_base_url,
                ROUTER_ENABLED_ENV_NAME: "true",
                ROUTER_PROVIDER_ENV_NAME: provider,
            }
        )
        settings.router_api_key = submitted_key
        settings.router_model = submitted_model
        settings.router_base_url = submitted_base_url
        settings.router_enabled = True
        settings.router_provider = provider
    except Exception:
        logger.exception("settings_save_router: failed to persist API AI config to .env")
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                error="Không ghi được file cấu hình — kiểm tra quyền ghi trên server",
                router_base_url_value=submitted_base_url,
                router_provider_value=provider,
            ),
        )

    # restart_worker() is blocking (subprocess/os.kill/sleep) — run it off
    # the event loop so one operator saving a key doesn't freeze the whole
    # Dashboard (other users' pages, the incidents WebSocket) for seconds.
    restart_result = await asyncio.to_thread(restart_worker)
    if restart_result["restarted"]:
        success = f"Đã lưu — chatbox sẵn sàng sử dụng. Đã khởi động lại Worker (PID {restart_result['new_pid']})."
        worker_restart_error = None
    else:
        success = "Đã lưu — chatbox sẵn sàng sử dụng."
        worker_restart_error = (
            "Không thể tự khởi động lại Worker — vui lòng khởi động lại thủ công "
            "(xem log server để biết chi tiết)."
        )

    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(user, success=success, worker_restart_error=worker_restart_error),
    )


@router.post("/settings/9router/disconnect", response_class=HTMLResponse)
async def settings_disconnect_router(request: Request, user: str = Depends(require_login)):
    """Backs the DISPLAY STATE's "[Huỷ kết nối]" button — clears the saved
    API AI config entirely (api_key/base_url/model, router_enabled=False)
    so the Settings page falls back to showing Step 1 again, and the
    chatbox falls back to MISSING_AI_CONFIG_MESSAGE until reconnected.
    Deliberately leaves router_provider untouched — reconnecting later
    should still default back to whichever connection type (Claude/Codex/
    OpenRouter/9router) the operator had picked, not reset to 9router."""
    try:
        _update_env_file_batch(
            {
                ROUTER_API_KEY_ENV_NAME: "",
                ROUTER_MODEL_ENV_NAME: "",
                ROUTER_BASE_URL_ENV_NAME: "",
                ROUTER_ENABLED_ENV_NAME: "false",
            }
        )
        settings.router_api_key = ""
        settings.router_model = ""
        settings.router_base_url = ""
        settings.router_enabled = False
    except Exception:
        logger.exception("settings_disconnect_router: failed to clear API AI config in .env")
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user, error="Không ghi được file cấu hình — kiểm tra quyền ghi trên server"
            ),
        )

    # Mirrors settings_save_router — restart Worker so it picks up the
    # cleared/disabled config immediately rather than keeping the old key
    # in memory until its next natural restart.
    await asyncio.to_thread(restart_worker)

    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(user, success="Đã huỷ kết nối."),
    )


@router.post("/settings/cluster", response_class=HTMLResponse)
async def cluster_settings_submit(
    request: Request,
    user: str = Depends(require_login),
    ceph_mon_nodes: str = Form(""),
    ceph_mon_hostnames: str = Form(""),
    ceph_container_name: str = Form(""),
    ceph_osd_nodes: str = Form(""),
    ceph_osd_container_name: str = Form(""),
    ceph_mgr_nodes: str = Form(""),
    ceph_rgw_nodes: str = Form(""),
    ceph_rgw_container_name: str = Form(""),
    ceph_exec_mode: str = Form("docker"),
    ceph_keyring_path: str = Form(""),
    ssh_user: str = Form(""),
):
    submitted = {
        "ceph_mon_nodes": ceph_mon_nodes.strip(),
        "ceph_mon_hostnames": ceph_mon_hostnames.strip(),
        "ceph_container_name": ceph_container_name.strip(),
        "ceph_osd_nodes": ceph_osd_nodes.strip(),
        "ceph_osd_container_name": ceph_osd_container_name.strip(),
        "ceph_mgr_nodes": ceph_mgr_nodes.strip(),
        "ceph_rgw_nodes": ceph_rgw_nodes.strip(),
        "ceph_rgw_container_name": ceph_rgw_container_name.strip(),
        "ceph_exec_mode": ceph_exec_mode.strip() or "docker",
        "ceph_keyring_path": ceph_keyring_path.strip(),
        "ssh_user": ssh_user.strip(),
    }
    mon_nodes_list = _parse_node_list(submitted["ceph_mon_nodes"])

    if submitted["ceph_exec_mode"] not in VALID_EXEC_MODES:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                cluster_error=f"Kiểu deploy không hợp lệ: {submitted['ceph_exec_mode']!r}",
                cluster_values=submitted,
            ),
        )

    # Neither "none" (ceph-deploy/package install) nor "cephadm" (`cephadm
    # shell` infers its own container/keyring — verified against a real
    # cephadm/reef cluster) has a container to name, so MON container is
    # only required for docker/podman.
    container_required = submitted["ceph_exec_mode"] not in ("none", "cephadm")

    # AC #1: MON nodes/SSH user are always required; MON container is
    # required unless exec mode is "none"/"cephadm". OSD/MGR nodes may be
    # left blank (not every operator needs OSD/MGR log collection configured
    # immediately). SSH key path is NOT part of this form anymore — it's a
    # one-time server-side setting (.env), checked separately below.
    if (
        not mon_nodes_list
        or (container_required and not submitted["ceph_container_name"])
        or not submitted["ssh_user"]
        or not submitted["ceph_keyring_path"]
    ):
        message = (
            "Vui lòng điền đủ MON nodes, MON container, SSH user và Ceph keyring path."
            if container_required
            else "Vui lòng điền đủ MON nodes, SSH user và Ceph keyring path."
        )
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(user, cluster_error=message, cluster_values=submitted),
        )

    # SSH key path is configured once via .env, not through this form — an
    # operator can't "fix" a missing/bad one by retyping it here, so this is
    # checked as a distinct server-config problem rather than folded into
    # the required-fields message above.
    if not settings.ssh_key_path:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                cluster_error=(
                    "Chưa cấu hình SSH key path trên server — đặt SSH_KEY_PATH trong "
                    "file .env rồi khởi động lại Dashboard."
                ),
                cluster_values=submitted,
            ),
        )

    # AC #4: check the SSH key path BEFORE attempting any SSH connection —
    # a bad path must fail with a clear message, not a confusing paramiko error.
    key_error = ssh_key_path_error(settings.ssh_key_path)
    if key_error:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(user, cluster_error=key_error, cluster_values=submitted),
        )

    # AC #2: test the connection with the SUBMITTED (not-yet-saved) values —
    # paramiko I/O is blocking, run off the event loop like restart_worker().
    # ssh_key_path itself isn't "submitted" (see above) — always the current
    # server-configured one.
    try:
        await asyncio.to_thread(
            validate_ceph_keyring_with,
            mon_nodes_list, submitted["ceph_container_name"], submitted["ssh_user"],
            settings.ssh_key_path, submitted["ceph_exec_mode"], submitted["ceph_keyring_path"],
        )
        await asyncio.to_thread(
            query_cluster_health_with,
            mon_nodes_list,
            submitted["ceph_container_name"],
            submitted["ssh_user"],
            settings.ssh_key_path,
            submitted["ceph_exec_mode"],
        )
    except CephQueryError as exc:
        logger.warning("cluster_settings_submit: connection test failed: %s", exc)
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user, cluster_error=f"Không kết nối được tới cụm: {exc}", cluster_values=submitted
            ),
        )

    # AC #3: test succeeded — persist, then update the running Dashboard's
    # own `settings`, then restart Watcher to apply the new config. Save
    # failure must not fall through to a raw 500.
    try:
        _update_env_file_batch(
            {env_name: submitted[field] for field, env_name in CLUSTER_ENV_NAMES.items()}
        )
        for field in CLUSTER_ENV_NAMES:
            setattr(settings, field, submitted[field])
        # Dashboard and the multi-cluster Watcher resolve the default
        # cluster through this DB row, not directly through ``settings``.
        # Keep that mirror current before either process is restarted.
        with db.SessionLocal() as session:
            sync_default_cluster_from_settings(session)
    except Exception:
        logger.exception("cluster_settings_submit: failed to persist cluster config")
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                cluster_error="Không ghi được file cấu hình — kiểm tra quyền ghi trên server",
                cluster_values=submitted,
            ),
        )

    restart_result = await asyncio.to_thread(restart_watcher)
    remediation_restart_result = await asyncio.to_thread(restart_remediation_watcher)
    if restart_result["restarted"]:
        cluster_success = (
            f"Kết nối thành công. Đã khởi động lại Watcher (PID {restart_result['new_pid']})."
        )
        watcher_restart_error = None
    else:
        cluster_success = "Kết nối thành công."
        watcher_restart_error = (
            "Không thể tự khởi động lại Watcher — vui lòng khởi động lại thủ công "
            "(xem log server để biết chi tiết)."
        )
    if not remediation_restart_result["restarted"]:
        watcher_restart_error = (
            (watcher_restart_error + " ") if watcher_restart_error else ""
        ) + "Không thể tự khởi động lại AI Remediation Watcher."

    # 2026-07-24 fix: Worker also reads ceph_exec_mode/ceph_mon_nodes/etc
    # directly (worker/executor/commands.py's package-based upgrade AND
    # patch-pipeline command builders — both call
    # shared.cluster_nodes.configured_nodes() and/or check
    # settings.ceph_exec_mode) — until now only Watcher got restarted here,
    # so a Worker that had been running since BEFORE this save kept using
    # its stale in-memory ceph_exec_mode/node list indefinitely (observed
    # live: every package-based-upgrade approval failed with "no Command
    # for action_id=... — marking this node failed" because Worker still
    # thought ceph_exec_mode was something other than "none", the value
    # just saved here). Same explicit env= override technique restart_worker
    # already needs for router settings (see _start_worker's docstring).
    worker_restart_result = await asyncio.to_thread(restart_worker)
    cluster_worker_restart_error = (
        None
        if worker_restart_result["restarted"]
        else (
            "Không thể tự khởi động lại Worker — vui lòng khởi động lại thủ công ở mục 'Tiến "
            "trình hệ thống' (xem log server để biết chi tiết). Cho tới lúc đó, Worker vẫn đang "
            "dùng cấu hình cụm CŨ khi thực thi các hành động đã duyệt."
        )
    )

    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(
            user,
            cluster_success=cluster_success,
            watcher_restart_error=watcher_restart_error,
            cluster_worker_restart_error=cluster_worker_restart_error,
        ),
    )


# The old always-visible "Xoá SSH host key cũ của 1 node" form that used to
# live here (POST /settings/cluster/forget-host-key) moved to
# dashboard/routes/deploy_cluster.py's POST /deploy-cluster/forget-host-key
# — it's now a hidden control that only appears inline on the Deploy
# Cluster page's log when _phase_ssh_check actually hits a host-key
# mismatch, right where an operator would already be looking, instead of a
# permanent Settings-page form for something only relevant in that one
# specific failure. watcher/ceph_client.py::forget_host_key() itself is
# unchanged — only which route/page calls it moved.


@router.post("/settings/database/test")
async def settings_test_database(
    user: str = Depends(require_login),
    db_host: str = Form(""),
    db_port: str = Form(str(DEFAULT_POSTGRES_PORT)),
    db_name: str = Form(""),
    db_username: str = Form(""),
    db_password: str = Form(""),
    database_url_raw: str = Form(""),
):
    """Backs the "Kiểm tra kết nối" button — raw SELECT 1 only, no
    migration, no write to .env, so an operator can try several
    host/port/credential combos (or a pasted Database URL — see
    _resolve_database_url) before committing to one. Admin-gated like every
    other control in this section (see auth.is_admin_user) — saving a
    database connection always ends in a Dashboard self-restart (see
    settings_save_database below), same privilege boundary as the manual
    restart buttons."""
    _require_admin_privilege(user)
    url, error = _resolve_database_url(db_host, db_port, db_name, db_username, db_password, database_url_raw)
    if error:
        return {"valid": False, "message": error}
    valid, message = await asyncio.to_thread(_test_database_connection, url)
    return {"valid": valid, "message": message}


@router.post("/settings/database/save", response_class=HTMLResponse)
async def settings_save_database(
    request: Request,
    user: str = Depends(require_login),
    db_host: str = Form(""),
    db_port: str = Form(str(DEFAULT_POSTGRES_PORT)),
    db_name: str = Form(""),
    db_username: str = Form(""),
    db_password: str = Form(""),
    database_url_raw: str = Form(""),
):
    """Switches the app's storage backend to a PostgreSQL database — the
    Settings page's "Kết nối Database" section (Postgres only; see this
    module's `_build_postgres_url` docstring for why Mongo/MySQL aren't
    offered here). Sequence, each step gating the next:
    1. re-test the connection server-side (never trust the browser's own
       /settings/database/test round trip as proof — same posture as
       settings_save_router re-verifying 9router before saving)
    2. run `alembic upgrade head` against the CANDIDATE database — a brand
       new Postgres database has no tables at all until this runs
    3. persist DATABASE_URL to .env (only after 1+2 succeed — never leaves
       .env pointed at an empty/unreachable database)
    4. restart Worker + Watcher (separate processes — each binds its own
       shared.db.engine at import time, so only a real process restart
       picks up the new DATABASE_URL, same reasoning as
       CLUSTER_ENV_NAMES/ROUTER_*_ENV_NAME restarts elsewhere in this file)
    5. restart the Dashboard itself LAST — this very process's own
       shared.db.engine/SessionLocal (module-level singletons, see
       shared/db.py) are just as bound to the OLD url as Worker/Watcher's,
       so without this the Dashboard would keep querying the OLD database
       until someone restarts it by hand. Uses the same watchdog-script
       self-restart as restart_dashboard_submit, so the response the
       browser actually sees is "restarting.html", not a rendered success
       message on this page.
    """
    _require_admin_privilege(user)

    submitted_values = {
        "db_host": db_host.strip(),
        "db_port": db_port.strip(),
        "db_name": db_name.strip(),
        "db_username": db_username.strip(),
    }

    url, error = _resolve_database_url(db_host, db_port, db_name, db_username, db_password, database_url_raw)
    if error:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(user, database_error=error, database_values=submitted_values),
        )

    valid, message = await asyncio.to_thread(_test_database_connection, url)
    if not valid:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                database_error=f"Không kết nối được database: {message}",
                database_values=submitted_values,
            ),
        )

    try:
        await asyncio.to_thread(_run_alembic_upgrade_head, url)
    except Exception as exc:
        logger.exception("settings_save_database: alembic upgrade head failed against candidate DB")
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                database_error=f"Kết nối được nhưng chạy migration thất bại: {readable_exception_message(exc)}",
                database_values=submitted_values,
            ),
        )

    # settings.database_url is already the new url at this point (set by
    # _run_alembic_upgrade_head on its success path) — persist it to .env
    # so the NEXT process start (Worker/Watcher/Dashboard, right below) is
    # the FIRST one to ever pick this up, not this request's own settings
    # object doing double duty as the source of truth.
    try:
        _update_env_file_batch({DATABASE_URL_ENV_NAME: url.render_as_string(hide_password=False)})
    except Exception:
        logger.exception("settings_save_database: failed to persist DATABASE_URL to .env")
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                database_error="Không ghi được file cấu hình — kiểm tra quyền ghi trên server",
                database_values=submitted_values,
            ),
        )

    await asyncio.to_thread(restart_worker)
    await asyncio.to_thread(restart_watcher)
    await asyncio.to_thread(restart_remediation_watcher)

    dashboard_host = request.url.hostname or "127.0.0.1"
    dashboard_port = request.url.port or 80
    try:
        await asyncio.to_thread(restart_dashboard_process, dashboard_host, dashboard_port)
    except Exception:
        logger.exception("settings_save_database: failed to launch Dashboard restart watchdog")
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                database_success=(
                    "Đã lưu và migrate database mới — Worker/Watcher đã khởi động lại, nhưng KHÔNG tự "
                    "khởi động lại được Dashboard. Vào mục 'Tiến trình hệ thống' phía trên để khởi động "
                    "lại Dashboard thủ công (Dashboard vẫn đang đọc database CŨ cho tới lúc đó)."
                ),
            ),
        )
    return templates.TemplateResponse(request, "restarting.html", {"user": user})


def _reset_database_connection() -> None:
    """Disposes the Dashboard's current DB connection pool and replaces it
    with a fresh one bound to the SAME settings.database_url (not a
    switch — see settings_save_database above for that) — useful when the
    pool is holding a connection stuck against a misbehaving database
    server (e.g. a hung backend on the DB host itself, observed once
    against a test Postgres instance whose underlying storage had stalled;
    `engine.dispose()` closes every pooled DBAPI connection immediately
    rather than waiting for whatever query is stuck on it to time out).

    Deliberately does NOT touch Worker/Watcher — each holds its own
    separate `db.engine`/`SessionLocal` in its own process (same reasoning
    _run_alembic_upgrade_head's docstring gives for why a database SWITCH
    needs to restart all three processes) — this lighter action only
    resets the Dashboard's own connection. If Worker/Watcher also need a
    fresh connection, restart them individually via 'Tiến trình hệ thống'
    above.

    Rebinds the SAME `db.engine`/`db.SessionLocal` module attributes the
    test suite's `dashboard_client` fixture already swaps for an isolated
    test DB (shared/db.py) — proof this rebind-in-place technique works
    without restarting the process.
    """
    old_engine = db.engine
    db.engine = db.make_engine()
    db.SessionLocal = sessionmaker(bind=db.engine, autoflush=False, autocommit=False)
    old_engine.dispose()


@router.post("/settings/database/reset-connection", response_class=HTMLResponse)
async def settings_reset_database_connection(request: Request, user: str = Depends(require_login)):
    _require_admin_privilege(user)
    try:
        await asyncio.to_thread(_reset_database_connection)
    except Exception as exc:
        logger.exception("settings_reset_database_connection: failed to reset DB connection")
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(user, database_reset_error=f"Không reset được kết nối: {readable_exception_message(exc)}"),
        )
    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(user, database_reset_success="Đã khởi động lại kết nối tới database hiện tại."),
    )


@router.post("/settings/database/migrate", response_class=HTMLResponse)
async def settings_run_migrations(request: Request, user: str = Depends(require_login)):
    """Admin-only "Chạy migration (alembic upgrade head)" button — lets an
    admin bring the CURRENT database's schema up to date with whatever
    this deployment's code now expects (e.g. a table a newer code update
    added, like shared/models.py::VolumeMetric) without needing SSH/
    terminal access to run `alembic upgrade head` by hand. A real,
    recurring gap: this app has never auto-run migrations on startup (by
    design — a schema change should be a deliberate, visible admin action,
    not something that silently happens on every process restart), so a
    `git pull` alone leaves an old deployment's DB missing whatever new
    table/column the new code references — that has already broken this
    exact page's own cleanup form once in practice.

    Unlike settings_save_database above, this does NOT switch which
    database is configured (same url before and after, from
    settings.database_url — _run_alembic_upgrade_head's re-assignment of
    it below is a same-value no-op), so it never needs to restart Worker/
    Watcher/Dashboard the way a real switch does (those restarts exist
    specifically because a SWITCH leaves other processes pointed at the
    OLD db; nothing here changes which db anyone is pointed at)."""
    _require_admin_privilege(user)
    try:
        url = make_url(settings.database_url)
        await asyncio.to_thread(_run_alembic_upgrade_head, url)
    except Exception as exc:
        logger.exception("settings_run_migrations: alembic upgrade head failed")
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user, database_migrate_error=f"Chạy migration thất bại: {readable_exception_message(exc)}"
            ),
        )
    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(
            user,
            database_migrate_success="Đã chạy migration thành công — schema database đã cập nhật mới nhất.",
        ),
    )


@router.post("/settings/patch-pipeline", response_class=HTMLResponse)
async def patch_pipeline_settings_submit(
    request: Request,
    user: str = Depends(require_login),
    ceph_patch_build_node: str = Form(""),
    ceph_patch_source_dir: str = Form(""),
    ceph_patch_build_command: str = Form(""),
    ceph_patch_output_dir: str = Form(""),
    ceph_patch_node_staging_dir: str = Form(""),
):
    """Configures the Ceph patch build & deploy pipeline (Vá lỗi Ceph page,
    dashboard/routes/patch.py) — where the build server is and how to build
    RPMs on it. See config/settings.py's ceph_patch_* fields for what each
    one means; ssh_user/ssh_key_path are NOT part of this form (same shared
    SSH credential already used for every Ceph node — see "Kết nối cụm
    Ceph" above).

    No connection test here (unlike "Kết nối cụm Ceph"/"Kết nối Database")
    — the build server doesn't need to be reachable just to SAVE its
    address; worker/executor/commands.py's _patch_build_and_stage_command
    already validates all of this is non-blank and fails loudly (not a
    guess) if the build server itself turns out to be unreachable when a
    patch build is actually proposed."""
    _require_admin_privilege(user)

    submitted = {
        "ceph_patch_build_node": ceph_patch_build_node.strip(),
        "ceph_patch_source_dir": ceph_patch_source_dir.strip(),
        "ceph_patch_build_command": ceph_patch_build_command.strip(),
        "ceph_patch_output_dir": ceph_patch_output_dir.strip(),
        "ceph_patch_node_staging_dir": ceph_patch_node_staging_dir.strip(),
    }

    try:
        _update_env_file_batch(
            {env_name: submitted[field] for field, env_name in PATCH_PIPELINE_ENV_NAMES.items()}
        )
        for field in PATCH_PIPELINE_ENV_NAMES:
            setattr(settings, field, submitted[field])
    except Exception:
        logger.exception("patch_pipeline_settings_submit: failed to persist config to .env")
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                patch_pipeline_error="Không ghi được file cấu hình — kiểm tra quyền ghi trên server",
                patch_pipeline_values=submitted,
            ),
        )

    await asyncio.to_thread(restart_worker)

    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(
            user,
            patch_pipeline_success="Đã lưu cấu hình — Worker đã khởi động lại để áp dụng ngay.",
        ),
    )


@router.post("/settings/code-repair", response_class=HTMLResponse)
async def code_repair_settings_submit(
    request: Request,
    user: str = Depends(require_login),
    code_repair_planner_provider: str = Form("auto"),
    code_repair_planner_model: str = Form(""),
    code_repair_implementer_provider: str = Form("auto"),
    code_repair_implementer_model: str = Form(""),
    code_repair_max_review_rounds: str = Form("2"),
):
    """Persist the two AI roles used by the external repair supervisor."""
    _require_admin_privilege(user)
    values = {
        "code_repair_planner_provider": code_repair_planner_provider.strip().lower(),
        "code_repair_planner_model": code_repair_planner_model.strip(),
        "code_repair_implementer_provider": code_repair_implementer_provider.strip().lower(),
        "code_repair_implementer_model": code_repair_implementer_model.strip(),
        "code_repair_max_review_rounds": code_repair_max_review_rounds.strip(),
    }

    def fail(message: str):
        return templates.TemplateResponse(
            request, "settings.html",
            _settings_context(user, code_repair_error=message, code_repair_values=values),
        )

    for field in ("code_repair_planner_provider", "code_repair_implementer_provider"):
        if values[field] not in CODE_REPAIR_PROVIDERS:
            return fail(f"{field}: provider phải là auto, codex hoặc claude.")
    try:
        rounds = int(values["code_repair_max_review_rounds"])
    except ValueError:
        return fail("Số vòng review phải là số nguyên từ 0 đến 5.")
    if not 0 <= rounds <= CODE_REPAIR_MAX_REVIEW_ROUNDS:
        return fail("Số vòng review phải nằm trong khoảng 0 đến 5.")
    values["code_repair_max_review_rounds"] = rounds

    try:
        _update_env_file_batch({
            env_name: str(values[field])
            for field, env_name in CODE_REPAIR_ENV_NAMES.items()
        })
        for field in CODE_REPAIR_ENV_NAMES:
            setattr(settings, field, values[field])
    except Exception:
        logger.exception("code_repair_settings_submit: failed to persist config")
        return fail("Không ghi được cấu hình — kiểm tra quyền ghi file .env")

    return templates.TemplateResponse(
        request, "settings.html",
        _settings_context(
            user,
            code_repair_success=(
                "Đã lưu cấu hình hai AI. Supervisor sẽ dùng cấu hình này ở lần chạy kế tiếp."
            ),
        ),
    )


def _validate_backup_target_slot(slot: str, submitted: dict) -> str | None:
    """Returns an error message, or None if `slot`'s submitted fields are
    internally consistent for its chosen transport. transport="" (chưa
    dùng) skips validation entirely — leaving a slot unconfigured is valid
    (PRD FR-5 only requires 2 copies once an operator actually sets both
    up, not that both must be configured immediately)."""
    label = submitted[f"backup_target_{slot}_label"] or f"Slot {slot.upper()}"
    transport = submitted[f"backup_target_{slot}_transport"]
    if transport not in ("", "ssh", "s3"):
        return f"{label}: kiểu kết nối không hợp lệ ({transport!r})"
    if transport == "ssh":
        required = ("ssh_host", "ssh_user", "ssh_key_path", "ssh_landing_dir")
        if any(not submitted[f"backup_target_{slot}_{f}"] for f in required):
            return f"{label}: cần điền đủ Host, User, SSH key path, Thư mục lưu trữ (SSH)"
    elif transport == "s3":
        required = ("s3_access_key", "s3_secret_key", "s3_bucket")
        if any(not submitted[f"backup_target_{slot}_{f}"] for f in required):
            return f"{label}: cần điền đủ Access key, Secret key, Bucket (S3)"
    return None


@router.post("/settings/backup-targets", response_class=HTMLResponse)
async def backup_targets_settings_submit(
    request: Request,
    user: str = Depends(require_login),
    backup_target_a_transport: str = Form(""),
    backup_target_a_label: str = Form(""),
    backup_target_a_ssh_host: str = Form(""),
    backup_target_a_ssh_user: str = Form(""),
    backup_target_a_ssh_key_path: str = Form(""),
    backup_target_a_ssh_landing_dir: str = Form(""),
    backup_target_a_s3_endpoint: str = Form(""),
    backup_target_a_s3_access_key: str = Form(""),
    backup_target_a_s3_secret_key: str = Form(""),
    backup_target_a_s3_bucket: str = Form(""),
    backup_target_a_immutable_lock_days: str = Form("7"),
    backup_target_b_transport: str = Form(""),
    backup_target_b_label: str = Form(""),
    backup_target_b_ssh_host: str = Form(""),
    backup_target_b_ssh_user: str = Form(""),
    backup_target_b_ssh_key_path: str = Form(""),
    backup_target_b_ssh_landing_dir: str = Form(""),
    backup_target_b_s3_endpoint: str = Form(""),
    backup_target_b_s3_access_key: str = Form(""),
    backup_target_b_s3_secret_key: str = Form(""),
    backup_target_b_s3_bucket: str = Form(""),
    backup_target_b_immutable_lock_days: str = Form("7"),
):
    """Epic 9 (Story 9.2's backend, never wired to a Settings UI until now)
    — where each of the 2 fixed backup target slots actually sends backup
    data (`worker/backup/storage/factory.py::get_backend()` reads these
    same `settings.backup_target_<slot>_*` fields). Deliberately separate
    from "Kết nối cụm Ceph" — these credentials must never share network
    access with the SOURCE cluster's admin path (PRD FR-4, see
    config/settings.py's own comment on these fields).

    No live connection test here (same posture as "Build & Copy Patch Ceph"
    above) — `worker/backup/storage/ssh_backend.py`/`s3_backend.py` already
    fail loudly, not silently, the next time a real backup/restore actually
    runs against a bad destination; this form's job is only to get the
    values saved correctly.

    s3_secret_key follows the same "blank submit = keep the currently
    saved value" convention as router_api_key above — the rendered form
    never carries the real secret back into HTML (see
    `_backup_target_form_values()`), so a blank field here does NOT mean
    "clear the secret", it means "unchanged"."""
    _require_admin_privilege(user)

    raw = {
        "backup_target_a_transport": backup_target_a_transport.strip(),
        "backup_target_a_label": backup_target_a_label.strip(),
        "backup_target_a_ssh_host": backup_target_a_ssh_host.strip(),
        "backup_target_a_ssh_user": backup_target_a_ssh_user.strip(),
        "backup_target_a_ssh_key_path": backup_target_a_ssh_key_path.strip(),
        "backup_target_a_ssh_landing_dir": backup_target_a_ssh_landing_dir.strip(),
        "backup_target_a_s3_endpoint": backup_target_a_s3_endpoint.strip(),
        "backup_target_a_s3_access_key": backup_target_a_s3_access_key.strip(),
        "backup_target_a_s3_secret_key": backup_target_a_s3_secret_key.strip()
        or settings.backup_target_a_s3_secret_key,
        "backup_target_a_s3_bucket": backup_target_a_s3_bucket.strip(),
        "backup_target_b_transport": backup_target_b_transport.strip(),
        "backup_target_b_label": backup_target_b_label.strip(),
        "backup_target_b_ssh_host": backup_target_b_ssh_host.strip(),
        "backup_target_b_ssh_user": backup_target_b_ssh_user.strip(),
        "backup_target_b_ssh_key_path": backup_target_b_ssh_key_path.strip(),
        "backup_target_b_ssh_landing_dir": backup_target_b_ssh_landing_dir.strip(),
        "backup_target_b_s3_endpoint": backup_target_b_s3_endpoint.strip(),
        "backup_target_b_s3_access_key": backup_target_b_s3_access_key.strip(),
        "backup_target_b_s3_secret_key": backup_target_b_s3_secret_key.strip()
        or settings.backup_target_b_s3_secret_key,
        "backup_target_b_s3_bucket": backup_target_b_s3_bucket.strip(),
    }
    # Rendered back on a validation error — must NOT include the actual
    # secret values (same reasoning _backup_target_form_values() documents).
    display_values = {k: v for k, v in raw.items() if not k.endswith("s3_secret_key")}

    try:
        immutable_days_a = int(backup_target_a_immutable_lock_days or 7)
        immutable_days_b = int(backup_target_b_immutable_lock_days or 7)
        if immutable_days_a < 1 or immutable_days_b < 1:
            raise ValueError
    except ValueError:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                backup_target_error="Số ngày khoá immutable phải là số nguyên >= 1",
                backup_target_values=display_values,
            ),
        )
    raw["backup_target_a_immutable_lock_days"] = immutable_days_a
    raw["backup_target_b_immutable_lock_days"] = immutable_days_b

    for slot in ("a", "b"):
        error = _validate_backup_target_slot(slot, raw)
        if error:
            return templates.TemplateResponse(
                request,
                "settings.html",
                _settings_context(user, backup_target_error=error, backup_target_values=display_values),
            )

    try:
        env_fields: dict[str, str] = {}
        for slot in ("a", "b"):
            env_names = env_config.backup_target_env_names(slot)
            for field, env_name in env_names.items():
                env_fields[env_name] = str(raw[field])
        _update_env_file_batch(env_fields)
        for field in raw:
            setattr(settings, field, raw[field])
    except Exception:
        logger.exception("backup_targets_settings_submit: failed to persist config to .env")
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                backup_target_error="Không ghi được file cấu hình — kiểm tra quyền ghi trên server",
                backup_target_values=display_values,
            ),
        )

    await asyncio.to_thread(restart_worker)

    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(
            user,
            backup_target_success="Đã lưu cấu hình lưu trữ Backup — Worker đã khởi động lại để áp dụng ngay.",
        ),
    )


@router.post("/settings/restart-dashboard", response_class=HTMLResponse)
async def restart_dashboard_submit(request: Request, user: str = Depends(require_login)):
    """Lets an operator restart the Dashboard itself from the browser —
    previously this required someone with shell access to manually kill and
    relaunch the `uvicorn` process. Derives host/port from the incoming
    request (what the browser is actually connecting to) rather than adding
    yet another setting that would need to stay in sync with however this
    process was actually launched.
    """
    _require_admin_privilege(user)
    host = request.url.hostname or "127.0.0.1"
    port = request.url.port or 80
    try:
        await asyncio.to_thread(restart_dashboard_process, host, port)
    except Exception:
        logger.exception("restart_dashboard_submit: failed to launch restart watchdog")
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                dashboard_restart_error=(
                    "Không khởi động lại được — xem log server để biết chi tiết."
                ),
            ),
        )
    return templates.TemplateResponse(request, "restarting.html", {"user": user})


@router.post("/settings/restart-worker", response_class=HTMLResponse)
async def restart_worker_submit(request: Request, user: str = Depends(require_login)):
    """Manual counterpart to the automatic restart_worker() call in
    settings_save_router — for picking up a code change (e.g. a new
    action_id's Command builder) without needing to also touch the 9router
    config just to trigger a restart. restart_worker() itself never raises
    (see its own docstring) — this route only has to handle rendering
    whichever outcome it returns.
    """
    _require_admin_privilege(user)
    result = await asyncio.to_thread(restart_worker)
    if result["restarted"]:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user, manual_worker_restart_success=f"Đã khởi động lại Worker (PID {result['new_pid']})."
            ),
        )
    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(
            user,
            manual_worker_restart_error=(
                result["error"] or "Không khởi động lại được Worker — xem log server để biết chi tiết."
            ),
        ),
    )


@router.post("/settings/restart-watcher", response_class=HTMLResponse)
async def restart_watcher_submit(request: Request, user: str = Depends(require_login)):
    """Manual counterpart to the automatic restart_watcher() call in
    cluster_settings_submit — same reasoning as restart_worker_submit above."""
    _require_admin_privilege(user)
    result = await asyncio.to_thread(restart_watcher)
    if result["restarted"]:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user, manual_watcher_restart_success=f"Đã khởi động lại Watcher (PID {result['new_pid']})."
            ),
        )
    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(
            user,
            manual_watcher_restart_error=(
                result["error"] or "Không khởi động lại được Watcher — xem log server để biết chi tiết."
            ),
        ),
    )


@router.post("/settings/restart-remediation-watcher", response_class=HTMLResponse)
async def restart_remediation_watcher_submit(
    request: Request, user: str = Depends(require_login),
):
    """Restart the low-latency watcher dedicated to autonomous remediation."""
    _require_admin_privilege(user)
    result = await asyncio.to_thread(restart_remediation_watcher)
    if result["restarted"]:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                manual_remediation_watcher_restart_success=(
                    f"Đã khởi động lại AI Remediation Watcher (PID {result['new_pid']})."
                ),
            ),
        )
    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(
            user,
            manual_remediation_watcher_restart_error=(
                result["error"]
                or "Không khởi động lại được AI Remediation Watcher — xem log server."
            ),
        ),
    )


@router.post("/settings/log-intel", response_class=HTMLResponse)
async def log_intel_settings_submit(
    request: Request,
    user: str = Depends(require_login),
    log_intel_enabled: str = Form(""),
    log_intel_ai_enabled: str = Form(""),
    log_intel_source: str = Form("ssh"),
    log_intel_scan_interval_seconds: str = Form("900"),
    log_intel_window_minutes: str = Form("60"),
    log_intel_max_lines_per_daemon: str = Form("5000"),
    log_intel_loki_url: str = Form(""),
    log_intel_loki_tenant: str = Form(""),
):
    """Cấu hình Log Intelligence (Plan/log-intelligence-rca-plan.md).

    Hai công tắc TÁCH RIÊNG có chủ đích -- "Thu thập" và "Phân tích AI" --
    vì bật thu thập không đồng nghĩa với bật chi tiêu token. Runbook
    (docs/runbook-log-intelligence.md mục 2) khuyến nghị chạy thu thập 3-7
    ngày trước, xem cột "Gắn cờ" trên /log-intelligence, rồi mới bật AI:
    chi phí token tỉ lệ thuận đúng con số mà tầng triage thả qua.

    Chọn nguồn "loki" mà bỏ trống URL bị CHẶN NGAY tại đây thay vì để lưu
    rồi mới hỏng lúc quét: một cấu hình thiếu mà im lặng trông y hệt một
    cụm không phát sinh log nào -- đúng kiểu hỏng làm cả kho evidence âm
    thầm vô giá trị.
    """
    _require_admin_privilege(user)

    submitted = {
        "log_intel_enabled": bool(log_intel_enabled),
        "log_intel_ai_enabled": bool(log_intel_ai_enabled),
        "log_intel_source": log_intel_source.strip() or "ssh",
        "log_intel_scan_interval_seconds": log_intel_scan_interval_seconds.strip(),
        "log_intel_window_minutes": log_intel_window_minutes.strip(),
        "log_intel_max_lines_per_daemon": log_intel_max_lines_per_daemon.strip(),
        "log_intel_loki_url": log_intel_loki_url.strip().rstrip("/"),
        "log_intel_loki_tenant": log_intel_loki_tenant.strip(),
    }

    def _fail(message: str):
        return templates.TemplateResponse(
            request, "settings.html",
            _settings_context(user, log_intel_error=message, log_intel_values=submitted),
        )

    if submitted["log_intel_source"] not in ("ssh", "loki"):
        return _fail("Nguồn log chỉ nhận 'ssh' hoặc 'loki'.")
    if submitted["log_intel_source"] == "loki" and not submitted["log_intel_loki_url"]:
        return _fail("Chọn nguồn Loki thì bắt buộc phải điền Loki URL.")
    if submitted["log_intel_loki_url"] and not submitted["log_intel_loki_url"].startswith(
        ("http://", "https://")
    ):
        return _fail("Loki URL phải bắt đầu bằng http:// hoặc https://")

    numeric = {}
    for field, label, minimum in (
        ("log_intel_scan_interval_seconds", "Chu kỳ quét", 60),
        ("log_intel_window_minutes", "Cửa sổ thời gian", 1),
        ("log_intel_max_lines_per_daemon", "Số dòng tối đa mỗi daemon", 100),
    ):
        try:
            value = int(submitted[field])
        except ValueError:
            return _fail(f"{label} phải là số nguyên.")
        if value < minimum:
            return _fail(f"{label} phải >= {minimum}.")
        numeric[field] = value
    submitted.update(numeric)

    # Cửa sổ phải LỚN HƠN chu kỳ quét, nếu không một tick chậm sẽ để lại lỗ
    # hổng dữ liệu vĩnh viễn (xem log_intel_window_minutes trong settings.py).
    if numeric["log_intel_window_minutes"] * 60 <= numeric["log_intel_scan_interval_seconds"]:
        return _fail(
            "Cửa sổ thời gian phải LỚN HƠN chu kỳ quét — nếu không, một lần quét chậm "
            "sẽ để lại lỗ hổng dữ liệu không bao giờ lấy lại được."
        )

    try:
        _update_env_file_batch({
            env_name: str(submitted[field])
            for field, env_name in LOG_INTEL_ENV_NAMES.items()
        })
        for field in LOG_INTEL_ENV_NAMES:
            setattr(settings, field, submitted[field])
    except Exception:
        logger.exception("log_intel_settings_submit: failed to persist config to .env")
        return _fail("Không ghi được file cấu hình — kiểm tra quyền ghi trên server")

    # Watcher là tiến trình chạy vòng quét này, nên nó (không phải Worker)
    # mới là cái cần khởi động lại để áp dụng ngay.
    await asyncio.to_thread(restart_watcher)

    note = ""
    if submitted["log_intel_enabled"] and submitted["log_intel_ai_enabled"]:
        note = (
            " Lưu ý: bạn đã bật CẢ phân tích AI — nên xem cột \"Gắn cờ\" trên trang "
            "Log Intelligence vài ngày trước khi tin vào chi phí token."
        )
    return templates.TemplateResponse(
        request, "settings.html",
        _settings_context(
            user,
            log_intel_success=f"Đã lưu cấu hình — Watcher đã khởi động lại để áp dụng ngay.{note}",
        ),
    )


@router.post("/settings/log-intel/test-loki")
async def log_intel_test_loki(
    user: str = Depends(require_login),
    log_intel_loki_url: str = Form(""),
    log_intel_loki_tenant: str = Form(""),
):
    """Thử gọi `/ready` của Loki trước khi lưu — cùng posture "test kết nối"
    mà form Database/Cụm Ceph đã có. Không lưu gì, chỉ trả kết quả."""
    _require_admin_privilege(user)

    url = log_intel_loki_url.strip().rstrip("/")
    if not url:
        return {"ok": False, "message": "Chưa điền Loki URL."}

    def _probe() -> tuple[bool, str]:
        import httpx

        headers = {}
        if log_intel_loki_tenant.strip():
            headers["X-Scope-OrgID"] = log_intel_loki_tenant.strip()
        try:
            response = httpx.get(f"{url}/ready", headers=headers, timeout=10)
        except Exception as exc:
            return False, f"Không kết nối được: {type(exc).__name__}: {exc}"
        if response.status_code == 200:
            return True, "Loki phản hồi OK (/ready)."
        return False, f"Loki trả HTTP {response.status_code} tại {url}/ready"

    ok, message = await asyncio.to_thread(_probe)
    return {"ok": ok, "message": message}
