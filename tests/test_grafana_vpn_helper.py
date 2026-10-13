import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "deploy" / "grafana_vpn_helper.py"
SPEC = importlib.util.spec_from_file_location("grafana_vpn_helper", MODULE_PATH)
vpn = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vpn)


PROFILE = b"""client
dev tun
proto udp
remote vpn.example.org 1194
remote-cert-tls server
<ca>
CA DATA
</ca>
<cert>
CERT DATA
</cert>
<key>
KEY DATA
</key>
"""
PASSWORD_PROFILE = b"""client
dev tun
proto tcp
remote vpn.example.org 13142
remote-cert-tls server
auth-user-pass
reneg-sec 0
<ca>
CA DATA
</ca>
<tls-auth>
TLS AUTH DATA
</tls-auth>
"""


def test_accepts_inline_certificate_profile():
    assert vpn.validate_profile(PROFILE).startswith("client\n")


def test_accepts_inline_ca_with_interactive_password():
    result = vpn.validate_profile(PASSWORD_PROFILE)
    assert f"auth-user-pass {vpn.CREDENTIALS}\n" in result
    assert "auth-user-pass\n" not in result


def test_otp_credentials_are_runtime_only_and_not_autostarted(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    credentials = runtime / "credentials"
    monkeypatch.setattr(vpn, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(vpn, "CREDENTIALS", credentials)
    monkeypatch.setattr(vpn, "status", lambda: {"installed": True, "configured": True, "state": "connecting"})
    monkeypatch.setattr(vpn, "_auth_required", lambda: True)
    calls = []
    monkeypatch.setattr(vpn, "_systemctl", lambda *args: calls.append(args) or args != ("is-active", "--quiet"))

    result = vpn.handle({"action": "connect", "username": "operator", "password": "password-plus-otp"})
    assert result["configured"] is True
    assert credentials.read_bytes() == b"operator\npassword-plus-otp\n"
    assert credentials.stat().st_mode & 0o777 == 0o600
    assert ("start",) in calls
    assert not any("enable" in args for args in calls)
    vpn.handle({"action": "disconnect"})
    assert not credentials.exists()


def test_failed_otp_start_removes_runtime_credentials(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    credentials = runtime / "credentials"
    monkeypatch.setattr(vpn, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(vpn, "CREDENTIALS", credentials)
    monkeypatch.setattr(vpn, "status", lambda: {"installed": True, "configured": True})
    monkeypatch.setattr(vpn, "_auth_required", lambda: True)
    monkeypatch.setattr(vpn, "_systemctl", lambda *args: False)
    with pytest.raises(ValueError, match="Không khởi động"):
        vpn.handle({"action": "connect", "username": "operator", "password": "fresh-otp"})
    assert not credentials.exists()


def test_certificate_profile_keeps_existing_autostart(monkeypatch):
    monkeypatch.setattr(vpn, "status", lambda: {"installed": True, "configured": True})
    monkeypatch.setattr(vpn, "_auth_required", lambda: False)
    calls = []
    monkeypatch.setattr(vpn, "_systemctl", lambda *args: calls.append(args) or args != ("is-active", "--quiet"))
    vpn.handle({"action": "connect"})
    assert ("enable", "--now") in calls


@pytest.mark.parametrize("username,password", (("", "secret"), ("operator", ""), ("user\nroot", "secret"),
                                              ("operator", "secret\nroot")))
def test_rejects_missing_or_multiline_credentials(username, password):
    with pytest.raises(ValueError):
        vpn._credentials({"username": username, "password": password})


@pytest.mark.parametrize("directive", (
    b"script-security 2", b"up /bin/sh", b"plugin /tmp/evil.so",
    b"redirect-gateway def1", b"route 0.0.0.0 0.0.0.0",
    b"pull-filter ignore route", b"auth-user-pass /tmp/secret",
    b"management 127.0.0.1 5555", b"ca /tmp/other.pem",
))
def test_rejects_untrusted_or_route_changing_directives(directive):
    with pytest.raises(ValueError):
        vpn.validate_profile(PROFILE + directive + b"\n")


def test_upload_only_requires_profile_and_does_not_set_routes(tmp_path, monkeypatch):
    import base64

    config = tmp_path / "vpn"
    monkeypatch.setattr(vpn, "CONFIG_DIR", config)
    monkeypatch.setattr(vpn, "PROFILE", config / "grafana.ovpn")
    monkeypatch.setattr(vpn, "CREDENTIALS", tmp_path / "credentials")
    monkeypatch.setattr(vpn, "_systemctl", lambda *args: args == ("disable",))
    result = vpn.handle({"action": "upload", "profile": base64.b64encode(PASSWORD_PROFILE).decode("ascii")})
    assert result["configured"] is True
    assert result["auth_required"] is True
    assert "target" not in result
    assert not (config / "grafana-target.env").exists()


def test_connected_state_requires_own_tunnel_device(tmp_path, monkeypatch):
    profile = tmp_path / "profile.ovpn"
    profile.write_text(f"auth-user-pass {vpn.CREDENTIALS}\n", encoding="utf-8")
    monkeypatch.setattr(vpn, "PROFILE", profile)
    monkeypatch.setattr(vpn, "_systemctl", lambda *args: True)
    monkeypatch.setattr(vpn, "_tunnel_present", lambda: False)
    assert vpn.status()["state"] == "connecting"
    monkeypatch.setattr(vpn, "_tunnel_present", lambda: True)
    result = vpn.status()
    assert result["state"] == "connected"
    assert result["route_device"] == "cephvpn0"


def test_vpn_unit_opens_tunnel_without_modifying_routes_or_dns():
    unit = (MODULE_PATH.parent / "systemd" / "ceph-ai-grafana-vpn.service").read_text(encoding="utf-8")
    assert "--dev cephvpn0" in unit
    assert "--route-nopull" in unit
    assert "--route ${GRAFANA_IP}" not in unit
    assert "EnvironmentFile=" not in unit
    assert "--up " not in unit
