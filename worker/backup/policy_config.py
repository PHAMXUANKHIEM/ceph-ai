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
    lifecycle = normalized.get("alert_lifecycle") or {}
    if not isinstance(lifecycle, dict):
        raise BackupPolicyValidationError("alert_lifecycle phải là object")
    normalized["alert_lifecycle"] = {
        "cooldown_minutes": _bounded_int(
            lifecycle.get("cooldown_minutes", 5), "alert_lifecycle.cooldown_minutes", 1, 1440
        ),
        "reminder_hours": _bounded_int(
            lifecycle.get("reminder_hours", 1), "alert_lifecycle.reminder_hours", 1, 168
        ),
        "escalation_minutes": _bounded_int(
            lifecycle.get("escalation_minutes", 60), "alert_lifecycle.escalation_minutes", 5, 10080
        ),
    }
    retention = normalized.get("retention") or {}
    if not isinstance(retention, dict):
        raise BackupPolicyValidationError("retention phải là object")
    normalized["retention"] = {
        "keep_full_count": _bounded_int(retention.get("keep_full_count", 3), "keep_full_count", 1, 1000),
        "keep_incremental_count": _bounded_int(
            retention.get("keep_incremental_count", 7), "keep_incremental_count", 1, 1000
        ),
    }
    clusters = normalized.get("clusters") or {}
    if not isinstance(clusters, dict) or len(clusters) > 100:
        raise BackupPolicyValidationError("clusters phải là object tối đa 100 cụm")
    for cluster_id, scoped in clusters.items():
        try:
            from uuid import UUID
            UUID(str(cluster_id))
        except (TypeError, ValueError) as exc:
            raise BackupPolicyValidationError("clusters phải dùng Cluster.id UUID") from exc
        if not isinstance(scoped, dict) or not isinstance(scoped.get("schedule") or {}, dict):
            raise BackupPolicyValidationError("clusters.<id>.schedule phải là object")
        schedule = scoped.get("schedule") or {}
        for key in ("cron", "metadata_cron", "digest_cron", "restore_drill_cron"):
            if key in schedule and schedule[key] is not False and not isinstance(schedule[key], dict):
                raise BackupPolicyValidationError(f"clusters.<id>.schedule.{key} không hợp lệ")
        drill = scoped.get("restore_drill") or {}
        if not isinstance(drill, dict):
            raise BackupPolicyValidationError("clusters.<id>.restore_drill phải là object")
        if drill and not all(_NAME_RE.fullmatch(str(drill.get(name) or "")) for name in
                             ("pool", "image", "scratch_pool", "scratch_image")):
            raise BackupPolicyValidationError("clusters.<id>.restore_drill thiếu pool/image/scratch")
        if drill and drill["pool"] == drill["scratch_pool"] and drill["image"] == drill["scratch_image"]:
            raise BackupPolicyValidationError("RestoreDrill scratch phải khác image nguồn")
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


def rollback_backup_policy(revision_id: str, *, actor: str = "system") -> dict:
    """Restore one previously persisted policy revision atomically.

    Rollback is itself saved as a new revision, so the operator can undo an
    accidental rollback without editing YAML on disk.  The selected revision
    is validated before the live policy is replaced and the operation never
    accepts a path supplied by the caller.
    """
    if not _REVISION_RE.fullmatch(str(revision_id)):
        raise BackupPolicyValidationError("revision_id không hợp lệ")
    revision_path = Path(POLICY_REVISION_DIR) / f"{revision_id}.yaml"
    with _POLICY_LOCK:
        try:
            envelope = yaml.safe_load(revision_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise BackupPolicyValidationError("Không tìm thấy policy revision") from exc
        except (OSError, yaml.YAMLError) as exc:
            raise BackupPolicyValidationError("Không đọc được policy revision") from exc
        if envelope.get("revision_id") != revision_id:
            raise BackupPolicyValidationError("Policy revision không hợp lệ")
        restored = validate_backup_policy(envelope.get("policy"))
        saved = save_backup_policy(
            restored,
            actor=f"{str(actor)[:48]}:rollback:{revision_id[:8]}",
        )
        saved["rolled_back_from"] = revision_id
        return saved


def backup_targets_from_policy() -> list[dict]:
    """Shared by `engine.py` (RBD backup/retention) and `metadata.py`
    (Story 9.3) — lives here rather than in either module so neither has
    to import the other just for this."""
    return load_backup_policy().get("backup_targets") or []


def cluster_schedule_policy(policy: dict, cluster_id: str | None) -> dict:
    """A secondary cluster never inherits the default drill destination."""
    if cluster_id is None:
        return policy
    scoped = (policy.get("clusters") or {}).get(cluster_id)
    return scoped if isinstance(scoped, dict) else {}
