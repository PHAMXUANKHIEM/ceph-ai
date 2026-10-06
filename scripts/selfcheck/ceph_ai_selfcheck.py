#!/usr/bin/python3.11
"""Host-side self-check for the Ceph AI stack (plan SM3).

Runs every minute from ceph-ai-selfcheck.timer, outside every container, and
reports problems straight to the Telegram Bot API. It must not depend on what
it watches: no telegram-ai, no outbox, no RabbitMQ, no application imports,
standard library only.

Checks: expected containers running and not unhealthy, service heartbeats in
/run/ceph-ai, RabbitMQ answering, database TCP reachable, Dashboard answering,
disk space. A problem is reported after two failed runs in a row, reminded
every six hours while it lasts, and a recovery is reported once. After a host
reboot the stack gets a grace period, then one boot summary is sent.

Self-heal (plan SM2): podman-compose 1.0.6 cannot pass --health-on-failure,
so a container that stays unhealthy for three runs is restarted here, at
most three times in six hours; after that it is only reported. Full Executor
is never restarted automatically: it may be in the middle of a remediation.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
# Only ever runs /usr/bin/podman with constant arguments (see _podman).
import subprocess  # nosec B404
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

ENV_FILE = Path(os.environ.get("CEPH_AI_SELFCHECK_ENV", "/var/lib/ceph-ai/config/.env"))
STATE_FILE = Path(os.environ.get("CEPH_AI_SELFCHECK_STATE", "/var/lib/ceph-ai/selfcheck/state.json"))
RUNTIME_DIR = Path(os.environ.get("CEPH_AI_SELFCHECK_RUNTIME", "/run/ceph-ai"))
PODMAN = "/usr/bin/podman"
DASHBOARD = ("127.0.0.1", 8000, "/login")

EXPECTED_CONTAINERS = (
    "rabbitmq",
    "ceph-ai_dashboard-web_1",
    "ceph-ai_telegram-ai_1",
    "ceph-ai_full-executor_1",
    "ceph-ai_watcher_1",
    "ceph-ai_remediation-watcher_1",
    "ceph-ai_worker_1",
    "ceph-ai_vault-monitor_1",
)
HEARTBEATS = ("watcher", "worker", "remediation-watcher", "telegram-ai")
HEARTBEAT_STALE_SECONDS = 180
DISK_PATHS = ("/", "/var/lib/containers", "/var/lib/ceph-ai")
DISK_LIMIT_PERCENT = 90
FAILURES_BEFORE_ALERT = 2
REMIND_SECONDS = 6 * 3600
BOOT_GRACE_SECONDS = 600
COMMAND_TIMEOUT_SECONDS = 20
UNHEALTHY_RUNS_BEFORE_RESTART = 3
MAX_RESTARTS = 3
RESTART_WINDOW_SECONDS = 6 * 3600
NEVER_AUTO_RESTART = frozenset({"rabbitmq", "ceph-ai_full-executor_1"})


# --- inputs -----------------------------------------------------------------

def read_env(path: Path = ENV_FILE) -> dict[str, str]:
    """KEY=VALUE lines of the stack's .env (no python-dotenv on the host)."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _podman(*args: str) -> subprocess.CompletedProcess:
    # Fixed binary, no shell, arguments are constants from this module.
    return subprocess.run(  # nosec B603
        [PODMAN, *args], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False,
    )


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def uptime_seconds() -> float:
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


# --- checks: each returns None when healthy or a short problem text ---------

def check_containers() -> dict[str, str | None]:
    try:
        listed = json.loads(_podman("ps", "-a", "--format", "json").stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"containers": f"không đọc được podman: {exc}"}
    by_name = {}
    for row in listed:
        names = row.get("Names") or []
        for name in names if isinstance(names, list) else [names]:
            by_name[name] = row
    results: dict[str, str | None] = {}
    for name in EXPECTED_CONTAINERS:
        row = by_name.get(name)
        if row is None:
            results[f"container:{name}"] = "không tồn tại"
        elif row.get("State") != "running":
            results[f"container:{name}"] = f"không chạy ({row.get('State')})"
        elif "unhealthy" in str(row.get("Status", "")):
            results[f"container:{name}"] = "unhealthy"
        else:
            results[f"container:{name}"] = None
    return results


