"""Admin-only settings API for a host-managed Grafana OpenVPN tunnel."""

import base64
import json
import socket

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from dashboard.routes import auth
from dashboard.routes.auth import require_login


router = APIRouter()
SOCKET_PATH = "/tmp/grafana-vpn-control.sock"
MAX_PROFILE_BYTES = 256 * 1024


def _admin(user: str = Depends(require_login)) -> str:
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được quản lý VPN.")
    return user


def _host_request(payload: dict) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(25)
            connection.connect(SOCKET_PATH)
            connection.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
            data = bytearray()
            while len(data) < 16384:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
                if b"\n" in chunk:
                    break
        result = json.loads(data.decode("utf-8"))
    except (OSError, ValueError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail="Host VPN helper chưa sẵn sàng.") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Host VPN helper trả về dữ liệu không hợp lệ.")
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/api/settings/grafana-vpn")
def grafana_vpn_status(user: str = Depends(_admin)) -> dict:
    return _host_request({"action": "status"})


@router.post("/api/settings/grafana-vpn/profile")
async def grafana_vpn_profile(
    profile: UploadFile = File(...),
    target: str = Form(...),
    user: str = Depends(_admin),
) -> dict:
    name = profile.filename or ""
    if not name.lower().endswith(".ovpn") or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Chỉ chọn một file .ovpn.")
    data = await profile.read(MAX_PROFILE_BYTES + 1)
    if not data or len(data) > MAX_PROFILE_BYTES:
        raise HTTPException(status_code=400, detail="File .ovpn phải nhỏ hơn 256 KiB.")
    return _host_request({"action": "upload", "profile": base64.b64encode(data).decode("ascii"), "target": target})


@router.post("/api/settings/grafana-vpn/connect")
def grafana_vpn_connect(user: str = Depends(_admin)) -> dict:
    return _host_request({"action": "connect"})


@router.post("/api/settings/grafana-vpn/disconnect")
def grafana_vpn_disconnect(user: str = Depends(_admin)) -> dict:
    return _host_request({"action": "disconnect"})
