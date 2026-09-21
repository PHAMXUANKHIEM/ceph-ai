"""Shared loader for `worker/policy/backup_policy.yaml` — used by both
`scheduler.py` (schedule/tracked_images) and `engine.py` (retention/
tracked_images), same "one file, one loader" posture as `worker/policy/
gate.py` for `action_policy.yaml`.
"""

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

import yaml

from worker.backup import application_consistency

POLICY_PATH = os.path.join(os.path.dirname(__file__), "..", "policy", "backup_policy.yaml")
POLICY_REVISION_DIR = os.environ.get(
    "CEPH_AI_BACKUP_POLICY_REVISION_DIR", "/var/lib/ceph-ai/backup-policy-revisions"
)
_POLICY_LOCK = RLock()
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{32}$")
_SECRET_KEY_RE = re.compile(r"secret|password|token|private.?key|access.?key", re.I)


class BackupPolicyValidationError(ValueError):
    """Raised when an operator policy is unsafe or structurally invalid."""


def _bounded_int(value, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise BackupPolicyValidationError(f"{name} phải là số nguyên")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise BackupPolicyValidationError(f"{name} phải là số nguyên") from exc
    if not minimum <= number <= maximum:
        raise BackupPolicyValidationError(f"{name} phải nằm trong {minimum}..{maximum}")
    return number


def _assert_no_secret_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                raise BackupPolicyValidationError("Policy không được chứa credential/secret")
            _assert_no_secret_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_secret_keys(nested)


def validate_backup_policy(policy: dict) -> dict:
    """Validate and normalize the non-secret backup policy document."""
    if not isinstance(policy, dict):
        raise BackupPolicyValidationError("Policy phải là object")
    _assert_no_secret_keys(policy)
    normalized = dict(policy)
    targets = normalized.get("backup_targets") or []
    if not isinstance(targets, list) or not 1 <= len(targets) <= 2:
        raise BackupPolicyValidationError("Cần từ 1 đến 2 backup target")
    slots = []
    clean_targets = []
    for target in targets:
        if not isinstance(target, dict) or target.get("slot") not in {"a", "b"}:
            raise BackupPolicyValidationError("Backup target phải có slot a hoặc b")
        slot = str(target["slot"])
        if slot in slots:
            raise BackupPolicyValidationError("Backup target bị trùng slot")
        slots.append(slot)
        clean_targets.append({"slot": slot, "immutable": bool(target.get("immutable", False))})
    normalized["backup_targets"] = clean_targets
    normalized["required_copy_count"] = _bounded_int(
        normalized.get("required_copy_count", 1), "required_copy_count", 1, len(clean_targets)
    )
    tracked = normalized.get("tracked_images") or []
    if not isinstance(tracked, list) or len(tracked) > 500:
        raise BackupPolicyValidationError("tracked_images tối đa 500 image")
    clean_tracked = []
    seen = set()
    for item in tracked:
        if not isinstance(item, dict):
            raise BackupPolicyValidationError("tracked_images phải là danh sách object")
        pool, image = str(item.get("pool", "")).strip(), str(item.get("image", "")).strip()
        if not _NAME_RE.fullmatch(pool) or not _NAME_RE.fullmatch(image):
            raise BackupPolicyValidationError("pool/image không hợp lệ")
        identity = (pool, image)
        if identity in seen:
            raise BackupPolicyValidationError(f"tracked image bị trùng: {pool}/{image}")
        seen.add(identity)
        entry = {"pool": pool, "image": image}
        if item.get("full_refresh_every_n_days") is not None:
            entry["full_refresh_every_n_days"] = _bounded_int(
                item["full_refresh_every_n_days"], "full_refresh_every_n_days", 1, 3650
            )
        if item.get("rpo_hours") is not None:
            entry["rpo_hours"] = _bounded_int(item["rpo_hours"], "rpo_hours", 1, 8760)
        if item.get("required_copy_count") is not None:
            entry["required_copy_count"] = _bounded_int(
                item["required_copy_count"], "required_copy_count", 1, len(clean_targets)
            )
        try:
            consistency = application_consistency.normalize_policy(item)
        except (TypeError, ValueError) as exc:
            raise BackupPolicyValidationError(str(exc)) from exc
        entry["consistency_mode"] = consistency.mode
        if consistency.mode == "application-consistent":
            entry["application_consistency"] = {
                "pre_hook": list(consistency.pre_hook),
                "post_hook": list(consistency.post_hook),
                "timeout_seconds": consistency.timeout_seconds,
            }
        clean_tracked.append(entry)
    normalized["tracked_images"] = clean_tracked
    normalized["rpo_hours"] = _bounded_int(normalized.get("rpo_hours", 24), "rpo_hours", 1, 8760)
    normalized["metadata_rpo_hours"] = _bounded_int(
        normalized.get("metadata_rpo_hours", 12), "metadata_rpo_hours", 1, 8760
    )
    normalized["restore_drill_rpo_hours"] = _bounded_int(
        normalized.get("restore_drill_rpo_hours", 192), "restore_drill_rpo_hours", 1, 8760
    )
    retention = normalized.get("retention") or {}
    if not isinstance(retention, dict):
        raise BackupPolicyValidationError("retention phải là object")
    normalized["retention"] = {
        "keep_full_count": _bounded_int(retention.get("keep_full_count", 3), "keep_full_count", 1, 1000),
        "keep_incremental_count": _bounded_int(
            retention.get("keep_incremental_count", 7), "keep_incremental_count", 1, 1000
        ),
    }
    return normalized


def load_backup_policy() -> dict:
    with open(POLICY_PATH) as f:
        return validate_backup_policy(yaml.safe_load(f) or {})


def _write_yaml_atomic(policy: dict) -> None:
    destination = Path(POLICY_PATH).resolve()
    destination.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(policy, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save_backup_policy(policy: dict, *, actor: str = "system") -> dict:
    """Atomically save a validated policy and preserve the previous revision."""
    normalized = validate_backup_policy(policy)
    revision_id = uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    revision_dir = Path(POLICY_REVISION_DIR)
    with _POLICY_LOCK:
        previous = None
        try:
            previous = load_backup_policy()
        except (OSError, yaml.YAMLError, BackupPolicyValidationError):
            previous = None
        revision_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        if previous is not None:
            revision_path = revision_dir / f"{revision_id}.yaml"
            revision_path.write_text(
                yaml.safe_dump({"revision_id": revision_id, "created_at": created_at,
                                "actor": str(actor)[:64], "policy": previous},
                               allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            revision_path.chmod(0o640)
        _write_yaml_atomic(normalized)
    return {"revision_id": revision_id, "created_at": created_at, "policy": normalized}


def list_policy_revisions(limit: int = 20) -> list[dict]:
    revision_dir = Path(POLICY_REVISION_DIR)
    rows = []
    try:
        paths = sorted(revision_dir.glob("*.yaml"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return rows
    for path in paths[: max(1, min(int(limit), 100))]:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if _REVISION_RE.fullmatch(str(data.get("revision_id", ""))):
                rows.append({"revision_id": data["revision_id"], "created_at": data.get("created_at"), "actor": data.get("actor", "system")})
        except (OSError, yaml.YAMLError, TypeError):
            continue
    return rows


def backup_targets_from_policy() -> list[dict]:
    """Shared by `engine.py` (RBD backup/retention) and `metadata.py`
    (Story 9.3) — lives here rather than in either module so neither has
    to import the other just for this."""
    return load_backup_policy().get("backup_targets") or []