def check_heartbeats(now: float) -> dict[str, str | None]:
    results: dict[str, str | None] = {}
    for service in HEARTBEATS:
        path = RUNTIME_DIR / f"{service}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            updated = datetime.fromisoformat(str(payload["updated_at"])).timestamp()
        except (OSError, ValueError, KeyError, TypeError):
            results[f"heartbeat:{service}"] = "không có heartbeat"
            continue
        age = now - updated
        results[f"heartbeat:{service}"] = (
            f"heartbeat cũ {int(age)} giây" if age > HEARTBEAT_STALE_SECONDS else None
        )
    return results


def check_rabbitmq() -> dict[str, str | None]:
    try:
        result = _podman("exec", "rabbitmq", "rabbitmq-diagnostics", "-q", "ping")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"rabbitmq": f"không trả lời ({type(exc).__name__})"}
    return {"rabbitmq": None if result.returncode == 0 else "không trả lời ping"}


def check_database(env: dict[str, str]) -> dict[str, str | None]:
    """TCP reachability only: the self-check never holds DB credentials in use."""
    url = urlsplit(env.get("DATABASE_URL", ""))
    if not url.hostname or url.scheme.startswith("sqlite"):
        return {"database": None}
    try:
        with socket.create_connection((url.hostname, url.port or 5432), timeout=5):
            return {"database": None}
    except OSError as exc:
        return {"database": f"không kết nối được {url.hostname}:{url.port or 5432} ({type(exc).__name__})"}


def check_dashboard() -> dict[str, str | None]:
    host, port, path = DASHBOARD
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request("GET", path)
        status = connection.getresponse().status
    except OSError as exc:
        return {"dashboard": f"không trả lời ({type(exc).__name__})"}
    finally:
        connection.close()
    return {"dashboard": None if status < 500 else f"HTTP {status}"}


def check_disks() -> dict[str, str | None]:
    results: dict[str, str | None] = {}
    for path in DISK_PATHS:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        percent = 100 * usage.used / usage.total if usage.total else 0
        results[f"disk:{path}"] = f"đầy {percent:.0f}%" if percent >= DISK_LIMIT_PERCENT else None
    return results


def run_checks(env: dict[str, str], now: float) -> dict[str, str | None]:
    results: dict[str, str | None] = {}
    for part in (check_containers(), check_heartbeats(now), check_rabbitmq(),
                 check_database(env), check_dashboard(), check_disks()):
        results.update(part)
    return results


# --- state and messages ------------------------------------------------------

def load_state(path: Path = STATE_FILE) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def _since(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60} giờ {minutes % 60} phút" if minutes >= 60 else f"{minutes} phút"


def evaluate(results: dict[str, str | None], state: dict, now: float, *, in_grace: bool) -> list[str]:
    """Update per-check state; return the message lines to send this run."""
    checks = state.setdefault("checks", {})
    lines: list[str] = []
    for name, problem in sorted(results.items()):
        entry = checks.setdefault(name, {"failures": 0, "alerted_at": None, "since": None})
        if problem is None:
            if entry.get("alerted_at") is not None:
                lines.append(f"✅ {name}: đã hồi phục sau {_since(now - (entry.get('since') or now))}")
            checks[name] = {"failures": 0, "alerted_at": None, "since": None}
            continue
        entry["failures"] = int(entry.get("failures") or 0) + 1
        entry["since"] = entry.get("since") or now
        entry["problem"] = problem
        if in_grace or entry["failures"] < FAILURES_BEFORE_ALERT:
            continue
        alerted = entry.get("alerted_at")
        if alerted is None:
            lines.append(f"🔴 {name}: {problem}")
            entry["alerted_at"] = now
        elif now - alerted >= REMIND_SECONDS:
            lines.append(f"🔴 {name}: vẫn lỗi sau {_since(now - entry['since'])} — {problem}")
            entry["alerted_at"] = now
    return lines


