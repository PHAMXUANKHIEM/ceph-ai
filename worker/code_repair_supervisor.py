"""Supervise Code Repair and run a read-only nightly system review.

This process deliberately lives outside Watcher/Worker. Candidate deployments
restart those services, while this supervisor must survive long enough to
evaluate the deployment and promote or roll it back.
"""

from __future__ import annotations

import fcntl
from collections import deque
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
    reconcile_stale_attempts_file,
    run_repair,
    _provider_command,
    _role_account_dirs,
    _ai_process_environment,
    _run,
    send_code_repair_alert,
)
from worker import ceph_capability_learning as ceph_learning
from shared.ai_budget import AIBudgetError, check as check_ai_budget
from shared.ai_observability import record_ai_attempt
from shared import service_health

logger = logging.getLogger(__name__)
REPAIR_COOLDOWN_SECONDS = 3600
NIGHTLY_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
NIGHTLY_IMPROVEMENT_EVIDENCE = (
    "Scheduled read-only nightly operations review for ceph-ai. Assess what the operator should do next "
    "in three areas: (1) system upgrades and production readiness, (2) operational errors and failure "
    "handling, and (3) tests, coverage, and validation strategy. Produce a prioritized plan only. "
    "Do not implement, modify files, commit, push, deploy, execute tests, or run remediation."
)
NIGHTLY_ANALYSIS_REPORT_LIMIT = 4_500
NIGHTLY_ERROR_EVIDENCE_MAX_FILES = 6
NIGHTLY_ERROR_EVIDENCE_MAX_CHARS = 6_000
NIGHTLY_ERROR_EVIDENCE_MAX_BYTES_PER_FILE = 100_000
# Since the move to containers the services log to podman, not /var/log: the
# 09/10/2026 review read no service log and missed ~170k worker tracebacks.
NIGHTLY_CONTAINER_PREFIX = "ceph-ai_"
NIGHTLY_CONTAINER_TAIL_LINES = 400
NIGHTLY_CONTAINER_LOG_TIMEOUT_SECONDS = 120
_NIGHTLY_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:api[_-]?key|secret|password|token|authorization|private[_-]?key)"
    r"[\"']?\s*[:=]\s*)(?P<quote>[\"']?)(?P<value>[^\"'\s,}]+)(?P=quote)"
)
NIGHTLY_ANALYSTS = (
    (
        "system_upgrade",
        "system and dependency upgrades, production readiness, compatibility, migrations, security posture, "
        "deployment/rollback, and operational capacity",
    ),
    (
        "error_review",
        "operational failure modes, error handling, stale state, retries, logs/diagnostics, alert quality, "
        "and unresolved defects evident in source or existing reports",
    ),
    (
        "test_review",
        "test gaps, flaky or missing regression coverage, acceptance/e2e validation, safe test commands, "
        "and how to verify the highest-priority operational changes",
    ),
)


def _redact_nightly_text(value: str) -> str:
    """Redact assignment and JSON-style credential values before persistence/logging."""
    return _NIGHTLY_SECRET_RE.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote')}<redacted>{match.group('quote')}",
        value or "",
    )


