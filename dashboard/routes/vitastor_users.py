"""Independent Vitastor account administration."""

import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.vitastor import require_vitastor_login
from dashboard.templating import make_templates
from shared import db
from shared.models import VitastorUser

router = APIRouter(prefix="/vitastor/users", tags=["vitastor-users"])
templates = make_templates()
MIN_PASSWORD_LENGTH = 8


def _require_admin(user: str) -> None:
    if not auth.is_vitastor_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ Vitastor Admin được quản lý người dùng")


def _context(user: str, *, error: str | None = None, success: str | None = None) -> dict:
    with db.SessionLocal() as session:
        users = session.query(VitastorUser).order_by(VitastorUser.created_at.desc()).all()
    return {"user": user, "users": users, "error": error, "success": success}


@router.get("", response_class=HTMLResponse)
async def users_page(request: Request, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    return templates.TemplateResponse(request, "vitastor/users.html", _context(user))


@router.post("/create", response_class=HTMLResponse)
async def create_user(
    request: Request,
    user: str = Depends(require_vitastor_login),
    new_username: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
    new_is_admin: str = Form(""),
):
    _require_admin(user)
    username = new_username.strip()
    error = None
    if not username or not new_password:
        error = "Vui lòng điền Username và Mật khẩu."
    elif username == settings.dashboard_username:
        error = f"Username {username!r} là tài khoản root, hãy chọn tên khác."
    elif len(new_password) < MIN_PASSWORD_LENGTH:
        error = f"Mật khẩu phải có ít nhất {MIN_PASSWORD_LENGTH} ký tự."
    elif new_password != new_password_confirm:
        error = "Mật khẩu nhập lại không khớp."

    if error is None:
        with db.SessionLocal() as session:
            if session.query(VitastorUser).filter_by(username=username).first() is not None:
                error = f"Username {username!r} đã tồn tại trong Vitastor."
            else:
                session.add(VitastorUser(
                    username=username,
                    password_hash=bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode(),
                    is_admin=new_is_admin.lower() in {"on", "true", "1"},
                    is_active=True,
                    created_by=user,
                ))
                session.commit()
    return templates.TemplateResponse(
        request, "vitastor/users.html",
        _context(user, error=error, success=None if error else f"Đã tạo Vitastor user {username!r}."),
    )


@router.post("/{user_id}/toggle-active", response_class=HTMLResponse)
async def toggle_active(request: Request, user_id: str, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    error = None
    with db.SessionLocal() as session:
        target = session.get(VitastorUser, user_id)
        if target is None:
            error = "Không tìm thấy Vitastor user."
        elif target.username == user:
            error = "Không thể tự vô hiệu hoá tài khoản đang đăng nhập."
        else:
            target.is_active = not target.is_active
            session.commit()
    return templates.TemplateResponse(request, "vitastor/users.html", _context(user, error=error))


@router.post("/{user_id}/delete", response_class=HTMLResponse)
async def delete_user(request: Request, user_id: str, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    error = None
    deleted = None
    with db.SessionLocal() as session:
        target = session.get(VitastorUser, user_id)
        if target is None:
            error = "Không tìm thấy Vitastor user."
        elif target.username == user:
            error = "Không thể tự xoá tài khoản đang đăng nhập."
        else:
            deleted = target.username
            session.delete(target)
            session.commit()
    return templates.TemplateResponse(
        request, "vitastor/users.html",
        _context(user, error=error, success=f"Đã xoá Vitastor user {deleted!r}." if deleted else None),
    )
