"""Continuously trigger bounded Code Repair from newly appended app logs.

This process deliberately lives outside Watcher/Worker. Candidate deployments
restart those services, while this supervisor must survive long enough to
evaluate the deployment and promote or roll it back.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
import subprocess
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

from config.settings import settings
from worker.code_repair import (
    ERROR_RE,
    SECRET_RE,
    RepairConfig,
    cleanup_stale_worktrees,
    clean_evidence,
    reconcile_stale_attempts_file,
    run_repair,
)
from worker import ceph_capability_learning as ceph_learning
from shared.telegram_alerts import send_code_repair_alert

logger = logging.getLogger(__name__)
REPAIR_COOLDOWN_SECONDS = 3600


def _configured_account_profile(source: str, profile: str) -> str:
    """Map Settings' source/profile pair to the pipeline's safe profile value."""
    return profile.strip() if source == "separate" else "configured"


@dataclass
class Cursor:
    inode: int
    offset: int


def _load_cursors(path: Path) -> dict[str, Cursor]:
    try:
        raw = json.loads(path.read_text())
        return {name: Cursor(int(value["inode"]), int(value["offset"])) for name, value in raw.items()}
    except (FileNotFoundError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {}


def _save_cursors(path: Path, cursors: dict[str, Cursor]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({name: vars(cursor) for name, cursor in cursors.items()}, indent=2))
    os.replace(temporary, path)


def read_new_errors(paths: list[Path], cursors: dict[str, Cursor], *, initialize_at_end: bool) -> list[str]:
    """Read only appended bytes, skipping historical errors on first startup."""
    max_tail_bytes = 250_000
    evidence: list[str] = []
    for path in paths:
        if "code-repair" in path.name or not path.is_file():
            continue
        stat = path.stat()
        key = str(path)
        cursor = cursors.get(key)
        if cursor is None:
            offset = stat.st_size if initialize_at_end else 0
        elif cursor.inode != stat.st_ino or stat.st_size < cursor.offset:
            offset = 0  # rotation/truncation: the replacement file is new data
        else:
            offset = cursor.offset
        # Busy watcher logs can grow faster than one supervisor poll. Reading
        # the *first* 250 KB after an old cursor made the supervisor replay a
        # historical traceback for hours while never catching up. Keep only
        # the freshest bounded tail and advance to EOF; Code Repair is for a
        # current application failure, not archival log processing.
        offset = max(offset, stat.st_size - max_tail_bytes)
        with path.open("r", errors="replace") as handle:
            handle.seek(offset)
            appended = handle.read()
            cursors[key] = Cursor(stat.st_ino, handle.tell())
        matches = list(ERROR_RE.finditer(appended))
        if matches:
            block = appended[max(0, matches[-1].start() - 1200):matches[-1].start() + 16_000]
            block = SECRET_RE.sub(lambda match: match.group(1) + "=<redacted>", block)
            block = clean_evidence(block)
            if block:
                evidence.append(f"Source application log: {path.name}\n{block}")
    return evidence


def run_forever(*, max_iterations: int | None = None) -> None:
    repo = Path(__file__).resolve().parents[1]
    cursor_file = Path(settings.code_repair_cursor_file)
    cursors = _load_cursors(cursor_file)
    first_scan = not cursor_file.exists()
    learning_state_file = Path(settings.ceph_capability_learning_state_file)
    learning_state = ceph_learning.load_state(learning_state_file)
    if settings.ceph_capability_learning_enabled and not learning_state.get("initialized"):
        if not settings.ceph_capability_learning_include_existing:
            for dedupe_key in ceph_learning.eligible_keys():
                learning_state.setdefault("findings", {})[dedupe_key] = {"status": "BASELINED"}
        learning_state["initialized"] = True
        ceph_learning.save_state(learning_state_file, learning_state)
    iterations = 0
    # A restart/deploy must not immediately replay a queued learning job and
    # surprise operators with another repair notification. Tests that request
    # a bounded run still start immediately.
    last_repair_at: float | None = time.monotonic() if max_iterations is None else None
    while settings.code_repair_auto_enabled:
        state_file = RepairConfig(repo=repo).state_file
        stale_branches = reconcile_stale_attempts_file(
            state_file,
            stale_seconds=settings.code_repair_running_stale_seconds,
        )
        if stale_branches:
            removed = cleanup_stale_worktrees(repo, stale_branches)
            logger.warning(
                "reconciled %d stale Code Repair attempt(s); removed %d orphan worktree(s)",
                len(stale_branches), len(removed),
            )
        candidate = None
        if settings.ceph_capability_learning_enabled:
            base_revision = subprocess.run(
                ["git", "rev-parse", "origin/main"], cwd=repo, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            ).stdout.strip()
            # A crashed process must not leave a finding RUNNING forever.
            now = datetime.now(timezone.utc)
            for value in learning_state.setdefault("findings", {}).values():
                if value.get("status") != "RUNNING":
                    continue
                try:
                    age = (now - datetime.fromisoformat(value["updated_at"])).total_seconds()
                except (KeyError, TypeError, ValueError):
                    age = settings.code_repair_running_stale_seconds + 1
                if age > settings.code_repair_running_stale_seconds:
                    value["status"] = "FAILED_STALE"
            seen = ceph_learning.blocked_keys(
                learning_state, base_revision,
                max_attempts=settings.code_repair_max_attempts,
            )
            candidate = ceph_learning.next_candidate(seen)
        cooldown_elapsed = (
            last_repair_at is None
            or time.monotonic() - last_repair_at >= REPAIR_COOLDOWN_SECONDS
        )
        if candidate is not None and not cooldown_elapsed:
            logger.info("Ceph capability learning deferred during repair cooldown")
            candidate = None
        if candidate is not None:
            if not candidate.verification.eligible_for_learning:
                status = f"VERIFIED_NO_CODE_CHANGE:{candidate.verification.code}"
                ceph_learning.mark(
                    learning_state, candidate, status, base_revision=base_revision,
                )
                ceph_learning.save_state(learning_state_file, learning_state)
                facts = "\n".join(f"• {fact}" for fact in candidate.verification.live_facts)
                send_code_repair_alert(
                    "🔎 CEPH LIVE VERIFICATION\n"
                    f"Kết luận: {candidate.verification.summary}\n"
                    f"Mã: {candidate.verification.code}\n"
                    f"{facts}\n"
                    "Không sửa source ceph-ai từ finding này."
                )
                logger.info(
                    "Ceph finding verified without learning: %s finding=%s",
                    candidate.verification.code, candidate.finding_id,
                )
                candidate = None
        if candidate is not None:
            last_repair_at = time.monotonic()
            ceph_learning.mark(
                learning_state, candidate, "RUNNING", base_revision=base_revision,
                increment_attempt=True,
            )
            ceph_learning.save_state(learning_state_file, learning_state)
            result = run_repair(
                candidate.evidence,
                RepairConfig(
                    repo=repo,
                    provider=settings.code_repair_provider,
                    planner_provider=settings.code_repair_planner_provider,
                    planner_model=settings.code_repair_planner_model,
                    planner_account_profile=_configured_account_profile(
                        settings.code_repair_planner_account_source,
                        settings.code_repair_planner_account_profile,
                    ),
                    implementer_provider=settings.code_repair_implementer_provider,
                    implementer_model=settings.code_repair_implementer_model,
                    implementer_account_profile=_configured_account_profile(
                        settings.code_repair_implementer_account_source,
                        settings.code_repair_implementer_account_profile,
                    ),
                    test_command=settings.code_repair_test_command,
                    timeout_seconds=settings.code_repair_timeout_seconds,
                    push=settings.code_repair_push,
                    deploy_staging=settings.code_repair_deploy_staging,
                    promote_main=settings.code_repair_promote_main,
                    task_kind="ceph-capability-learning",
                    task_instructions=ceph_learning.LEARNING_INSTRUCTIONS,
                    max_ai_attempts=settings.code_repair_max_attempts,
                    max_pipeline_attempts=settings.code_repair_max_attempts,
                    running_stale_seconds=settings.code_repair_running_stale_seconds,
                ),
            )
            learned_status = "LEARNED" if result.status in {
                "PUSHED", "STAGING_VERIFIED", "PROMOTED",
            } else result.status
            ceph_learning.mark(
                learning_state, candidate, learned_status, base_revision=base_revision,
            )
            ceph_learning.save_state(learning_state_file, learning_state)
            logger.info(
                "Ceph capability learning completed: %s finding=%s fingerprint=%s",
                result.status, candidate.finding_id, result.fingerprint,
            )
        else:
            paths = sorted(Path("/var/log").glob("ceph-ai-*.log"))
            errors = read_new_errors(paths, cursors, initialize_at_end=first_scan)
            first_scan = False
            _save_cursors(cursor_file, cursors)
            if errors and cooldown_elapsed:
                # Set before invoking the pipeline: FAILED is still an
                # attempt and must not recursively trigger dozens of new
                # repairs from logs emitted by its own staging smoke test.
                last_repair_at = time.monotonic()
                result = run_repair(
                    max(errors, key=len),
                    RepairConfig(
                        repo=repo,
                        provider=settings.code_repair_provider,
                        planner_provider=settings.code_repair_planner_provider,
                        planner_model=settings.code_repair_planner_model,
                        planner_account_profile=_configured_account_profile(
                            settings.code_repair_planner_account_source,
                            settings.code_repair_planner_account_profile,
                        ),
                        implementer_provider=settings.code_repair_implementer_provider,
                        implementer_model=settings.code_repair_implementer_model,
                        implementer_account_profile=_configured_account_profile(
                            settings.code_repair_implementer_account_source,
                            settings.code_repair_implementer_account_profile,
                        ),
                        test_command=settings.code_repair_test_command,
                        timeout_seconds=settings.code_repair_timeout_seconds,
                        push=settings.code_repair_push,
                        deploy_staging=settings.code_repair_deploy_staging,
                        promote_main=settings.code_repair_promote_main,
                        max_ai_attempts=settings.code_repair_max_attempts,
                        max_pipeline_attempts=settings.code_repair_max_attempts,
                        running_stale_seconds=settings.code_repair_running_stale_seconds,
                    ),
                )
                logger.info("automatic Code Repair completed: %s (%s)", result.status, result.fingerprint)
            elif errors:
                logger.warning(
                    "automatic Code Repair suppressed %d error block(s) during %ss cooldown",
                    len(errors), REPAIR_COOLDOWN_SECONDS,
                )
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return
        time.sleep(max(5, settings.code_repair_poll_interval_seconds))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    if not settings.code_repair_auto_enabled:
        logger.info("automatic Code Repair is disabled")
        return 0
    lock_path = Path(settings.code_repair_lock_file)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("another Code Repair supervisor already holds the lock")
            return 0
        run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
