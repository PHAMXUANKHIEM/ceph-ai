"""Application-consistency hooks for RBD backups.

The backup worker cannot infer a VM's filesystem or database topology from an
RBD image alone.  Operators therefore provide a small, non-secret pre/post
hook pair (for example a wrapper around QEMU guest-agent freeze/thaw or a
database quiesce script).  Hooks run on the worker host with a bounded
timeout, a fixed argument vector, and redacted output.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

VALID_MODES = {"crash-consistent", "application-consistent"}
DEFAULT_TIMEOUT_SECONDS = 30
MAX_OUTPUT = 4096
_SECRET_RE = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)(\s*[=:]\s*)\S+")


class ApplicationConsistencyError(RuntimeError):
    """A required application-consistency hook failed."""


@dataclass(frozen=True)
class ConsistencyPolicy:
    mode: str
    pre_hook: tuple[str, ...] = ()
    post_hook: tuple[str, ...] = ()
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


@dataclass
class ConsistencySession:
    policy: ConsistencyPolicy
    thawed: bool = False


def _redact_output(value: str) -> str:
    value = _SECRET_RE.sub(r"\1=***", value)
    return value[-MAX_OUTPUT:]


def _command(value, name: str) -> tuple[str, ...]:
    if value in (None, [], ""):
        return ()
    if not isinstance(value, list) or not value or len(value) > 32:
        raise ValueError(f"{name} phải là command dạng danh sách, tối đa 32 phần tử")
    result = tuple(str(item) for item in value)
    if any(not item or len(item) > 2048 for item in result):
        raise ValueError(f"{name} chứa phần tử rỗng hoặc quá dài")
    return result


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
    pre_hook = _command(hooks.get("pre_hook"), "application_consistency.pre_hook")
    post_hook = _command(hooks.get("post_hook"), "application_consistency.post_hook")
    if mode == "application-consistent" and (not pre_hook or not post_hook):
        raise ValueError("application-consistent cần cả pre_hook và post_hook")
    return ConsistencyPolicy(mode, pre_hook, post_hook, timeout)


def validate_image_policy(item: dict | None) -> None:
    normalize_policy(item)


def _run_hook(command: tuple[str, ...], phase: str, pool: str, image: str, timeout: int) -> None:
    env = os.environ.copy()
    env.update({"CEPH_AI_BACKUP_POOL": pool, "CEPH_AI_BACKUP_IMAGE": image, "CEPH_AI_BACKUP_HOOK_PHASE": phase})
    try:
        completed = subprocess.run(
            list(command), capture_output=True, text=True, timeout=timeout,
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
            _run_hook(policy.pre_hook, "pre-freeze", pool, image, policy.timeout_seconds)
        except ApplicationConsistencyError:
            # A freeze hook can partially succeed before returning an error;
            # always attempt the matching thaw before surfacing the failure.
            try:
                _run_hook(policy.post_hook, "post-thaw", pool, image, policy.timeout_seconds)
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
    _run_hook(session.policy.post_hook, "post-thaw", pool, image, session.policy.timeout_seconds)
