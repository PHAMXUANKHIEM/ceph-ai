import logging
import hashlib
import hmac
from collections import defaultdict
from datetime import datetime, timedelta
from shared.time import utc_now

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from dashboard.templating import make_templates
from shared import db
from shared.models import AuthLoginRateLimit, ChatPreference, User, VitastorUser

router = APIRouter()
templates = make_templates()
VALID_PRODUCTS = {"ceph", "vitastor"}
logger = logging.getLogger(__name__)


def _product_home(product: str | None) -> str:
    return "/vitastor" if product == "vitastor" else "/"


def _login_context(product: str, error: str | None = None) -> dict:
    return {"error": error, "product": product}

# A hash of a value nobody will ever submit as a real password — used to keep
# bcrypt.checkpw's (deliberately slow) cost constant regardless of whether the
# submitted username is valid, so response timing can't be used to enumerate
# whether an account exists.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode()

# Shared PostgreSQL rate limit.  The legacy dictionary remains only as a
# compatibility hook for old test fixtures; it is not consulted by login.
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 300
_failed_attempts: dict[str, list[float]] = defaultdict(list)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_locked_out(key: str) -> bool:
    now = utc_now()
    try:
        with db.SessionLocal() as session:
            row = session.get(AuthLoginRateLimit, key, with_for_update=True)
            if row is None:
                return False
            if now - row.window_started_at >= timedelta(seconds=LOCKOUT_WINDOW_SECONDS):
                session.delete(row)
                session.commit()
                return False
            return bool(row.locked_until and row.locked_until > now)
    except SQLAlchemyError:
        # A missing/unavailable shared store must never silently disable
        # brute-force protection on one replica.
        logger.exception("login rate-limit store is unavailable while checking %s", key)
        return True


def _insert_rate_limit_row(session, key: str, now: datetime) -> None:
    """Create the row once, tolerating simultaneous first attempts."""
    values = {
        "client_key": key,
        "failed_attempts": 0,
        "window_started_at": now,
        "updated_at": now,
    }
    dialect = session.bind.dialect.name
    if dialect == "postgresql":
        session.execute(
            postgres_insert(AuthLoginRateLimit)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["client_key"])
        )
    elif dialect == "sqlite":
        session.execute(
            sqlite_insert(AuthLoginRateLimit)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["client_key"])
        )
    else:
        session.execute(insert(AuthLoginRateLimit).values(**values))


def _get_locked_rate_limit_row(session, key: str, now: datetime):
    _insert_rate_limit_row(session, key, now)
    return session.execute(
        select(AuthLoginRateLimit)
        .where(AuthLoginRateLimit.client_key == key)
        .with_for_update()
    ).scalar_one()


def _record_failure(key: str) -> None:
    now = utc_now()
    try:
        with db.SessionLocal() as session:
            row = _get_locked_rate_limit_row(session, key, now)
            if now - row.window_started_at >= timedelta(seconds=LOCKOUT_WINDOW_SECONDS):
                row.failed_attempts = 1
                row.window_started_at = now
                row.locked_until = None
            else:
                row.failed_attempts += 1
                if row.failed_attempts >= MAX_LOGIN_ATTEMPTS:
                    row.locked_until = now + timedelta(seconds=LOCKOUT_WINDOW_SECONDS)
            row.updated_at = now
            session.commit()
    except SQLAlchemyError:
        logger.exception("login rate-limit store is unavailable while recording %s", key)
        raise


def _clear_failures(key: str) -> None:
    try:
        with db.SessionLocal() as session:
            session.execute(
                delete(AuthLoginRateLimit).where(AuthLoginRateLimit.client_key == key)
            )
            session.commit()
    except SQLAlchemyError:
        logger.exception("login rate-limit store is unavailable while clearing %s", key)
        raise


