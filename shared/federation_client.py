"""Customer side of the Ceph AI federation (plan readiness-gap-review R8, 14/10/2026).

A self-hosted install that may reach out joins our federation database
(``federation/schema.sql``) with an activation code we issue:

1. ``enroll(code)`` logs in as the shipped, low-privilege ``fed_enroll`` user and
   calls ``enroll_api.enroll``. The answer is this install's own login and its own
   INSERT-only table ``inbox.c_<id>``; they are kept in a 0600 JSON file and never
   sent back to the browser.
2. ``build_payload()`` is everything that would be sent: counts over the last 24 h
   (incidents by Ceph code and severity, actions by type and outcome, number of
   clusters). No names, addresses, fsids, logs or commands. The dashboard shows it
   before the operator presses "Gửi".
3. ``push()`` inserts that payload as this install's login.

Connections require TLS with SCRAM channel binding, so a man in the middle cannot
relay this install's own password.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from config.settings import settings
from shared import db
from shared.models import Action, Cluster, Incident
from shared.time import utc_now

PAYLOAD_VERSION = 1
WINDOW_HOURS = 24
ENROLL_USER = "fed_enroll"
_CODE_RE = re.compile(r"^CAI(-[A-Z0-9]{4}){5}$")
_INBOX_RE = re.compile(r"^inbox\.(c_[0-9a-f]{12})$")
_LOGIN_RE = re.compile(r"^cust_[0-9a-f]{12}$")
_CONNECT_TIMEOUT_SECONDS = 10
# What a payload key may look like once reduced (codes, action ids, statuses, severities).
_BASE_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_IPV4_RE = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")


class FederationError(ValueError):
    """A refusal or failure with a message safe to show the operator."""


# --- local state ----------------------------------------------------------------------

def _state_path() -> Path:
    return Path(settings.federation_state_file)


def load_state() -> dict[str, Any]:
    try:
        value = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def _instance_id(state: dict[str, Any]) -> str:
    """A random id for this install, so we can tell installs apart without learning who they are."""
    if not state.get("instance_id"):
        state["instance_id"] = uuid.uuid4().hex
    return str(state["instance_id"])


def enrolled(state: dict[str, Any]) -> bool:
    return bool(_LOGIN_RE.match(str(state.get("login") or "")) and state.get("password")
                and _INBOX_RE.match(str(state.get("inbox_table") or "")))


def public_status() -> dict[str, Any]:
    """What the page may show: never the password."""
    state = load_state()
    return {
        "server": f"{settings.federation_host}:{settings.federation_port}",
        "enrolled": enrolled(state),
        "customer_id": state.get("customer_id"),
        "tier": state.get("tier"),
        "enrolled_at": state.get("enrolled_at"),
        "last_push_at": state.get("last_push_at"),
        "last_push_error": state.get("last_push_error"),
        "enroll_login_ready": _enroll_password() is not None,
    }


def forget() -> None:
    """Drop this install's login (the customer row on our side stays until we disable it)."""
    state = load_state()
    _save_state({key: state[key] for key in ("instance_id",) if key in state})


# --- server connections ---------------------------------------------------------------

def _enroll_password() -> str | None:
    try:
        value = Path(settings.federation_enroll_password_file).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _connect(user: str, password: str) -> psycopg.Connection:
    return psycopg.connect(
        host=settings.federation_host, port=settings.federation_port, dbname=settings.federation_database,
        user=user, password=password, sslmode="require", channel_binding="require",
        connect_timeout=_CONNECT_TIMEOUT_SECONDS, application_name="ceph-ai-federation",
    )


def _server_error(exc: psycopg.Error) -> FederationError:
    if isinstance(exc, psycopg.errors.InvalidAuthorizationSpecification):  # SQLSTATE 28000 from enroll()
        return FederationError("Mã kích hoạt không hợp lệ, đã dùng, đã thu hồi hoặc đã hết hạn.")
    if isinstance(exc, psycopg.OperationalError):
        return FederationError(f"Không kết nối được máy chủ federation {settings.federation_host}:"
                               f"{settings.federation_port} (mạng, tường lửa hoặc tài khoản đã bị khoá).")
    return FederationError(f"Máy chủ federation từ chối: {type(exc).__name__}")


def normalize_code(code: str) -> str:
    value = re.sub(r"\s+", "", str(code or "")).upper()
    if not _CODE_RE.match(value):
        raise FederationError("Mã kích hoạt có dạng CAI-XXXX-XXXX-XXXX-XXXX-XXXX.")
    return value


