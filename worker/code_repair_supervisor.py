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
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

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
NIGHTLY_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
NIGHTLY_IMPROVEMENT_EVIDENCE = (
    "Scheduled nightly review: Cần nâng cấp gì cho phần AI của tool này?"
)
NIGHTLY_REGRESSION_TEST_COMMAND = (
    "PYTHONPATH=. .venv/bin/pytest -q "
    "tests/test_code_repair.py "
    "tests/test_code_repair_supervisor.py"
)
NIGHTLY_AI_STEP_TIMEOUT_SECONDS = 1200
NIGHTLY_MAX_REVIEW_ROUNDS = 2
NIGHTLY_IMPROVEMENT_INSTRUCTIONS = """This is a proactive nightly AI improvement task, not an incident repair.

Review only the ceph-ai AI product surface: provider routing, Codex/Claude integration, Chat-with-AI,
two-agent workflows, rate-limit/budget safeguards, AI observability, learning, and regression tests.
Identify at most ONE smallest useful, testable improvement. Do not modify credentials, OAuth/account handling,
.env, Telegram configuration, deployment scripts, database migrations, Ceph commands, or safety policy.
Never create a cosmetic-only change. If no bounded improvement is justified, finish the plan with exactly:
VERDICT: NO_CHANGE_NEEDED
Otherwise give the Implementer an exact, low-risk plan and tests. The Implementer must keep the same scope
and add or update at least one regression test under tests/ in the candidate diff.
"""


def _configured_account_profile(source: str, profile: str) -> str:
    """Map Settings' source/profile pair to the pipeline's safe profile value."""
    return profile.strip() if source == "separate" else "configured"


@contextmanager
def _repair_run_lock():
    """Hold the cross-process lock for one complete repair pipeline."""
    lock_path = Path(settings.code_repair_run_lock_file)
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def run_repair_exclusively(
    evidence: str, config: RepairConfig, *, force: bool = False,
):
    """Run exactly one repair pipeline at a time across supervisor and timer jobs."""
    with _repair_run_lock():
        if force:
            return run_repair(evidence, config, force=True)
        return run_repair(evidence, config)