def _find_active_user(username: str) -> User | None:
    """Looks up an admin-created login account (shared/models.py::User) —
    separate from the single `.env`-configured account below, which never
    gets a row here. Uses `db.SessionLocal()` (module-attribute call, not
    `from shared.db import SessionLocal`) so this keeps working after the
    Settings page's runtime database-switch feature rebinds that attribute
    (see dashboard/routes/settings.py::_run_alembic_upgrade_head)."""
    with db.SessionLocal() as session:
        return (
            session.query(User)
            .filter(User.username == username, User.is_active.is_(True))
            .first()
        )


def _check_password(username: str, password: str, product: str = "ceph") -> bool:
    """Constant-cost check: always runs bcrypt exactly once, never
    short-circuits on whether the username matches a real account (the
    `.env` account or an active DB-created User row)."""
    if username == settings.dashboard_username:
        found = True
        hash_to_check = settings.dashboard_password_hash
    else:
        if product == "vitastor":
            with db.SessionLocal() as session:
                db_user = session.query(VitastorUser).filter(
                    VitastorUser.username == username, VitastorUser.is_active.is_(True)
                ).first()
        else:
            db_user = _find_active_user(username)
        found = db_user is not None
        hash_to_check = db_user.password_hash if db_user else _DUMMY_HASH
    try:
        password_matches = bcrypt.checkpw(password.encode(), hash_to_check.encode())
    except ValueError:
        # bcrypt rejects inputs over 72 bytes rather than silently truncating
        # (current bcrypt) — either way, that's simply not a valid password.
        password_matches = False
    return found and password_matches


def is_admin_user(username: str) -> bool:
    """Single source of truth for "is this account allowed to see the
    admin-only Settings sections (Tiến trình hệ thống / Kết nối Database /
    Người dùng)" — the `.env` account is always admin (the always-available
    root account, see shared/models.py::User's docstring), or an active
    DB-created User row with is_admin=True."""
    if username == settings.dashboard_username:
        return True
    db_user = _find_active_user(username)
    return db_user is not None and db_user.is_admin


def is_vitastor_admin_user(username: str) -> bool:
    """Vitastor equivalent of :func:`is_admin_user`, using its own table."""
    if username == settings.dashboard_username:
        return True
    with db.SessionLocal() as session:
        db_user = session.query(VitastorUser).filter(
            VitastorUser.username == username, VitastorUser.is_active.is_(True)
        ).first()
        return db_user is not None and db_user.is_admin


def is_ceph_chat_restricted(username: str) -> bool:
    """Whether chat must reject questions outside Ceph for this login.

    Every admin is always unrestricted, including the root account from
    ``.env``. Missing/inactive accounts fail closed because this helper is
    also safe to call independently of the login dependency.
    """
    if username == settings.dashboard_username:
        return False
    db_user = _find_active_user(username)
    if db_user is None:
        return True
    return False if db_user.is_admin else db_user.ceph_chat_restricted


def chat_ai_name(username: str) -> str:
    """Return this login's configured assistant display/persona name."""
    try:
        with db.SessionLocal() as session:
            preference = session.get(ChatPreference, username)
            return preference.ai_name if preference is not None else "AI"
    except SQLAlchemyError:
        # Rolling deployment safety: old DB schema remains usable until the
        # migration step creates chat_preferences.
        return "AI"


def chat_female_address(username: str) -> str:
    """Return the configured feminine opening phrase for this login."""
    try:
        with db.SessionLocal() as session:
            preference = session.get(ChatPreference, username)
            return preference.female_address if preference is not None else "Mình yêu ơi, em là"
    except (SQLAlchemyError, AttributeError):
        return "Mình yêu ơi, em là"


def _root_session_fingerprint(product: str) -> str:
    """Bind the env-backed root session to the current credential material."""
    material = "\0".join((product, settings.dashboard_username, settings.dashboard_password_hash))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _session_claims_for_login(username: str, product: str) -> dict | None:
    """Build server-owned claims; never trust identity claims from the client."""
    if username == settings.dashboard_username:
        return {
            "user": username,
            "product": product,
            "auth_subject": "root",
            "root_session_fingerprint": _root_session_fingerprint(product),
        }
    model = VitastorUser if product == "vitastor" else User
    with db.SessionLocal() as session:
        account = session.query(model).filter(
            model.username == username,
            model.is_active.is_(True),
        ).first()
        if account is None:
            return None
        return {
            "user": account.username,
            "product": product,
            "auth_subject": "database",
            "user_id": account.id,
            "session_version": int(account.session_version),
        }


