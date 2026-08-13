import time
from collections import defaultdict

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from dashboard.templating import make_templates
from shared import db
from shared.models import ChatPreference, User

router = APIRouter()
templates = make_templates()
VALID_PRODUCTS = {"ceph", "vitastor"}


def _product_home(product: str | None) -> str:
    return "/vitastor" if product == "vitastor" else "/"


def _login_context(product: str, error: str | None = None) -> dict:
    return {"error": error, "product": product}

# A hash of a value nobody will ever submit as a real password — used to keep
# bcrypt.checkpw's (deliberately slow) cost constant regardless of whether the
# submitted username is valid, so response timing can't be used to enumerate
# whether an account exists.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode()

# Simple in-memory rate limit: single-process, resets on restart — adequate
# for a single static account, not meant to survive multi-worker deployment.
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 300
_failed_attempts: dict[str, list[float]] = defaultdict(list)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_locked_out(key: str) -> bool:
    now = time.monotonic()
    _failed_attempts[key] = [t for t in _failed_attempts[key] if now - t < LOCKOUT_WINDOW_SECONDS]
    return len(_failed_attempts[key]) >= MAX_LOGIN_ATTEMPTS


def _record_failure(key: str) -> None:
    _failed_attempts[key].append(time.monotonic())


def _clear_failures(key: str) -> None:
    _failed_attempts.pop(key, None)


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


def _check_password(username: str, password: str) -> bool:
    """Constant-cost check: always runs bcrypt exactly once, never
    short-circuits on whether the username matches a real account (the
    `.env` account or an active DB-created User row)."""
    if username == settings.dashboard_username:
        found = True
        hash_to_check = settings.dashboard_password_hash
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


async def require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user:
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

    if not _check_password(username, password):
        _record_failure(client_key)
        return templates.TemplateResponse(
            request,
            "login.html",
            _login_context(product, "Sai tên đăng nhập hoặc mật khẩu"),
            status_code=401,
        )

    _clear_failures(client_key)
    request.session.clear()
    request.session["user"] = username
    request.session["product"] = product
    return RedirectResponse(_product_home(product), status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
