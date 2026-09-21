"""Safe bounded connectivity probes for configured backup targets."""

from __future__ import annotations

import hashlib
import io
import re
import time
import uuid

from config.settings import Settings
from worker.backup.storage.factory import get_backend

_SENSITIVE_RE = re.compile(
    r"(?i)(access[_ -]?key|secret[_ -]?key|token|password|private[_ -]?key|key[_ -]?path)"
    r"\s*[:=]\s*[^,;\s]+"
)


def _safe_error(exc: Exception, secrets: list[str]) -> str:
    text = str(exc)
    for value in secrets:
        if value:
            text = text.replace(value, "[redacted]")
    return _SENSITIVE_RE.sub(lambda match: match.group(1) + "=[redacted]", text)[:400]


def _target_identity(slot: str, settings: Settings) -> tuple[str, ...] | None:
    transport = str(getattr(settings, f"backup_target_{slot}_transport", "") or "")
    if transport == "s3":
        return ("s3", str(getattr(settings, f"backup_target_{slot}_s3_endpoint", "") or "").lower(),
                str(getattr(settings, f"backup_target_{slot}_s3_bucket", "") or "").lower())
    if transport == "ssh":
        return ("ssh", str(getattr(settings, f"backup_target_{slot}_ssh_host", "") or "").lower(),
                str(getattr(settings, f"backup_target_{slot}_ssh_landing_dir", "") or "").rstrip("/").lower())
    return None


def duplicate_target_warnings(slot: str, settings: Settings) -> list[str]:
    current = _target_identity(slot, settings)
    if current is None:
        return []
    return [f"SLOT_{other.upper()}_TRUNG_CÙNG_TARGET" for other in ("a", "b")
            if other != slot and current == _target_identity(other, settings)]


def _step(steps: list[dict], name: str, fn, *, secrets: list[str]) -> object:
    started = time.monotonic()
    try:
        result = fn()
    except Exception as exc:
        steps.append({"name": name, "status": "failed",
                      "duration_ms": round((time.monotonic() - started) * 1000),
                      "detail": _safe_error(exc, secrets)})
        raise
    steps.append({"name": name, "status": "passed",
                  "duration_ms": round((time.monotonic() - started) * 1000),
                  "detail": result if isinstance(result, str) else None})
    return result


def probe_target(slot: str, settings: Settings, *, immutable_enabled: bool = False) -> dict:
    """Probe one target with a random short-lived object, then clean it up."""
    del immutable_enabled  # Never create a compliance-locked probe artifact.
    if slot not in {"a", "b"}:
        raise ValueError("slot phải là a hoặc b")
    transport = str(getattr(settings, f"backup_target_{slot}_transport", "") or "")
    label = str(getattr(settings, f"backup_target_{slot}_label", "") or f"Slot {slot.upper()}")
    secrets = [str(getattr(settings, f"backup_target_{slot}_s3_access_key", "") or ""),
               str(getattr(settings, f"backup_target_{slot}_s3_secret_key", "") or ""),
               str(getattr(settings, f"backup_target_{slot}_ssh_key_path", "") or "")]
    result = {"slot": slot, "label": label, "transport": transport or None,
              "status": "failed", "warnings": duplicate_target_warnings(slot, settings),
              "steps": [], "cleanup": {"status": "not_started"}}
    if transport not in {"ssh", "s3"}:
        result["steps"].append({"name": "configuration", "status": "failed", "duration_ms": 0,
                                "detail": "Target chưa được cấu hình (chọn SSH/SFTP hoặc S3)."})
        return result
    backend = None
    remote_key = None
    try:
        backend = get_backend(slot, settings, immutable_enabled=False)
        metadata = getattr(backend, "probe_metadata", None)
        if metadata is not None:
            metadata_result = _step(result["steps"], "connection_and_metadata", metadata, secrets=secrets)
            if isinstance(metadata_result, dict):
                result["metadata"] = metadata_result
        else:
            _step(result["steps"], "connection", lambda: "Backend đã khởi tạo", secrets=secrets)
        payload = (f"ceph-ai-target-probe:{slot}:{uuid.uuid4().hex}\n").encode("utf-8")
        expected_sha = hashlib.sha256(payload).hexdigest()
        remote_key = f".ceph-ai/probe/{uuid.uuid4().hex}.bin"
        uploaded = _step(result["steps"], "write_probe_object",
                         lambda: backend.upload(io.BytesIO(payload), remote_key), secrets=secrets)
        if uploaded.size != len(payload) or uploaded.sha256 != expected_sha:
            raise RuntimeError("Target trả về checksum/kích thước artifact ghi không khớp.")
        verified = _step(result["steps"], "stat_and_verify",
                         lambda: backend.verify(remote_key, len(payload), expected_sha), secrets=secrets)
        if verified is not True:
            raise RuntimeError("Target không xác nhận được kích thước hoặc checksum artifact.")
        downloaded = io.BytesIO()
        _step(result["steps"], "download_probe_object",
              lambda: backend.download(remote_key, downloaded), secrets=secrets)
        if downloaded.getvalue() != payload:
            raise RuntimeError("Artifact tải về không khớp payload probe.")
        _step(result["steps"], "delete_probe_object",
              lambda: backend.delete(remote_key), secrets=secrets)
        result["cleanup"] = {"status": "passed"}
        result["status"] = "passed"
    except Exception as exc:
        result["error"] = _safe_error(exc, secrets)
        if backend is not None and remote_key is not None:
            try:
                backend.delete(remote_key)
                result["cleanup"] = {"status": "passed"}
            except Exception as cleanup_exc:
                result["cleanup"] = {"status": "failed",
                                      "detail": _safe_error(cleanup_exc, secrets),
                                      "key": "[probe artifact bị giữ lại — cần operator kiểm tra]"}
    return result