def enroll(code: str) -> dict[str, Any]:
    value = normalize_code(code)
    state = load_state()
    if enrolled(state):
        raise FederationError("Bản cài này đã đăng ký; bấm \"Huỷ đăng ký trên máy này\" trước khi dùng mã khác.")
    password = _enroll_password()
    if password is None:
        raise FederationError(
            f"Thiếu file mật khẩu đăng ký ({settings.federation_enroll_password_file} trong container). "
            "Trên máy cài bằng bộ cài khách, chạy trên host: "
            "scripts/customer/ceph-ai-customer.sh enroll-password < <file mật khẩu nhà cung cấp gửi>.")
    try:
        with _connect(ENROLL_USER, password) as connection:
            row = connection.execute("SELECT customer_id, login, password, inbox_table, tier "
                                     "FROM enroll_api.enroll(%s, %s)", (value, _instance_id(state))).fetchone()
    except psycopg.Error as exc:
        raise _server_error(exc) from exc
    if row is None:
        raise FederationError("Máy chủ federation không trả về tài khoản.")
    customer_id, login, customer_password, inbox_table, tier = (str(item) for item in row)
    if not (_LOGIN_RE.match(login) and _INBOX_RE.match(inbox_table)):
        raise FederationError("Máy chủ federation trả về tài khoản không đúng định dạng.")
    state.update({"customer_id": customer_id, "login": login, "password": customer_password,
                  "inbox_table": inbox_table, "tier": tier, "enrolled_at": utc_now().isoformat(timespec="seconds") + "Z",
                  "last_push_at": None, "last_push_error": None})
    _save_state(state)
    return public_status()


# --- payload --------------------------------------------------------------------------

def base_code(value: str | None) -> str:
    """The generic part of a code. Incident codes may carry a node, an OSD id or a hash after
    ':' ("NODE_UNREACHABLE:10.2.0.83" left 10.2.0.94 on 16/10/2026); only the part before it,
    and only when it looks like a code, may leave the install."""
    head = str(value or "").split(":", 1)[0].strip()
    return head if _BASE_CODE_RE.match(head) else "OTHER"


def assert_anonymous(payload: dict[str, Any]) -> None:
    """Last check before sending: no IPv4 address anywhere in the payload."""
    text = json.dumps(payload, ensure_ascii=False)
    if _IPV4_RE.search(text):
        raise FederationError("Dữ liệu gửi đi có địa chỉ IP; đã chặn, không gửi.")

def build_payload(session=None) -> dict[str, Any]:
    """Counts only: the operator sees exactly this before anything is sent."""
    since = utc_now() - timedelta(hours=WINDOW_HOURS)
    own = session is None
    session = session or db.SessionLocal()
    try:
        clusters = session.query(Cluster).count()
        incidents = session.query(Incident.ceph_code, Incident.severity).filter(Incident.created_at >= since).all()
        actions = session.query(Action.action_id, Action.classification, Action.status).filter(
            Action.created_at >= since).all()
    finally:
        if own:
            session.close()
    by_action: dict[str, Counter] = defaultdict(Counter)
    for action_id, classification, status in actions:
        by_action[f"{base_code(action_id)} ({base_code(classification)})"][base_code(status)] += 1
    return {
        "payload_version": PAYLOAD_VERSION,
        "generated_at": utc_now().isoformat(timespec="seconds") + "Z",
        "window_hours": WINDOW_HOURS,
        "clusters": clusters,
        "incidents_by_code": dict(sorted(Counter(base_code(code) for code, _ in incidents).items())),
        "incidents_by_severity": dict(sorted(Counter(base_code(severity or "unknown") for _, severity in incidents).items())),
        "actions": {key: dict(sorted(value.items())) for key, value in sorted(by_action.items())},
    }


def push(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    state = load_state()
    if not enrolled(state):
        raise FederationError("Bản cài này chưa đăng ký federation.")
    payload = payload if payload is not None else build_payload()
    assert_anonymous(payload)
    match = _INBOX_RE.match(state["inbox_table"])
    if match is None:  # enrolled() checked it; keeps mypy and a hand-edited file honest
        raise FederationError("Tài khoản federation đã lưu không hợp lệ; hãy huỷ đăng ký và kích hoạt lại.")
    table = match.group(1)
    try:
        with _connect(state["login"], state["password"]) as connection:
            connection.execute(
                sql.SQL("INSERT INTO inbox.{} (payload_version, payload) VALUES (%s, %s::jsonb)").format(
                    sql.Identifier(table)),
                (PAYLOAD_VERSION, json.dumps(payload, ensure_ascii=False)))
    except psycopg.Error as exc:
        error = _server_error(exc)
        state["last_push_error"] = str(error)
        _save_state(state)
        raise error from exc
    state.update({"last_push_at": utc_now().isoformat(timespec="seconds") + "Z", "last_push_error": None})
    _save_state(state)
    return public_status()