def _load_nightly_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _save_nightly_state(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _nightly_due(state: dict, now: datetime) -> bool:
    """Run once per day, except a process killed mid-run is retried."""
    local = now.astimezone(NIGHTLY_TIMEZONE)
    if state.get("last_run_date") != local.date().isoformat():
        return True
    # A RUNNING state only remains after an abnormal process death: a live
    # pipeline still owns _repair_run_lock, and normal completion writes a
    # terminal status.  FAILED is intentionally retried by systemd.
    return state.get("status") in {"RUNNING", "FAILED"}


def _dirty_checkout(repo: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return "không thể kiểm tra Git checkout"
    return "\n".join(result.stdout.splitlines()[:12])


def run_nightly_ai_improvement(repo: Path, state_path: Path, *, now: datetime | None = None) -> bool:
    """Run one bounded proactive two-agent AI review and persist its outcome."""
    try:
        # Wait for an active repair before deciding this day's state. A killed
        # timer process can therefore never consume the daily run merely while
        # it is blocked behind the supervisor.
        with _repair_run_lock():
            return _run_nightly_ai_improvement_locked(repo, state_path, now=now)
    except Exception as exc:
        current = now or datetime.now(timezone.utc)
        state = _load_nightly_state(state_path)
        # A runtime failure is retryable.  Do not consume this calendar day;
        # the systemd failure exit will invoke this job again after backoff.
        state.pop("last_run_date", None)
        state.update({
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "FAILED",
            "error": str(exc),
        })
        try:
            _save_nightly_state(state_path, state)
        except OSError:
            logger.exception("could not persist failed nightly AI improvement state")
        logger.exception("nightly AI improvement failed")
        try:
            send_code_repair_alert(
                "⚠️ AI NIGHTLY IMPROVEMENT KHÔNG TRIỂN KHAI\n"
                f"Lỗi runtime: {str(exc)[:900]}"
            )
        except Exception:
            logger.exception("could not send nightly AI improvement failure alert")
        return False


def _run_nightly_ai_improvement_locked(
    repo: Path, state_path: Path, *, now: datetime | None = None,
) -> bool:
    """Run the nightly pipeline while the cross-process repair lock is held."""
    current = now or datetime.now(timezone.utc)
    state = _load_nightly_state(state_path)
    if not _nightly_due(state, current):
        return False

    local = current.astimezone(NIGHTLY_TIMEZONE)
    dirty_checkout = _dirty_checkout(repo)
    if dirty_checkout:
        state.update({
            "last_run_date": local.date().isoformat(),
            "finished_at": current.isoformat(),
            "status": "BLOCKED_DIRTY_CHECKOUT",
            "error": dirty_checkout,
        })
        _save_nightly_state(state_path, state)
        send_code_repair_alert(
            "⚠️ AI NIGHTLY IMPROVEMENT CHƯA CHẠY\n"
            "Checkout ceph-ai đang có thay đổi chưa commit nên job dừng trước khi gọi AI.\n"
            f"Files: {dirty_checkout[:900]}"
        )
        return True

    # The state records an in-progress run before invoking AI.  If this
    # process is killed, _nightly_due will retry only after it can reacquire
    # the process lock, so no two pipelines run concurrently.
    state.update({
        "last_run_date": local.date().isoformat(),
        "started_at": current.isoformat(),
        "status": "RUNNING",
    })
    _save_nightly_state(state_path, state)
    send_code_repair_alert(
        "🌙 AI NIGHTLY IMPROVEMENT BẮT ĐẦU\n"
        "Hai AI đang rà soát: ‘Cần nâng cấp gì cho phần AI của tool này?’\n"
        "Phạm vi: AI/chat/router/giới hạn/quan sát/học; chỉ worktree + test, không đụng tài khoản hay cấu hình bí mật."
    )
    repair_state = state_path.with_name("nightly-ai-improvement-repairs.json")
    result = run_repair(
        NIGHTLY_IMPROVEMENT_EVIDENCE,
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
            max_review_rounds=min(settings.code_repair_max_review_rounds, NIGHTLY_MAX_REVIEW_ROUNDS),
            test_command=NIGHTLY_REGRESSION_TEST_COMMAND,
            candidate_test_command=NIGHTLY_REGRESSION_TEST_COMMAND,
            require_changed_tests=True,
            timeout_seconds=min(settings.code_repair_timeout_seconds, NIGHTLY_AI_STEP_TIMEOUT_SECONDS),
            push=settings.code_repair_push,
            deploy_staging=settings.code_repair_deploy_staging,
            promote_main=settings.code_repair_promote_main,
            state_file=repair_state,
            task_kind="nightly-ai-improvement",
            task_instructions=NIGHTLY_IMPROVEMENT_INSTRUCTIONS,
            max_ai_attempts=1,
            max_pipeline_attempts=1,
            running_stale_seconds=settings.code_repair_running_stale_seconds,
            notify_telegram=False,
            allow_no_change=True,
        ),
        force=True,
    )
    state.update({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": result.status,
        "branch": result.branch,
        "commit": result.commit,
        "changed_files": result.changed_files or [],
        "error": result.error,
    })
    _save_nightly_state(state_path, state)
    if result.status == "NO_CHANGE":
        message = "🌙 AI NIGHTLY IMPROVEMENT\nKết quả: chưa có nâng cấp AI nào đủ nhỏ và an toàn để triển khai hôm nay."
    elif result.status in {"PUSHED", "STAGING_VERIFIED", "PROMOTED", "COMMITTED"}:
        files = ", ".join(result.changed_files or []) or "—"
        message = (
            "✅ AI NIGHTLY IMPROVEMENT HOÀN TẤT\n"
            f"Kết quả: {result.status}\nBranch: {result.branch or '—'}\n"
            f"Files: {files}\nReview rounds: {result.review_rounds}"
        )
    else:
        message = (
            "⚠️ AI NIGHTLY IMPROVEMENT KHÔNG TRIỂN KHAI\n"
            f"Kết quả: {result.status}\nLý do: {(result.error or 'không rõ')[:900]}"
        )
    send_code_repair_alert(message)
    logger.info("nightly AI improvement completed: %s (%s)", result.status, result.fingerprint)
    # Let systemd retry only real pipeline failures.  A clean NO_CHANGE or
    # an intentionally blocked dirty checkout remains one completed run.
    if result.status == "FAILED":
        state.pop("last_run_date", None)
        _save_nightly_state(state_path, state)
        return False
    return True


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
            result = run_repair_exclusively(
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
                result = run_repair_exclusively(
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
