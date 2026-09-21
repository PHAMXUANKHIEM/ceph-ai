"""Read-only HashiCorp Vault security monitor with a dedicated Telegram channel."""
from __future__ import annotations
import argparse
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any
import httpx
from config.settings import settings
from shared import env_config, telegram_outbox
from shared.vault_alerts import send_vault_alert as _send_vault_alert_direct

logger = logging.getLogger(__name__)
_ENV_NAMES = [
    "VAULT_ADDR", "VAULT_TOKEN", "VAULT_TOKEN_FILE", "VAULT_MONITOR_ENABLED",
    "VAULT_POLL_INTERVAL_SECONDS", "VAULT_TOKEN_EXPIRY_WARNING_SECONDS", "VAULT_TLS_VERIFY",
]


def _live_config() -> dict[str, Any]:
    raw = env_config.read_env_values(_ENV_NAMES)
    def value(name: str, fallback: Any) -> Any:
        return raw[name] if name in raw else fallback
    def boolean(name: str, fallback: bool) -> bool:
        return str(value(name, fallback)).strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return {
        "addr": str(value("VAULT_ADDR", settings.vault_addr)).strip().rstrip("/"),
        "token": str(value("VAULT_TOKEN", os.environ.get("VAULT_TOKEN", ""))).strip(),
        "token_file": str(value("VAULT_TOKEN_FILE", settings.vault_token_file)).strip(),
        "enabled": boolean("VAULT_MONITOR_ENABLED", settings.vault_monitor_enabled),
        "interval": max(10, int(value("VAULT_POLL_INTERVAL_SECONDS", settings.vault_poll_interval_seconds))),
        "ttl_warning": max(60, int(value("VAULT_TOKEN_EXPIRY_WARNING_SECONDS", settings.vault_token_expiry_warning_seconds))),
        "tls_verify": boolean("VAULT_TLS_VERIFY", settings.vault_tls_verify),
    }


def _read_token(config: dict[str, Any]) -> str:
    path = Path(config["token_file"])
    try:
        if path.is_file():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
    except OSError:
        logger.warning("Vault token file cannot be read: %s", path)
    return config["token"]


def send_vault_alert(
    title: str,
    severity: str,
    detail: str,
    remediation: str | None = None,
) -> bool:
    """Queue Vault findings through the durable alert delivery path."""
    fingerprint = hashlib.sha256(
        f"{title}|{severity}|{detail}|{remediation or ''}".encode("utf-8")
    ).hexdigest()[:24]
    return telegram_outbox.enqueue_alert_call_and_dispatch(
        event_id=f"vault:{fingerprint}",
        category="vault",
        function="send_vault_alert",
        args=(title, severity, detail, remediation),
        sender=_send_vault_alert_direct,
    )


