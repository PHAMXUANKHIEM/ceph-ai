import asyncio
import logging
import os
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

from config.settings import settings
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared.router_client import list_router_models, readable_exception_message
from watcher.ceph_client import (
    VALID_EXEC_MODES,
    CephQueryError,
    query_cluster_health_with,
    read_public_key,
    ssh_key_path_error,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

MASK_VISIBLE_CHARS = 4

ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
ROUTER_API_KEY_ENV_NAME = "ROUTER_API_KEY"
ROUTER_MODEL_ENV_NAME = "ROUTER_MODEL"
ROUTER_BASE_URL_ENV_NAME = "ROUTER_BASE_URL"
ROUTER_ENABLED_ENV_NAME = "ROUTER_ENABLED"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WORKER_MODULE = "worker.main"
WORKER_PGREP_PATTERN = r"-m\s+worker\.main"
WORKER_LOG_PATH = Path("/var/log/ceph-aiops-worker.log")
WORKER_STOP_TIMEOUT_SECONDS = 5.0
WORKER_START_CHECK_DELAY_SECONDS = 1.5
PGREP_TIMEOUT_SECONDS = 5.0

# Story 5.1: same process-management pattern as Worker above, applied to
# Watcher so a cluster-connection config change takes effect immediately.
WATCHER_MODULE = "watcher.main"
WATCHER_PGREP_PATTERN = r"-m\s+watcher\.main"
WATCHER_LOG_PATH = Path("/var/log/ceph-aiops-watcher.log")

# Restarting the Dashboard itself — unlike Worker/Watcher, this is the very
# process handling the HTTP request that triggers it, so it can't just spawn
# a replacement and kill the old one the same way _start_process() does (see
# restart_dashboard_process below for why).
DASHBOARD_LOG_PATH = Path("/var/log/ceph-aiops-dashboard.log")
DASHBOARD_RESTART_GRACE_SECONDS = 1.0
DASHBOARD_RESTART_WAIT_SECONDS = 10.0

# 2026-07-23: this app has exactly ONE account (no RBAC anywhere else in this
# codebase either — see shared/models.py's docstrings), so `require_login`
# alone can't express "only a privileged user" — every logged-in user IS the
# one account. Manually restarting Worker/Watcher/Dashboard from the browser
# is disruptive to whoever else is using the Dashboard at that moment, so
# it's gated behind a SEPARATE, hardcoded check against the literal username
# "admin" — deliberately NOT `settings.dashboard_username` (comparing a
# single-account system's current user against its own configured username
# is always true, i.e. no real restriction at all). This means renaming the
# account away from "admin" hides these controls for everyone, which is the
# intended fail-safe: nobody sees "restart" buttons by accident.
RESTART_CONTROLS_USERNAME = "admin"


def _require_restart_privilege(user: str) -> None:
    if user != RESTART_CONTROLS_USERNAME:
        raise HTTPException(
            status_code=403,
            detail="Chỉ tài khoản admin mới được phép khởi động lại tiến trình hệ thống",
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


def _apply_env_updates(existing_lines: list[str], fields: dict[str, str]) -> list[str]:
    for name, value in fields.items():
        # Review Story 5.1: an unchecked embedded newline in a submitted
        # field would inject an arbitrary extra line into .env (e.g.
        # overwriting DASHBOARD_PASSWORD_HASH/SESSION_SECRET_KEY) — reject
        # at the single insertion point so every caller is protected.
        if "\n" in value or "\r" in value:
            raise ValueError(f"{name}: giá trị không được chứa ký tự xuống dòng")

    remaining = dict(fields)
    new_lines = []
    for line in existing_lines:
        matched_name = next((name for name in remaining if line.startswith(f"{name}=")), None)
        if matched_name is not None:
            new_lines.append(f"{matched_name}={remaining.pop(matched_name)}")
        else:
            new_lines.append(line)
    for name, value in remaining.items():
        new_lines.append(f"{name}={value}")
    return new_lines


def _write_env_lines(new_lines: list[str]) -> None:
    """Writes to a temp file then os.replace()s over the original — never a
    window where .env is truncated/partially written if the process dies
    mid-write (.env also holds DASHBOARD_PASSWORD_HASH/SESSION_SECRET_KEY).
    Restricts the file to owner-only read/write afterward — this file holds
    multiple secrets."""
    tmp_path = ENV_PATH.with_suffix(".tmp")
    tmp_path.write_text("\n".join(new_lines) + "\n")
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp_path, ENV_PATH)


def _update_env_file(env_var_name: str, value: str) -> None:
    """Replace the line `env_var_name=...` in .env if present, else append it."""
    existing_lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    _write_env_lines(_apply_env_updates(existing_lines, {env_var_name: value}))


def _update_env_file_batch(fields: dict[str, str]) -> None:
    """Same atomicity guarantee as `_update_env_file`, but for several fields
    written together in ONE read-modify-write-replace cycle (Review Story
    5.1). The cluster-connection route originally called `_update_env_file`
    once per field in a loop — a failure partway through left `.env` with a
    mix of old and new field values and no rollback, while the in-memory
    `settings` object (updated only after all writes succeeded) stayed
    fully old. Writing the whole batch as a single temp-file-then-replace
    makes it all-or-nothing."""
    existing_lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    _write_env_lines(_apply_env_updates(existing_lines, fields))


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
    return [int(pid) for pid in result.stdout.split()]


def _find_worker_pids() -> list[int]:
    return _find_pids(WORKER_PGREP_PATTERN)


def _find_watcher_pids() -> list[int]:
    return _find_pids(WATCHER_PGREP_PATTERN)


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


def _start_worker() -> int:
    # Explicit env= override: the freshly-saved key/model must always win
    # over whatever the Dashboard process's own os.environ happens to hold
    # (e.g. a stale ROUTER_API_KEY exported once by hand) — pydantic-settings
    # would otherwise prefer a real env var over .env file contents, and the
    # new Worker would silently keep using an old key/model despite .env
    # being updated.
    child_env = {
        **os.environ,
        ROUTER_API_KEY_ENV_NAME: settings.router_api_key,
        ROUTER_MODEL_ENV_NAME: settings.router_model,
        ROUTER_BASE_URL_ENV_NAME: settings.router_base_url,
        ROUTER_ENABLED_ENV_NAME: "true" if settings.router_enabled else "false",
    }
    return _start_process(WORKER_MODULE, WORKER_LOG_PATH, child_env)


CLUSTER_ENV_NAMES = {
    "ceph_mon_nodes": "CEPH_MON_NODES",
    "ceph_mon_hostnames": "CEPH_MON_HOSTNAMES",
    "ceph_container_name": "CEPH_CONTAINER_NAME",
    "ceph_osd_nodes": "CEPH_OSD_NODES",
    "ceph_osd_container_name": "CEPH_OSD_CONTAINER_NAME",
    "ceph_mgr_nodes": "CEPH_MGR_NODES",
    "ceph_rgw_nodes": "CEPH_RGW_NODES",
    "ceph_rgw_container_name": "CEPH_RGW_CONTAINER_NAME",
    "ceph_exec_mode": "CEPH_EXEC_MODE",
    "ssh_user": "SSH_USER",
    # ssh_key_path is deliberately NOT here — it's no longer an editable
    # field on the cluster form (one key, set once via .env/server config,
    # not something that changes per Ceph cluster the way MON/OSD nodes do).
    # cluster_settings_submit reads settings.ssh_key_path directly instead.
}


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
    child_env = {
        **os.environ,
        **{env_name: getattr(settings, field) for field, env_name in CLUSTER_ENV_NAMES.items()},
    }
    return _start_process(WATCHER_MODULE, WATCHER_LOG_PATH, child_env)


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
        "ssh_user": settings.ssh_user,
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
    dashboard_restart_error: str | None = None,
    cluster_values: dict | None = None,
    router_model_value: str | None = None,
    router_base_url_value: str | None = None,
    router_models: list[str] | None = None,
    cleanup_error: str | None = None,
    cleanup_success: str | None = None,
    manual_worker_restart_success: str | None = None,
    manual_worker_restart_error: str | None = None,
    manual_watcher_restart_success: str | None = None,
    manual_watcher_restart_error: str | None = None,
    database_error: str | None = None,
    database_success: str | None = None,
    database_values: dict | None = None,
) -> dict:
    """Every form on the Settings page (9router connection, cluster
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
        "is_admin": user == RESTART_CONTROLS_USERNAME,
        "masked_key": masked_key,
        "router_model": (
            router_model_value if router_model_value is not None else settings.router_model
        ),
        "router_base_url": (
            router_base_url_value if router_base_url_value is not None else settings.router_base_url
        ),
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
        "error": error,
        "success": success,
        "worker_restart_error": worker_restart_error,
        "cluster_error": cluster_error,
        "cluster_success": cluster_success,
        "watcher_restart_error": watcher_restart_error,
        "dashboard_restart_error": dashboard_restart_error,
        "cleanup_error": cleanup_error,
        "cleanup_success": cleanup_success,
        "manual_worker_restart_success": manual_worker_restart_success,
        "manual_worker_restart_error": manual_worker_restart_error,
        "manual_watcher_restart_success": manual_watcher_restart_success,
        "manual_watcher_restart_error": manual_watcher_restart_error,
        "database_error": database_error,
        "database_success": database_success,
        "current_database_display": _current_database_display(),
    }
    context.update(database_values if database_values is not None else _database_form_values())
    context.update(cluster_values if cluster_values is not None else _cluster_form_values())
    # ssh_key_path is no longer an editable field on the cluster form (see
    # CLUSTER_ENV_NAMES) — always show the actual configured value here,
    # never a `cluster_values`/submitted one, since submitted dicts no
    # longer carry this key at all.
    context["ssh_key_path"] = settings.ssh_key_path
    # Shown as a read-only reference so the operator can copy it straight
    # into a NEW cluster's `~/.ssh/authorized_keys`.
    context["ssh_public_key"] = read_public_key(settings.ssh_key_path)
    return context


def _parse_node_list(raw: str) -> list[str]:
    return [h.strip() for h in raw.split(",") if h.strip()]


@router.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(request, "settings.html", _settings_context(user))


@router.post("/settings/9router/verify")
async def settings_verify_router(
    user: str = Depends(require_login),
    router_api_key: str = Form(""),
    router_base_url: str = Form(""),
):
    """Backs the Settings page's Step 1 "[Xác nhận kết nối]" button — a
    single GET /v1/models round trip (verify_router_connection above) both
    proves the just-typed (not-yet-saved) key/base_url work AND returns the
    real model catalog for Step 2, per spec. JSON in/out since this is only
    ever called from JS, never a full page load.

    A blank router_api_key/router_base_url falls back to whatever is already
    saved — the API key is a password field the browser never pre-fills with
    the real (masked) value, so without this fallback the "[Đổi model]" flow
    (re-verify against an already-connected 9router to refresh the model
    list) could never work without forcing the operator to retype a key
    they're not actually changing."""
    submitted_key = router_api_key.strip() or settings.router_api_key
    submitted_base_url = router_base_url.strip() or settings.router_base_url
    if not submitted_key:
        raise HTTPException(status_code=400, detail="Cần nhập API key trước khi kiểm tra kết nối")
    if not submitted_base_url:
        raise HTTPException(status_code=400, detail="Cần nhập Base URL (9router) trước khi kiểm tra kết nối")

    is_valid, message, models = await verify_router_connection(submitted_key, submitted_base_url)
    return {"valid": is_valid, "message": message, "models": models}


