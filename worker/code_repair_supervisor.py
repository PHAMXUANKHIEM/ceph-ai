"""Continuously trigger bounded Code Repair from newly appended app logs.

This process deliberately lives outside Watcher/Worker. Candidate deployments
restart those services, while this supervisor must survive long enough to
evaluate the deployment and promote or roll it back.
"""

from __future__ import annotations

import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import os
import re
import shutil
import tempfile
import time
import subprocess
import threading
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
    cleanup_preserved_candidates,
    reconcile_stale_attempts_file,
    run_repair,
    _provider_command,
    _role_account_dirs,
    _ai_process_environment,
    _run,
)
from worker import ceph_capability_learning as ceph_learning
from shared.ai_budget import AIBudgetError, check as check_ai_budget
from shared.ai_observability import record_ai_attempt
from shared import service_health
from shared.telegram_alerts import send_code_repair_alert

logger = logging.getLogger(__name__)
REPAIR_COOLDOWN_SECONDS = 3600
NIGHTLY_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
NIGHTLY_IMPROVEMENT_EVIDENCE = (
    "Scheduled nightly review: Cần nâng cấp gì cho phần AI của tool này?"
)
NIGHTLY_TEST_ENV_UNSET = (
    "AI_NIGHTLY_MULTI_AGENT_ANALYSIS_ENABLED",
    "AI_NIGHTLY_MULTI_AGENT_MAX_PARALLEL",
    "AI_NIGHTLY_MULTI_AGENT_TIMEOUT_SECONDS",
)
NIGHTLY_REGRESSION_TEST_COMMAND = (
    "env -u AI_NIGHTLY_MULTI_AGENT_ANALYSIS_ENABLED "
    "-u AI_NIGHTLY_MULTI_AGENT_MAX_PARALLEL "
    "-u AI_NIGHTLY_MULTI_AGENT_TIMEOUT_SECONDS "
    "CEPH_AI_ENV_FILE=/dev/null "
    "PYTHONPATH=. .venv/bin/python -m pytest -q "
    "tests/test_code_repair.py "
    "tests/test_code_repair_supervisor.py "
    "--deselect=tests/test_code_repair_supervisor.py::test_nightly_improvement_runs_once_and_uses_test_deploy_pipeline "
    "--deselect=tests/test_code_repair_supervisor.py::test_nightly_multi_agent_reports_are_passed_to_single_writer "
    "-k 'not (nightly_improvement or nightly_multi_agent or direct_nightly_call or nightly_failure "
    "or nightly_failed_pipeline or nightly_dirty_checkout or nightly_dashboard_override)'"
)
NIGHTLY_AI_STEP_TIMEOUT_SECONDS = 1200
NIGHTLY_MAX_REVIEW_ROUNDS = 2
NIGHTLY_CANDIDATE_MAX_COUNT = 7
NIGHTLY_CANDIDATE_RETENTION_SECONDS = 14 * 86400
NIGHTLY_ANALYSIS_REPORT_LIMIT = 4_500
NIGHTLY_ANALYSIS_TOTAL_LIMIT = 16_000
_NIGHTLY_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:api[_-]?key|secret|password|token|authorization|private[_-]?key)"
    r"[\"']?\s*[:=]\s*)(?P<quote>[\"']?)(?P<value>[^\"'\s,}]+)(?P=quote)"
)
NIGHTLY_ANALYSTS = (
    (
        "ai_product",
        "provider routing, chat-with-AI, two-agent workflows, and user-visible AI behavior",
    ),
    (
        "safety_budget",
        "rate limits, provider budgets, isolation, permissions, secrets, and failure handling",
    ),
    (
        "tests_observability",
        "regression tests, telemetry, lifecycle state, logs, and operational diagnosability",
    ),
)
NIGHTLY_IMPROVEMENT_INSTRUCTIONS = """This is a proactive nightly AI improvement task, not an incident repair.

The Implementer has full write access to the isolated candidate worktree. Make whatever repository changes
are needed for the selected improvement, including tests, configuration, scripts, and documentation. Do not
commit, push, deploy, or modify anything outside the candidate worktree. The supervisor will preserve the
uncommitted candidate for human review after the tests finish. Do not create cosmetic-only changes. If no
bounded improvement is justified, finish the plan with exactly:
VERDICT: NO_CHANGE_NEEDED
Otherwise give the Implementer an exact plan and tests, then implement it fully in the candidate worktree.
"""