class VaultMonitor:
    def __init__(self) -> None:
        self._active: dict[str, str] = {}

    @staticmethod
    def _request(client: httpx.Client, addr: str, path: str, token: str = "") -> tuple[int, dict[str, Any]]:
        response = client.get(f"{addr}{path}", headers={"X-Vault-Token": token} if token else {})
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body if isinstance(body, dict) else {}

    def _notify(self, finding: dict[str, str]) -> None:
        signature = "|".join(finding.get(name, "") for name in ("severity", "title", "detail"))
        if self._active.get(finding["key"]) == signature:
            return
        self._active[finding["key"]] = signature
        send_vault_alert(finding["title"], finding["severity"], finding["detail"], finding.get("remediation"))

    def _recover(self, key: str, title: str) -> None:
        if key in self._active:
            self._active.pop(key)
            send_vault_alert(title, "info", "Kiểm tra mới nhất đã xác nhận trạng thái bình thường trở lại.")

    def check_once(self, *, notify: bool = True) -> list[dict[str, str]]:
        config = _live_config()
        if not config["enabled"] or not config["addr"]:
            return []
        findings: list[dict[str, str]] = []
        token = _read_token(config)
        try:
            with httpx.Client(timeout=8, verify=config["tls_verify"]) as client:
                code, health = self._request(client, config["addr"], "/v1/sys/health")
                if code >= 500 and not health:
                    raise RuntimeError(f"HTTP {code}")
                if not health.get("initialized", True):
                    findings.append({"key": "uninitialized", "severity": "critical", "title": "Vault chưa được khởi tạo", "detail": "Vault chưa initialized; secret chưa sẵn sàng.", "remediation": "Khởi tạo Vault theo quy trình bootstrap được phê duyệt."})
                elif health.get("sealed"):
                    findings.append({"key": "sealed", "severity": "critical", "title": "Vault đang SEALED", "detail": "Các secret không thể truy cập cho đến khi unseal đủ quorum key holder.", "remediation": "Thực hiện unseal theo runbook; không gửi unseal keys vào chat/email."})
                else:
                    self._recover("uninitialized", "Vault đã initialized")
                    self._recover("sealed", "Vault đã UNSEALED")
                if not token:
                    findings.append({"key": "token-missing", "severity": "warning", "title": "Không kiểm tra được token Vault", "detail": "Chưa cấu hình VAULT_TOKEN_FILE hoặc VAULT_TOKEN; không thể kiểm tra TTL/root token.", "remediation": "Cấu hình token lookup-self giới hạn quyền, không dùng root token."})
                else:
                    audit_code, audit = self._request(client, config["addr"], "/v1/sys/audit", token)
                    audit_devices = audit.get("data") or {}
                    if audit_code in {200, 204} and not audit_devices:
                        findings.append({"key": "audit-disabled", "severity": "critical", "title": "Audit log Vault đang TẮT", "detail": "Không phát hiện audit device nào đang hoạt động; request Vault không được ghi log.", "remediation": "Bật audit device file/syslog theo runbook và kiểm tra lại."})
                    elif audit_code in {200, 204}:
                        self._recover("audit-disabled", "Audit log Vault đang hoạt động")
                    else:
                        findings.append({"key": "audit-unknown", "severity": "warning", "title": "Không kiểm tra được audit device Vault", "detail": f"Vault trả HTTP {audit_code} khi đọc /v1/sys/audit.", "remediation": "Kiểm tra token có quyền read sys/audit và xác nhận audit device trực tiếp."})
                    token_code, token_body = self._request(client, config["addr"], "/v1/auth/token/lookup-self", token)
                    if token_code == 200:
                        data = token_body.get("data") or {}
                        if "root" in {str(item).lower() for item in (data.get("policies") or [])}:
                            findings.append({"key": "root-token", "severity": "critical", "title": "Đang thao tác bằng ROOT TOKEN", "detail": "Root token có toàn quyền trên Vault; chỉ nên dùng tạm thời khi bootstrap.", "remediation": "Tạo token least-privilege và revoke root token ngay sau khi hoàn tất."})
                        else:
                            self._recover("root-token", "Không còn phát hiện ROOT TOKEN")
                        ttl = int(data.get("ttl") or 0)
                        if 0 < ttl <= config["ttl_warning"]:
                            findings.append({"key": "token-expiry", "severity": "warning", "title": "Token Vault sắp hết hạn", "detail": f"TTL còn khoảng {ttl} giây.", "remediation": "Renew token hoặc lấy token mới trước khi hết hạn để tránh gián đoạn."})
                        else:
                            self._recover("token-expiry", "TTL token Vault đã an toàn")
                    elif token_code in {401, 403}:
                        findings.append({"key": "token-lookup", "severity": "warning", "title": "Không lookup được token Vault", "detail": f"Vault trả HTTP {token_code} khi kiểm tra lookup-self.", "remediation": "Dùng token giới hạn quyền có thể lookup-self; không dùng root token hằng ngày."})
        except Exception as exc:
            findings.append({"key": "unreachable", "severity": "critical", "title": "Không kết nối được Vault", "detail": f"Không đọc được /v1/sys/health tại {config['addr']}: {type(exc).__name__}.", "remediation": "Kiểm tra Vault listener, TLS, DNS/firewall và VAULT_ADDR."})
        active_keys = {finding["key"] for finding in findings}
        if notify:
            for finding in findings:
                self._notify(finding)
            for key, title in {"unreachable": "Kết nối Vault đã phục hồi", "audit-unknown": "Đã đọc được audit device Vault", "token-lookup": "Đã lookup được token Vault", "token-missing": "Đã cấu hình token kiểm tra Vault"}.items():
                if key not in active_keys:
                    self._recover(key, title)
        return findings

    def run_forever(self) -> None:
        logger.info("Vault security monitor started")
        while True:
            try:
                config = _live_config()
                self.check_once()
                time.sleep(config["interval"])
            except Exception:
                logger.exception("Vault security monitor cycle failed")
                time.sleep(60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Vault security monitor")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    monitor = VaultMonitor()
    if args.once:
        for finding in monitor.check_once(notify=False):
            print(f"{finding['severity'].upper()}: {finding['title']} — {finding['detail']}")
        return
    monitor.run_forever()


if __name__ == "__main__":
    main()
