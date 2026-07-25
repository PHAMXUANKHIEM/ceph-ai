import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from shared.models import User

router = APIRouter()
templates = make_templates()

MIN_USER_PASSWORD_LENGTH = 8


def _require_admin_privilege(user: str) -> None:
    if not auth.is_admin_user(user):
        raise HTTPException(
            status_code=403,
            detail="Chỉ tài khoản admin mới được phép thực hiện thao tác này",
        )


def _list_users() -> list[User]:
    """Every admin-created login account (shared/models.py::User), newest
    first — never includes the `.env`-configured account itself (it isn't a
    User row, see that model's docstring)."""
    with db.SessionLocal() as session:
        return session.query(User).order_by(User.created_at.desc()).all()


def _users_context(
    user: str,
    *,
    user_create_error: str | None = None,
    user_create_success: str | None = None,
    user_toggle_error: str | None = None,
    user_delete_error: str | None = None,
    user_delete_success: str | None = None,
) -> dict:
    return {
        "user": user,
        "users": _list_users(),
        "user_create_error": user_create_error,
        "user_create_success": user_create_success,
        "user_toggle_error": user_toggle_error,
        "user_delete_error": user_delete_error,
        "user_delete_success": user_delete_success,
    }


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, user: str = Depends(require_login)):
    """Standalone "Người dùng" page — its own top-nav item (alongside
    Dashboard/Metrics Cluster/Upgrade Cluster/Cài đặt), not a card inside
    Settings anymore. Admin-only (403, not just a hidden nav link — a
    non-admin typing the URL directly must not see the user list either)."""
    _require_admin_privilege(user)
    return templates.TemplateResponse(request, "users.html", _users_context(user))


@router.post("/users/create", response_class=HTMLResponse)
async def create_user(
    request: Request,
    user: str = Depends(require_login),
    new_username: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
    new_is_admin: str = Form(""),
):
    """Backs the "Người dùng" page's create-user form — an admin (the `.env`
    account or any active DB-created User with is_admin=True, see
    auth.is_admin_user) creates a login account for someone else. Always
    non-blocking/instant (no migration/restart involved) — a plain insert,
    so this just re-renders the same page directly."""
    _require_admin_privilege(user)

    username = new_username.strip()
    is_admin_flag = new_is_admin.lower() in ("on", "true", "1")

    error: str | None = None
    if not username or not new_password:
        error = "Vui lòng điền Username và Mật khẩu."
    elif username == settings.dashboard_username:
        error = f"Username {username!r} đã được dùng cho tài khoản admin gốc (.env) — chọn tên khác."
    elif len(new_password) < MIN_USER_PASSWORD_LENGTH:
        error = f"Mật khẩu phải có ít nhất {MIN_USER_PASSWORD_LENGTH} ký tự."
    elif new_password != new_password_confirm:
        error = "Mật khẩu nhập lại không khớp."

    if error is None:
        with db.SessionLocal() as session:
            if session.query(User).filter(User.username == username).first() is not None:
                error = f"Username {username!r} đã tồn tại."
            else:
                session.add(
                    User(
                        username=username,
                        password_hash=bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode(),
                        is_admin=is_admin_flag,
                        is_active=True,
                        created_by=user,
                    )
                )
                session.commit()

    if error:
        return templates.TemplateResponse(request, "users.html", _users_context(user, user_create_error=error))
    return templates.TemplateResponse(
        request,
        "users.html",
        _users_context(user, user_create_success=f"Đã tạo user {username!r}."),
    )


@router.post("/users/{user_id}/toggle-active", response_class=HTMLResponse)
async def toggle_user_active(request: Request, user_id: str, user: str = Depends(require_login)):
    """Soft-disable/re-enable toggle for a DB-created User row (shared/models.py::User's
    is_active — never a hard delete, see that model's docstring). The `.env`
    account can never be a target here (it has no User row/id at all)."""
    _require_admin_privilege(user)

    with db.SessionLocal() as session:
        target = session.get(User, user_id)
        if target is None:
            return templates.TemplateResponse(
                request,
                "users.html",
                _users_context(user, user_toggle_error="Không tìm thấy user."),
            )
        if target.username == user:
            return templates.TemplateResponse(
                request,
                "users.html",
                _users_context(user, user_toggle_error="Không thể tự vô hiệu hoá chính tài khoản đang đăng nhập."),
            )
        target.is_active = not target.is_active
        session.commit()

    return templates.TemplateResponse(request, "users.html", _users_context(user))


@router.post("/users/{user_id}/delete", response_class=HTMLResponse)
async def delete_user(request: Request, user_id: str, user: str = Depends(require_login)):
    """Hard delete — unlike toggle-active (soft-disable, the normal way to
    revoke access without losing the row), this permanently removes the
    User row. Safe schema-wise: nothing else references User.id as a
    foreign key (AuditEntry.actor and every other "who did this" field in
    this codebase is a plain username string, not a FK — see
    shared/models.py::AuditEntry's own docstring), so past audit history
    for this username is untouched by the delete. The `.env` account can
    never be a target here (it has no User row/id at all)."""
    _require_admin_privilege(user)

    with db.SessionLocal() as session:
        target = session.get(User, user_id)
        if target is None:
            return templates.TemplateResponse(
                request,
                "users.html",
                _users_context(user, user_delete_error="Không tìm thấy user."),
            )
        if target.username == user:
            return templates.TemplateResponse(
                request,
                "users.html",
                _users_context(user, user_delete_error="Không thể tự xoá chính tài khoản đang đăng nhập."),
            )
        deleted_username = target.username
        session.delete(target)
        session.commit()

    return templates.TemplateResponse(
        request,
        "users.html",
        _users_context(user, user_delete_success=f"Đã xoá user {deleted_username!r}."),
    )