def _redact_nightly_text(value: str) -> str:
    """Redact assignment and JSON-style credential values before persistence/logging."""
    return _NIGHTLY_SECRET_RE.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote')}<redacted>{match.group('quote')}",
        value or "",
    )


def _nightly_analyst_prompt(role: str, focus: str, evidence: str) -> str:
    return f"""You are the read-only {role} analyst in a nightly multi-agent review of ceph-ai.

Inspect the isolated repository and analyze only this focus: {focus}.
Do not edit files, create files, commit, push, deploy, call Ceph, change configuration,
or access credentials. This is an advisory report for a separate Planner and one Implementer.
Return a concise report with: findings backed by exact files/functions, one or more bounded
improvement candidates if justified, risks, and the smallest regression-test idea. Do not
recommend more than one implementation candidate. If nothing is justified, say so clearly.

Nightly task context (already redacted):
---
{evidence[:4_000]}
---
"""


def _run_nightly_analyst(
    repo: Path,
    evidence: str,
    role: str,
    focus: str,
    *,
    provider: str,
    model: str,
    account_profile: str,
    timeout_seconds: int,
) -> tuple[str, str]:
    """Run one bounded read-only analyst in its own temporary worktree."""
    root = Path(tempfile.mkdtemp(prefix="ceph-ai-nightly-analysis-"))
    worktree = root / "repo"
    prompt = _nightly_analyst_prompt(role, focus, evidence)
    model_id = model.strip() or "default"
    started = time.monotonic()
    budget_checked = False
    reservation_id: str | None = None
    result = None
    try:
        _run(
            ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
            cwd=repo,
            timeout=60,
        )
        config = RepairConfig(repo=repo, planner_account_profile=account_profile)
        codex_home, claude_config_dir = _role_account_dirs(config, account_profile)
        selected_provider, command = _provider_command(
            provider,
            worktree,
            prompt,
            timeout_seconds,
            claude_config_dir=claude_config_dir,
            codex_home=codex_home,
            model=model,
            mode="review",
        )
        reservation_id = check_ai_budget(selected_provider, model_id, len(prompt))
        budget_checked = True
        with _ai_process_environment() as ai_env:
            result = _run(
                command,
                cwd=worktree,
                timeout=timeout_seconds,
                input_text=prompt,
                check=False,
                env=ai_env,
            )
        if result.returncode != 0:
            details = _redact_nightly_text(result.stdout[-1200:])
            raise RuntimeError(f"{selected_provider} exited with {result.returncode}: {details}")
        status = _run(["git", "status", "--porcelain"], cwd=worktree, check=False, timeout=30)
        if status.stdout.strip():
            raise RuntimeError("analyst modified its read-only worktree")
        report = _redact_nightly_text(result.stdout or "")
        report = report.strip()[-NIGHTLY_ANALYSIS_REPORT_LIMIT:]
        if not report:
            raise RuntimeError("analyst returned an empty report")
        record_ai_attempt(
            reservation_id=reservation_id,
            feature="nightly_multi_agent_analysis",
            provider=selected_provider,
            model_id=model_id,
            status="SUCCESS",
            latency_ms=round((time.monotonic() - started) * 1000),
            input_chars=len(prompt),
            output_chars=len(result.stdout or ""),
        )
        return role, f"[{role} / {selected_provider}]\n{report}"
    except AIBudgetError:
        record_ai_attempt(
            reservation_id=None,
            feature="nightly_multi_agent_analysis",
            provider=locals().get("selected_provider", provider),
            model_id=model_id,
            status="ERROR",
            latency_ms=round((time.monotonic() - started) * 1000),
            input_chars=0,
            output_chars=0,
            error_type="AIBudgetError",
        )
        raise
    except Exception as exc:
        if budget_checked:
            record_ai_attempt(
                reservation_id=reservation_id,
                feature="nightly_multi_agent_analysis",
                provider=locals().get("selected_provider", provider),
                model_id=model_id,
                status="ERROR",
                latency_ms=round((time.monotonic() - started) * 1000),
                input_chars=len(prompt),
                output_chars=len(getattr(result, "stdout", "") or ""),
                error_type=type(exc).__name__,
            )
        raise
    finally:
        cleanup_ok = True
        if worktree.exists():
            cleanup = _run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=repo,
                check=False,
                timeout=60,
            )
            cleanup_ok = cleanup.returncode == 0
            if not cleanup_ok:
                logger.error(
                    "could not remove nightly analyst worktree %s; preserving it for safe cleanup: %s",
                    worktree,
                    _redact_nightly_text(cleanup.stdout[-1200:]),
                )
        if cleanup_ok:
            shutil.rmtree(root, ignore_errors=True)