def _session_is_valid(session_data: dict) -> bool:
    """Validate active account state on every HTTP/WebSocket authentication."""
    username = session_data.get("user")
    product = session_data.get("product")
    subject = session_data.get("auth_subject")
    if not isinstance(username, str) or product not in VALID_PRODUCTS:
        return False
    if subject == "root":
        expected = _root_session_fingerprint(product)
        supplied = session_data.get("root_session_fingerprint")
        return isinstance(supplied, str) and hmac.compare_digest(supplied, expected)
    if subject != "database" or not isinstance(session_data.get("user_id"), str):
        return False
    try:
        expected_version = int(session_data.get("session_version", -1))
    except (TypeError, ValueError):
        return False
    model = VitastorUser if product == "vitastor" else User
    with db.SessionLocal() as session:
        account = session.get(model, session_data["user_id"])
        return bool(
            account is not None
            and account.is_active
            and account.username == username
            and int(account.session_version) == expected_version
        )


async def require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user or not _session_is_valid(request.session):
        # Clear revoked claims before redirecting so the browser cannot keep
        # presenting a stale identity on the next request.
        request.session.clear()
        # 303 + Location header is honored as a redirect by browsers and by
        # httpx/starlette's TestClient regardless of it being raised via
        # HTTPException rather than returned as a RedirectResponse.
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if request.session.get("user"):
        return RedirectResponse(_product_home(request.session.get("product")), status_code=303)
    if request.query_params.get("change") == "1":
        request.session.pop("login_product", None)
        return templates.TemplateResponse(request, "product_select.html", {})
    requested_product = request.query_params.get("product", "").strip().lower()
    if requested_product:
        if requested_product not in VALID_PRODUCTS:
            raise HTTPException(status_code=404, detail="Hệ thống không hợp lệ")
        request.session["login_product"] = requested_product
    else:
        requested_product = request.session.get("login_product", "")
    if requested_product not in VALID_PRODUCTS:
        return templates.TemplateResponse(request, "product_select.html", {})
    return templates.TemplateResponse(request, "login.html", _login_context(requested_product))


@router.post("/product/select")
async def select_product(request: Request, product: str = Form(...)):
    product = product.strip().lower()
    if product not in VALID_PRODUCTS:
        raise HTTPException(status_code=400, detail="Hệ thống không hợp lệ")
    request.session.clear()
    request.session["login_product"] = product
    return RedirectResponse(f"/login?product={product}", status_code=303)


@router.post("/login")
async def login_submit(
    request: Request, username: str = Form(...), password: str = Form(...),
    product: str = Form("ceph"),
):
    product = product.strip().lower()
    if product not in VALID_PRODUCTS:
        raise HTTPException(status_code=400, detail="Hệ thống không hợp lệ")
    client_key = _client_key(request)
    if _is_locked_out(client_key):
        return templates.TemplateResponse(
            request,
            "login.html",
            _login_context(product, "Quá nhiều lần đăng nhập sai — thử lại sau ít phút"),
            status_code=429,
        )

    if not _check_password(username, password, product):
        _record_failure(client_key)
        return templates.TemplateResponse(
            request,
            "login.html",
            _login_context(product, "Sai tên đăng nhập hoặc mật khẩu"),
            status_code=401,
        )

    claims = _session_claims_for_login(username, product)
    if claims is None:
        _record_failure(client_key)
        return templates.TemplateResponse(
            request,
            "login.html",
            _login_context(product, "Tài khoản không còn hoạt động"),
            status_code=401,
        )
    _clear_failures(client_key)
    request.session.clear()
    request.session.update(claims)
    return RedirectResponse(_product_home(product), status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