def heal(results: dict[str, str | None], state: dict, now: float, *, in_grace: bool) -> list[str]:
    """Restart containers that stay unhealthy, within the restart budget."""
    if in_grace:
        return []
    lines: list[str] = []
    restarts = state.setdefault("restarts", {})
    for name, problem in sorted(results.items()):
        container = name.removeprefix("container:")
        entry = state.get("checks", {}).get(name) or {}
        if (not name.startswith("container:") or problem != "unhealthy" or container in NEVER_AUTO_RESTART
                or int(entry.get("failures") or 0) < UNHEALTHY_RUNS_BEFORE_RESTART):
            continue
        recent = [at for at in restarts.get(container, []) if now - at < RESTART_WINDOW_SECONDS]
        if len(recent) >= MAX_RESTARTS:
            if entry.get("budget_spent") != recent[-1]:
                lines.append(f"⛔ {container}: đã tự restart {MAX_RESTARTS} lần trong 6 giờ, không tự restart nữa")
                entry["budget_spent"] = recent[-1]
            continue
        try:
            done = _podman("restart", "-t", "30", container).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            done = False
        recent.append(now)
        restarts[container] = recent
        entry["failures"] = 0  # give it time to become healthy again
        lines.append(f"🔧 {container}: unhealthy {UNHEALTHY_RUNS_BEFORE_RESTART} phút → "
                     + (f"đã restart (lần {len(recent)}/{MAX_RESTARTS} trong 6 giờ)" if done else "restart thất bại"))
    return lines


def boot_lines(results: dict[str, str | None], state: dict, current_boot: str, *, in_grace: bool) -> list[str]:
    """One summary per boot, sent once the grace period is over."""
    if not current_boot or state.get("boot_reported") == current_boot or in_grace:
        return []
    first_run = "boot_reported" not in state
    state["boot_reported"] = current_boot
    if first_run:
        return []  # installing the timer is not a reboot
    booted = datetime.fromtimestamp(time.time() - uptime_seconds(), tz=timezone.utc).astimezone()
    failing = [f"{name}: {problem}" for name, problem in sorted(results.items()) if problem]
    summary = "mọi kiểm tra đều đạt" if not failing else "còn lỗi:\n" + "\n".join(f"• {item}" for item in failing)
    return [f"🔄 Máy Ceph AI đã khởi động lại lúc {booted:%d/%m %H:%M}; {summary}"]


def send_telegram(env: dict[str, str], text: str) -> bool:
    token = env.get("TELEGRAM_NODE_BOT_TOKEN") or env.get("TELEGRAM_INCIDENT_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_NODE_CHAT_ID") or env.get("TELEGRAM_INCIDENT_CHAT_ID")
    if not token or not chat_id:
        print("selfcheck: no Telegram bot configured; message not sent", file=sys.stderr)
        return False
    body = urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"})
    connection = http.client.HTTPSConnection("api.telegram.org", timeout=15)
    try:
        connection.request("POST", f"/bot{token}/sendMessage", body=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        status = connection.getresponse().status
    except OSError as exc:
        print(f"selfcheck: Telegram send failed: {type(exc).__name__}", file=sys.stderr)
        return False
    finally:
        connection.close()
    if status != 200:
        print(f"selfcheck: Telegram returned HTTP {status}", file=sys.stderr)
    return status == 200


def main() -> int:
    env = read_env()
    now = time.time()
    state = load_state()
    in_grace = uptime_seconds() < BOOT_GRACE_SECONDS
    results = run_checks(env, now)
    lines = boot_lines(results, state, boot_id(), in_grace=in_grace) + evaluate(results, state, now, in_grace=in_grace)
    lines += heal(results, state, now, in_grace=in_grace)
    failing = sorted(name for name, problem in results.items() if problem)
    print(f"selfcheck: {len(results)} checks, failing: {', '.join(failing) or 'none'}")
    if lines and not send_telegram(env, "Ceph AI tự kiểm tra\n" + "\n".join(lines)):
        # Keep the alert pending so the next run retries it.
        for name in list(state.get("checks", {})):
            entry = state["checks"][name]
            if entry.get("alerted_at") == now:
                entry["alerted_at"] = None
    state["last_run"] = now
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
