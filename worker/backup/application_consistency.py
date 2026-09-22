"""Application-consistency hooks for RBD backups.

The backup worker cannot infer a VM's filesystem or database topology from an
RBD image alone.  Operators therefore provide a small, non-secret pre/post
hook pair (for example a wrapper around QEMU guest-agent freeze/thaw or a
database quiesce script).  Hooks run on the worker host with a bounded
timeout, a fixed argument vector, and redacted output.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

VALID_MODES = {"crash-consistent", "application-consistent"}
DEFAULT_TIMEOUT_SECONDS = 30
MAX_OUTPUT = 4096
HOOK_MANIFEST_PATH = os.environ.get(
    "CEPH_AI_BACKUP_HOOK_MANIFEST", "/etc/ceph-ai/backup-hooks.json"
)
ALLOWED_HOOK_IDS = frozenset({"qemu_guest_agent"})
_SHELL_EXECUTABLES = frozenset({
    "/bin/sh", "/bin/bash", "/bin/dash", "/bin/zsh", "/usr/bin/sh",
    "/usr/bin/bash", "/usr/bin/dash", "/usr/bin/zsh",
})
_SECRET_RE = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)(\s*[=:]\s*)\S+")


class ApplicationConsistencyError(RuntimeError):
    """A required application-consistency hook failed."""


@dataclass(frozen=True)
class ConsistencyPolicy:
    mode: str
    pre_hook_id: str = ""
    post_hook_id: str = ""
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


@dataclass
class ConsistencySession:
    policy: ConsistencyPolicy
    thawed: bool = False


def _redact_output(value: str) -> str:
    value = _SECRET_RE.sub(r"\1=***", value)
    return value[-MAX_OUTPUT:]


@dataclass(frozen=True)
class HookSpec:
    command: tuple[str, ...]
    sha256: str


def _trusted_file(path: Path, *, executable: bool) -> bool:
    """Require a root-owned, non-writable, regular hook/manifest file."""
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError:
        return False
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        return False
    if info.st_uid != 0 or info.st_mode & 0o022:
        return False
    return not executable or bool(info.st_mode & 0o111)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_hook_manifest() -> dict[str, dict[str, HookSpec]]:
    manifest = Path(HOOK_MANIFEST_PATH)
    if not _trusted_file(manifest, executable=False):
        raise ValueError("backup hook manifest phải là file root-owned, không writable")
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("backup hook manifest không hợp lệ") from exc
    hooks = raw.get("hooks") if isinstance(raw, dict) else None
    if not isinstance(hooks, dict):
        raise ValueError("backup hook manifest thiếu hooks")
    result: dict[str, dict[str, HookSpec]] = {}
    for hook_id, phases in hooks.items():
        if hook_id not in ALLOWED_HOOK_IDS or not isinstance(phases, dict):
            continue
        normalized: dict[str, HookSpec] = {}
        for phase in ("pre", "post"):
            item = phases.get(phase)
            if not isinstance(item, dict):
                raise ValueError(f"hook {hook_id} thiếu phase {phase}")
            command = item.get("command")
            sha256 = str(item.get("sha256") or "").lower()
            if not isinstance(command, list) or not command or len(command) > 16:
                raise ValueError(f"hook {hook_id}/{phase} có command không hợp lệ")
            command_tuple = tuple(str(value) for value in command)
            executable = Path(command_tuple[0])
            if not executable.is_absolute() or command_tuple[0] in _SHELL_EXECUTABLES:
                raise ValueError(f"hook {hook_id}/{phase} không được dùng shell interpreter")
            if any(value in {"-c", "--command"} for value in command_tuple[1:]):
                raise ValueError(f"hook {hook_id}/{phase} không được dùng shell command flag")
            if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ValueError(f"hook {hook_id}/{phase} thiếu SHA-256")
            if not _trusted_file(executable, executable=True):
                raise ValueError(f"hook {hook_id}/{phase} phải là executable root-owned")
            if _digest(executable) != sha256:
                raise ValueError(f"hook {hook_id}/{phase} checksum không khớp")
            normalized[phase] = HookSpec(command_tuple, sha256)
        result[hook_id] = normalized
    return result


def _hook_id(value, name: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or value not in ALLOWED_HOOK_IDS:
        raise ValueError(f"{name} phải là hook_id thuộc allowlist")
    return value


def normalize_policy(item: dict | None) -> ConsistencyPolicy:
    item = item or {}
    mode = str(item.get("consistency_mode", "crash-consistent")).strip().lower()
    if mode not in VALID_MODES:
        raise ValueError("consistency_mode phải là crash-consistent hoặc application-consistent")
    hooks = item.get("application_consistency") or {}
    if not isinstance(hooks, dict):
        raise ValueError("application_consistency phải là object")
    timeout = int(hooks.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    if not 1 <= timeout <= 300:
        raise ValueError("application_consistency.timeout_seconds phải nằm trong 1..300")
    if "pre_hook" in hooks or "post_hook" in hooks:
        raise ValueError("application-consistent chỉ nhận pre_hook_id và post_hook_id")
    pre_hook_id = _hook_id(hooks.get("pre_hook_id"), "application_consistency.pre_hook_id")
    post_hook_id = _hook_id(hooks.get("post_hook_id"), "application_consistency.post_hook_id")
    if mode == "application-consistent":
        if not pre_hook_id or not post_hook_id:
            raise ValueError("cần cả pre_hook_id và post_hook_id")
        manifest = _load_hook_manifest()
        if pre_hook_id not in manifest or post_hook_id not in manifest:
            raise ValueError("application-consistent hook_id chưa có trong manifest")
    return ConsistencyPolicy(mode, pre_hook_id, post_hook_id, timeout)


def validate_image_policy(item: dict | None) -> None:
    normalize_policy(item)


def _run_hook(hook_id: str, phase: str, pool: str, image: str, timeout: int) -> None:
    manifest = _load_hook_manifest()
    specs = manifest.get(hook_id)
    if specs is None:
        raise ApplicationConsistencyError(f"hook_id không được allowlist: {hook_id}")
    spec = specs["pre" if phase == "pre-freeze" else "post"]
    command = [
        value.format(pool=pool, image=image, phase=phase)
        for value in spec.command
    ]
    env = os.environ.copy()
    env.update({"CEPH_AI_BACKUP_POOL": pool, "CEPH_AI_BACKUP_IMAGE": image, "CEPH_AI_BACKUP_HOOK_PHASE": phase})
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            check=False, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ApplicationConsistencyError(f"{phase} hook timeout sau {timeout}s") from exc
    except OSError as exc:
        raise ApplicationConsistencyError(f"{phase} hook không chạy được: {exc}") from exc
    output = _redact_output("\n".join(part for part in (completed.stdout, completed.stderr) if part))
    if completed.returncode != 0:
        detail = f": {output}" if output else ""
        raise ApplicationConsistencyError(f"{phase} hook thất bại (exit {completed.returncode}){detail}")
    if output:
        logger.info("backup %s hook output: %s", phase, output)


def begin(policy: ConsistencyPolicy, pool: str, image: str) -> ConsistencySession:
    session = ConsistencySession(policy)
    if policy.mode == "application-consistent":
        try:
            _run_hook(policy.pre_hook_id, "pre-freeze", pool, image, policy.timeout_seconds)
        except ApplicationConsistencyError:
            # A freeze hook can partially succeed before returning an error;
            # always attempt the matching thaw before surfacing the failure.
            try:
                _run_hook(policy.post_hook_id, "post-thaw", pool, image, policy.timeout_seconds)
            except ApplicationConsistencyError as thaw_exc:
                logger.error("application consistency: thaw after failed freeze failed: %s", thaw_exc)
            session.thawed = True
            raise
    return session


def thaw(session: ConsistencySession, pool: str, image: str) -> None:
    if session.thawed:
        return
    session.thawed = True
    if session.policy.mode != "application-consistent":
        return
    _run_hook(session.policy.post_hook_id, "post-thaw", pool, image, session.policy.timeout_seconds)