def _nightly_analyst_prompt(role: str, focus: str, evidence: str) -> str:
    return f"""You are the read-only {role} analyst in a nightly multi-agent review of ceph-ai.

Inspect the isolated repository and analyze only this focus: {focus}.
This is planning-only: do not edit/create files, commit, push, deploy, call Ceph, change configuration,
execute tests, run commands that mutate state, or access credentials. Do not assume another agent will
implement your proposal tonight. Return a concise prioritized plan using this format:
1) Priority (P0/P1/P2) and proposed next task.
2) Evidence (exact files/functions or supplied operational evidence; distinguish fact from inference).
3) Expected benefit and risk/dependency.
4) Acceptance criteria and specific tests an operator should run later; do not run them now.
Recommend at most two tasks. If no actionable task is justified, say so clearly.
For error_review, use runtime log evidence only when it is explicitly included below; if it says no
matching errors or unavailable, do not claim that runtime incidents were reviewed.

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
    runtime_error_evidence = _collect_recent_nightly_error_evidence()
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
                evidence + runtime_error_evidence if role == "error_review" else evidence,
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


def _read_recent_tail(path: Path, cutoff: float) -> str | None:
    """The bounded tail of a log file updated since ``cutoff``, else None."""
    try:
        stat = path.stat()
        if stat.st_mtime < cutoff or not path.is_file():
            return None
        with path.open("rb") as handle:
            handle.seek(max(0, stat.st_size - NIGHTLY_ERROR_EVIDENCE_MAX_BYTES_PER_FILE))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def _redacted_error_blocks(text: str) -> list[str]:
    """The last two error blocks of ``text``, cleaned and secret-redacted."""
    blocks = []
    matches = list(ERROR_RE.finditer(text))[-2:]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start():min(end, match.start() + 2_000)]
        block = clean_evidence(block)
        block = SECRET_RE.sub(lambda item: item.group(1) + "=<redacted>", block)
        block = _redact_nightly_text(block).strip()
        if block:
            blocks.append(block)
    return blocks


def _container_log_tail(podman: str, name: str) -> tuple[str, int, str] | None:
    """(name, error lines in the last 24 h, bounded tail) of one container's log.

    The whole day is streamed to count errors, but only the last
    NIGHTLY_CONTAINER_TAIL_LINES lines are kept, and reading stops at the timeout.
    """
    deadline = time.monotonic() + NIGHTLY_CONTAINER_LOG_TIMEOUT_SECONDS
    errors = 0
    tail: deque[str] = deque(maxlen=NIGHTLY_CONTAINER_TAIL_LINES)
    try:
        with subprocess.Popen(  # nosec B603 - fixed argv, no shell
            [podman, "logs", "--since", "24h", name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
        ) as process:
            for line in process.stdout or ():
                if ERROR_RE.search(line):
                    errors += 1
                tail.append(line)
                if time.monotonic() > deadline:
                    process.kill()
                    break
    except (OSError, subprocess.SubprocessError):
        return None
    return name, errors, "".join(tail)


def _container_log_tails() -> list[tuple[str, int, str]]:
    """Every running ceph-ai service container's 24 h error count and log tail."""
    podman = shutil.which("podman") or "/usr/bin/podman"
    try:
        listed = subprocess.run(  # nosec B603 - fixed argv, no shell
            [podman, "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    names = sorted(name for name in listed.stdout.split()
                   if name.startswith(NIGHTLY_CONTAINER_PREFIX) and "code-repair" not in name)
    return [tail for tail in (_container_log_tail(podman, name) for name in names) if tail is not None]


def _nightly_log_sources(log_dir: Path, cutoff: float, containers) -> tuple[list[str], list[tuple[str, str]]]:
    """Per-container error counts, and (label, text) for every container log and recent log file."""
    counts: list[str] = []
    sources: list[tuple[str, str]] = []
    for name, errors, text in containers():
        # The count shows the scale a two-block excerpt cannot (170k identical tracebacks).
        counts.append(f"{name}={errors}")
        sources.append((f"container {name} ({errors} error lines in 24 h)", text))
    try:
        paths = sorted(log_dir.glob("ceph-ai-*.log"), key=lambda path: path.name)
    except OSError:
        paths = []
    for path in paths:
        text = None if "code-repair" in path.name else _read_recent_tail(path, cutoff)
        if text is not None:
            sources.append((path.name, text))
    return counts, sources


def _collect_recent_nightly_error_evidence(
    log_dir: Path = Path("/var/log"), *, now: datetime | None = None, containers=_container_log_tails,
) -> str:
    """Read a bounded 24-hour tail of application errors for the error analyst only."""
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - 24 * 60 * 60
    evidence: list[str] = []
    total_chars = 0
    counts, sources = _nightly_log_sources(log_dir, cutoff, containers)
    per_source = NIGHTLY_ERROR_EVIDENCE_MAX_CHARS // 4
    for label, text in sources:
        if len(evidence) >= NIGHTLY_ERROR_EVIDENCE_MAX_FILES * 2:
            break
        source_chars = 0
        for block in _redacted_error_blocks(text):
            remaining = min(NIGHTLY_ERROR_EVIDENCE_MAX_CHARS - total_chars, per_source - source_chars)
            if remaining <= 0:
                break
            block = block[:remaining]
            evidence.append(f"{label}:\n{block}")
            total_chars += len(block)
            source_chars += len(block)
        if total_chars >= NIGHTLY_ERROR_EVIDENCE_MAX_CHARS:
            break

    summary = f"\n\nError lines in the last 24 h per service container: {', '.join(counts)}." if counts else ""
    if not evidence:
        return summary + (
            "\n\nRuntime error evidence: no matching errors found in the ceph-ai container logs "
            "or readable ceph-ai log files of the last 24 hours."
        )
    return summary + (
        "\n\nBounded, secret-redacted error excerpts from the last 24 hours of ceph-ai container logs "
        "and log files (excerpt is not a strict event-time window) "
        "(provided only to the error-review analyst; verify against source):\n"
        + "\n---\n".join(evidence)
    )


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
    """Run once daily; allow explicit same-day retries after incomplete/failed runs."""
    local = now.astimezone(NIGHTLY_TIMEZONE)
    if state.get("last_run_date") != local.date().isoformat():
        return True
    # A RUNNING state only remains after an abnormal process death: a live
    # pipeline still owns _repair_run_lock, and normal completion writes a
    # terminal status.  FAILED is intentionally retried by systemd.
    return state.get("status") in {"RUNNING", "FAILED", "PLAN_INCOMPLETE"}


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
        state = _load_nightly_state(state_path)
        # A runtime failure is retryable.  Do not consume this calendar day;
        # the systemd failure exit will invoke this job again after backoff.
        state.pop("last_run_date", None)
        for field in (
            "analysis_status", "analysis_reports", "analysis_failures",
            "analysis_report_previews", "changed_files", "commit", "candidate_worktree",
            "source_revision", "checkout_dirty", "checkout_changes", "review_scope",
            "runtime_error_evidence",
        ):
            state.pop(field, None)
        state.update({
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "FAILED",
            "mode": "PLAN_ONLY",
            "analysis_reports": 0,
            "analysis_failures": [_redact_nightly_text(str(exc))[:600]],
            "analysis_report_previews": [],
            "runtime_error_evidence": "not_collected",
            "changed_files": [],
            "commit": None,
            "candidate_worktree": None,
            "error": _redact_nightly_text(str(exc)),
        })
        try:
            _save_nightly_state(state_path, state)
        except OSError:
            logger.exception("could not persist failed nightly AI improvement state")
        logger.exception("nightly AI improvement failed")
        try:
            send_code_repair_alert(
                "⚠️ NIGHTLY SYSTEM REVIEW KHÔNG LẬP ĐƯỢC KẾ HOẠCH\n"
                f"Lỗi runtime: {_redact_nightly_text(str(exc))[:900]}"
            )
        except Exception:
            logger.exception("could not send nightly AI improvement failure alert")
        return False


def _run_nightly_ai_improvement_locked(
    repo: Path, state_path: Path, *, now: datetime | None = None,
) -> bool:
    """Create a read-only nightly work plan while holding the shared job lock."""
    current = now or datetime.now(timezone.utc)
    state = _load_nightly_state(state_path)
    if not _nightly_due(state, current):
        return False

    local = current.astimezone(NIGHTLY_TIMEZONE)
    dirty_checkout = _dirty_checkout(repo)
    # Fixed argv (no caller input); git is resolved from the host service PATH.
    revision = subprocess.run(  # nosec B603 B607
        ["git", "rev-parse", "--short", "HEAD"], cwd=repo, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    source_revision = revision.stdout.strip() if revision.returncode == 0 else "unknown"

    # Do not let a crash or provider error leave yesterday's plan in today's
    # morning report while this run is still in progress.
    for field in (
        "analysis_status", "analysis_reports", "analysis_failures",
        "analysis_report_previews", "changed_files", "commit", "candidate_worktree",
        "error",
    ):
        state.pop(field, None)

    # The state records an in-progress run before invoking AI.  If this
    # process is killed, _nightly_due will retry only after it can reacquire
    # the process lock, so no two pipelines run concurrently.
    state.update({
        "last_run_date": local.date().isoformat(),
        "started_at": current.isoformat(),
        "status": "RUNNING",
        "mode": "PLAN_ONLY",
        "source_revision": source_revision,
        "checkout_dirty": bool(dirty_checkout),
        "checkout_changes": dirty_checkout.splitlines() if dirty_checkout else [],
        "review_scope": [name for name, _ in NIGHTLY_ANALYSTS],
        "runtime_error_evidence": "pending" if settings.ai_nightly_multi_agent_analysis_enabled else "not_collected",
    })
    _save_nightly_state(state_path, state)
    analysis_reports, analysis_failures = collect_nightly_multi_agent_analysis(
        repo, NIGHTLY_IMPROVEMENT_EVIDENCE,
    )
    status = "PLAN_READY" if analysis_reports and not analysis_failures else (
        "PLAN_READY_WITH_WARNINGS" if analysis_reports else "PLAN_INCOMPLETE"
    )
    state.update({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "analysis_status": (
            "COMPLETED" if analysis_reports else
            "FALLBACK_NO_REPORTS" if settings.ai_nightly_multi_agent_analysis_enabled else
            "DISABLED"
        ),
        "analysis_reports": len(analysis_reports),
        "analysis_failures": analysis_failures,
        "runtime_error_evidence": "bounded_redacted_24h" if settings.ai_nightly_multi_agent_analysis_enabled else "not_collected",
        # Bounded, redacted plan excerpts are what the morning report delivers.
        "analysis_report_previews": [
            _redact_nightly_text(report).strip()[:1_200]
            for report in analysis_reports
        ],
        "changed_files": [],
        "commit": None,
        "candidate_worktree": None,
        "error": "\n".join(analysis_failures) if analysis_failures else None,
    })
    _save_nightly_state(state_path, state)
    logger.info(
        "nightly read-only plan completed: status=%s analysts=%s source=%s dirty=%s",
        status, len(analysis_reports), source_revision, bool(dirty_checkout),
    )
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