def collect_nightly_multi_agent_analysis(repo: Path, evidence: str) -> tuple[list[str], list[str]]:
    """Collect independent advisory reports without granting any agent write access."""
    if not settings.ai_nightly_multi_agent_analysis_enabled:
        return [], []
    provider = settings.code_repair_planner_provider or settings.code_repair_provider
    account_profile = _configured_account_profile(
        settings.code_repair_planner_account_source,
        settings.code_repair_planner_account_profile,
    )
    reports: list[str] = []
    failures: list[str] = []
    max_workers = min(settings.ai_nightly_multi_agent_max_parallel, len(NIGHTLY_ANALYSTS))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="nightly-analyst") as pool:
        futures = {
            pool.submit(
                _run_nightly_analyst,
                repo,
                evidence,
                role,
                focus,
                provider=provider,
                model=settings.code_repair_planner_model,
                account_profile=account_profile,
                timeout_seconds=settings.ai_nightly_multi_agent_timeout_seconds,
            ): role
            for role, focus in NIGHTLY_ANALYSTS
        }
        for future in as_completed(futures):
            role = futures[future]
            try:
                _, report = future.result()
            except Exception as exc:
                safe_error = _redact_nightly_text(str(exc))[:600]
                failures.append(f"{role}: {safe_error}")
                logger.warning("nightly analyst %s failed: %s", role, safe_error)
            else:
                reports.append(report)
    reports.sort()
    return reports, failures


def _nightly_analysis_context(reports: list[str]) -> str:
    if not reports:
        return ""
    context = "\n\n".join(reports)
    return "\n\nIndependent read-only analyst reports (advisory; verify before changing):\n---\n" + context[:NIGHTLY_ANALYSIS_TOTAL_LIMIT] + "\n---"


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