@router.post("/settings/9router/save", response_class=HTMLResponse)
async def settings_save_router(
    request: Request,
    user: str = Depends(require_login),
    router_api_key: str = Form(""),
    router_base_url: str = Form(""),
    router_model_id: str = Form(""),
):
    # Same blank-falls-back-to-saved-value semantics as the verify route
    # above — Step 2's "[Lưu cấu hình]" submit and the "[Đổi model]" re-save
    # both go through here, and the latter never has a retyped key/base_url.
    submitted_key = router_api_key.strip() or settings.router_api_key
    submitted_base_url = router_base_url.strip() or settings.router_base_url
    submitted_model = router_model_id.strip()

    if not submitted_key:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(user, error="API key không được để trống"),
        )
    if not submitted_base_url:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                error="Base URL (9router) không được để trống",
                router_base_url_value=submitted_base_url,
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
                error=f"Không thể lưu — kết nối 9router không còn hợp lệ"
                + (f": {reason}" if reason else ""),
                router_base_url_value=submitted_base_url,
            ),
        )
    if models is not None and submitted_model not in models:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                error=f"Model '{submitted_model}' không khả dụng trên 9router",
                router_base_url_value=submitted_base_url,
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
            }
        )
        settings.router_api_key = submitted_key
        settings.router_model = submitted_model
        settings.router_base_url = submitted_base_url
        settings.router_enabled = True
    except Exception:
        logger.exception("settings_save_router: failed to persist 9router config to .env")
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user,
                error="Không ghi được file cấu hình — kiểm tra quyền ghi trên server",
                router_base_url_value=submitted_base_url,
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
    9router config entirely (api_key/base_url/model, router_enabled=False)
    so the Settings page falls back to showing Step 1 again, and the
    chatbox falls back to MISSING_AI_CONFIG_MESSAGE until reconnected."""
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
        logger.exception("settings_disconnect_router: failed to clear 9router config in .env")
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
        _settings_context(user, success="Đã huỷ kết nối 9router."),
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
    ):
        message = (
            "Vui lòng điền đủ MON nodes, MON container, SSH user."
            if container_required
            else "Vui lòng điền đủ MON nodes, SSH user."
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
    except Exception:
        logger.exception("cluster_settings_submit: failed to persist cluster config to .env")
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

    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(
            user, cluster_success=cluster_success, watcher_restart_error=watcher_restart_error
        ),
    )


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
    other control in this section (see RESTART_CONTROLS_USERNAME) — saving a
    database connection always ends in a Dashboard self-restart (see
    settings_save_database below), same privilege boundary as the manual
    restart buttons."""
    _require_restart_privilege(user)
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
    _require_restart_privilege(user)

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


@router.post("/settings/restart-dashboard", response_class=HTMLResponse)
async def restart_dashboard_submit(request: Request, user: str = Depends(require_login)):
    """Lets an operator restart the Dashboard itself from the browser —
    previously this required someone with shell access to manually kill and
    relaunch the `uvicorn` process. Derives host/port from the incoming
    request (what the browser is actually connecting to) rather than adding
    yet another setting that would need to stay in sync with however this
    process was actually launched.
    """
    _require_restart_privilege(user)
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
    _require_restart_privilege(user)
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
    _require_restart_privilege(user)
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