def nightly_override_for_today(now: datetime | None = None) -> bool | None:
    """Return today's Dashboard override, or None when the normal schedule applies."""
    current = now or datetime.now(timezone.utc)
    local_date = current.astimezone(NIGHTLY_TIMEZONE).date().isoformat()
    if settings.ai_nightly_improvement_override_date != local_date:
        return None
    return settings.ai_nightly_improvement_override_enabled


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
    # Keep this rule at the public function boundary as well as the systemd
    # entrypoint: manual callers and future schedulers must honour the same
    # Dashboard choice for the current local day.
    if nightly_override_for_today(now) is False:
        logger.info("nightly AI improvement is disabled for today by Dashboard override")
        return False
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
    override = nightly_override_for_today(current)
    if dirty_checkout and override is not True:
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
    candidate_root = state_path.parent / "nightly-ai-improvement-candidates"
    removed_candidates = cleanup_preserved_candidates(
        repo,
        candidate_root,
        keep=NIGHTLY_CANDIDATE_MAX_COUNT,
        max_age_seconds=NIGHTLY_CANDIDATE_RETENTION_SECONDS,
    )
    if removed_candidates:
        logger.info("nightly candidate cleanup removed %d old candidate(s)", len(removed_candidates))
    send_code_repair_alert(
        "🌙 AI NIGHTLY IMPROVEMENT BẮT ĐẦU\n"
        "Các analyst read-only đang rà soát; sau đó một Planner/Implementer duy nhất mới quyết định và sửa.\n"
        "Phạm vi: AI/chat/router/giới hạn/quan sát/học; chỉ worktree + test, không đụng tài khoản hay cấu hình bí mật."
        + ("\n⚠️ Dashboard đã cho phép chạy hôm nay dù checkout có thay đổi chưa commit." if dirty_checkout else "")
    )
    analysis_reports, analysis_failures = collect_nightly_multi_agent_analysis(
        repo, NIGHTLY_IMPROVEMENT_EVIDENCE,
    )
    analysis_context = _nightly_analysis_context(analysis_reports)
    state.update({
        "analysis_status": (
            "COMPLETED" if analysis_reports else
            "FALLBACK_NO_REPORTS" if settings.ai_nightly_multi_agent_analysis_enabled else
            "DISABLED"
        ),
        "analysis_reports": len(analysis_reports),
        "analysis_failures": analysis_failures,
    })
    _save_nightly_state(state_path, state)
    evidence = NIGHTLY_IMPROVEMENT_EVIDENCE + analysis_context
    repair_state = state_path.with_name("nightly-ai-improvement-repairs.json")
    result = run_repair(
        evidence,
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
            full_access=True,
            create_commit=False,
            preserve_candidate=True,
            candidate_root=candidate_root,
            isolate_venv=True,
            require_changed_tests=False,
            test_env_unset=NIGHTLY_TEST_ENV_UNSET,
            test_env_file="/dev/null",
            test_runner_command=".venv/bin/python -m pytest",
            timeout_seconds=min(settings.code_repair_timeout_seconds, NIGHTLY_AI_STEP_TIMEOUT_SECONDS),
            # Nightly produces an uncommitted candidate for human review.
            # Do not inherit the production repair pipeline's promotion
            # settings: those settings may intentionally be enabled for
            # incident repair and must not make this review-only job fail.
            push=False,
            deploy_staging=False,
            promote_main=False,
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
        "candidate_worktree": getattr(result, "candidate_worktree", None),
        "error": result.error,
    })
    _save_nightly_state(state_path, state)
    if result.status == "NO_CHANGE":
        message = "🌙 AI NIGHTLY IMPROVEMENT\nKết quả: chưa có nâng cấp AI nào đủ nhỏ và an toàn để triển khai hôm nay."
    elif result.status in {"PUSHED", "STAGING_VERIFIED", "PROMOTED", "COMMITTED", "PATCH_READY"}:
        files = ", ".join(result.changed_files or []) or "—"
        message = (
            "✅ AI NIGHTLY IMPROVEMENT ĐÃ TẠO CANDIDATE\n"
            f"Kết quả: {result.status}\nBranch: {result.branch or '—'}\n"
            f"Files: {files}\nCandidate: {getattr(result, 'candidate_worktree', None) or '—'}\n"
            f"Review rounds: {result.review_rounds}\nChưa commit/chưa push."
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
        service_health.record_safe("code-repair")
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
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(10):
                service_health.record_safe("code-repair")

        service_health.record_safe("code-repair")
        heartbeat_thread = threading.Thread(target=heartbeat, name="code-repair-heartbeat", daemon=True)
        heartbeat_thread.start()
        try:
            run_forever()
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
