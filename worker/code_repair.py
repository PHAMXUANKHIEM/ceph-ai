"""Bounded AI-assisted repair pipeline for the ceph-ai application itself.

This is deliberately an external supervisor, not part of Watcher/Worker: a
candidate deployment may restart those processes and must not kill the repair
controller that is deciding whether the deployment succeeded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings as app_settings
from shared.telegram_alerts import send_code_repair_alert


ALLOWED_PREFIXES = ("config/", "dashboard/", "shared/", "tests/", "watcher/", "worker/")
FORBIDDEN_PREFIXES = (".env", ".git", ".github/", ".codex", "alembic/versions/", "scripts/deploy/")
ERROR_RE = re.compile(r"(Traceback \(most recent call last\):|\b(?:CRITICAL|ERROR)\b)")
SECRET_RE = re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[=:]\s*\S+")
DIFF_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|secret|password|token)\s*[=:]\s*"
    r"[\"'][A-Za-z0-9_./+:-]{16,}[\"']"
)
_TELEGRAM_NOISE_RE = re.compile(
    r"(?i)(paramiko\.transport:)?(?:connected \(version|authentication \(publickey\) successful)"
)
_SOURCE_LOG_RE = re.compile(r"^Source (?:application )?log:\s*(?P<name>\S+)", re.MULTILINE)
TRANSCRIPT_CONTENT_LIMIT = 20_000


class RepairError(RuntimeError):
    pass


DEFAULT_REPAIR_REVIEW_ROUNDS = 2


def summarize_evidence(evidence: str, *, max_chars: int = 360) -> str:
    """Return one useful error line for Telegram; AI still receives full evidence."""
    source = _SOURCE_LOG_RE.search(evidence)
    source_name = source.group("name") if source else None
    lines = [" ".join(line.split()) for line in evidence.splitlines()]
    useful = [line for line in lines if line and not _TELEGRAM_NOISE_RE.search(line)]
    marker_indexes = [index for index, line in enumerate(useful) if ERROR_RE.search(line)]
    selected = useful[marker_indexes[-1]] if marker_indexes else (useful[-1] if useful else "Lỗi không rõ")
    if "Traceback (most recent call last):" in selected and marker_indexes:
        tail = useful[marker_indexes[-1] + 1:]
        exception_lines = [
            line for line in tail
            if re.match(r"^[A-Za-z_][\w.]*?(?:Error|Exception):", line)
        ]
        if exception_lines:
            selected = exception_lines[-1]
    selected = SECRET_RE.sub(lambda match: match.group(1) + "=<redacted>", selected)
    prefix = f"{source_name}: " if source_name else ""
    return (prefix + selected)[:max_chars]


def clean_evidence(evidence: str) -> str:
    """Remove transport chatter without discarding the actual traceback."""
    lines = [line for line in evidence.splitlines() if not _TELEGRAM_NOISE_RE.search(line)]
    return "\n".join(lines).strip()


@dataclass(frozen=True)
class RepairConfig:
    repo: Path
    remote: str = "origin"
    base_branch: str = "main"
    branch_prefix: str = "ai-repair/"
    provider: str = "auto"
    planner_provider: str | None = None
    planner_model: str = ""
    planner_account_profile: str = "configured"
    implementer_provider: str | None = None
    implementer_model: str = ""
    implementer_account_profile: str = "configured"
    # Internal safety default for source-repair jobs. The operator-facing
    # setting was removed; Dashboard dual chat has its own continuous loop.
    max_review_rounds: int = DEFAULT_REPAIR_REVIEW_ROUNDS
    test_command: str = "PYTHONPATH=. .venv/bin/pytest -q"
    # Optional bounded command for the second, host-level candidate gate.
    # Empty retains that script's default regression command.
    candidate_test_command: str = ""
    # Proactive changes must demonstrate coverage in the candidate diff.
    # Incident repair retains the historical permissive behaviour because a
    # focused test can be impractical for an infrastructure-only correction.
    require_changed_tests: bool = False
    timeout_seconds: int = 1800
    push: bool = False
    deploy_staging: bool = False
    promote_main: bool = False
    state_file: Path = Path("/var/lib/ceph-ai/code-repair-state.json")
    task_kind: str = "application-repair"
    task_instructions: str | None = None
    max_ai_attempts: int = 3
    max_pipeline_attempts: int = 3
    running_stale_seconds: int = 3600
    notify_telegram: bool = True
    transcript_file: Path | None = None
    # A proactive improvement task may correctly conclude that no small,
    # testable upgrade is warranted.  Normal repair tasks must still fail
    # closed when no patch is produced.
    allow_no_change: bool = False


@dataclass
class RepairResult:
    status: str
    fingerprint: str
    branch: str | None = None
    commit: str | None = None
    provider: str | None = None
    planner_provider: str | None = None
    implementer_provider: str | None = None
    review_rounds: int = 0
    changed_files: list[str] | None = None
    test_output: str = ""
    error: str | None = None


class RepairProgressNotifier:
    """Telegram lifecycle reporter with a ten-minute in-progress heartbeat."""

    def __init__(
        self, evidence: str, branch: str, *, interval_seconds: int = 3600,
        enabled: bool = True,
    ) -> None:
        self.evidence = summarize_evidence(evidence)
        self.branch = branch
        self.interval_seconds = interval_seconds
        self.enabled = enabled
        self.percent = 5
        self.stage = "Đang chuẩn bị worktree và branch sửa lỗi"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled:
            return
        send_code_repair_alert(
            "🚨 AI CODE REPAIR BẮT ĐẦU\n"
            f"Lỗi: {self.evidence}\n"
            f"Đang sửa: phân tích nguyên nhân và tạo patch trên `{self.branch}`\n"
            f"Tiến trình: {self.percent}% — {self.stage}"
        )
        self._thread = threading.Thread(target=self._heartbeat, name="code-repair-telegram", daemon=True)
        self._thread.start()

    def update(self, percent: int, stage: str) -> None:
        self.percent = max(self.percent, min(99, percent))
        self.stage = stage

    def _heartbeat(self) -> None:
        if not self.enabled:
            return
        while not self._stop.wait(self.interval_seconds):
            send_code_repair_alert(
                "⏳ AI CODE REPAIR ĐANG CHẠY\n"
                f"Lỗi: {self.evidence}\n"
                f"Đang sửa: {self.stage}\n"
                f"Tiến trình: {self.percent}%\n"
                "Hệ thống sẽ cập nhật lại sau 60 phút nếu chưa hoàn tất."
            )

    def finish(self, result: RepairResult) -> None:
        if not self.enabled:
            self._stop.set()
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        if result.status in {"PUSHED", "COMMITTED", "STAGING_VERIFIED", "PROMOTED"}:
            files = ", ".join(result.changed_files or []) or "không có"
            send_code_repair_alert(
                "✅ AI CODE REPAIR THÀNH CÔNG\n"
                f"Lỗi: {self.evidence}\n"
                f"Đã sửa: {files}\n"
                f"Kết quả: {result.status} · commit `{result.commit or 'n/a'}`\n"
                "Tiến trình: 100%"
            )
        else:
            send_code_repair_alert(
                "❌ AI CODE REPAIR KHÔNG HOÀN TẤT\n"
                f"Lỗi: {self.evidence}\n"
                f"Dừng tại: {self.stage}\n"
                f"Chi tiết: {' '.join((result.error or result.status).split())[:700]}"
            )


def _run(args: list[str], *, cwd: Path, timeout: int = 300, input_text: str | None = None,
         check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args, cwd=cwd, text=True, input=input_text, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=timeout, env=env or os.environ.copy(),
    )
    if check and result.returncode != 0:
        raise RepairError(f"{shlex.join(args)} failed ({result.returncode}):\n{result.stdout[-6000:]}")
    return result


def extract_latest_error(paths: list[Path], *, max_chars: int = 16_000) -> str | None:
    """Return the newest useful traceback/error window without credentials."""
    candidates: list[tuple[float, str]] = []
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")[-250_000:]
        matches = list(ERROR_RE.finditer(text))
        if not matches:
            continue
        start = matches[-1].start()
        block = text[max(0, start - 1200):start + max_chars]
        block = SECRET_RE.sub(lambda m: m.group(1) + "=<redacted>", block)
        candidates.append((path.stat().st_mtime, f"Source log: {path.name}\n{block}"))
    return max(candidates, default=(0, ""), key=lambda item: item[0])[1] or None


def fingerprint(evidence: str) -> str:
    normalized = re.sub(r"\b[0-9a-f]{8,}\b|\d{4}-\d\d-\d\d[^ ]*|\b\d+\b", "#", evidence)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"attempts": {}}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str))
    os.replace(temporary, path)


def _transcript_text(value: str, *, limit: int = TRANSCRIPT_CONTENT_LIMIT) -> str:
    value = value or ""
    if len(value) <= limit:
        return value
    half = max(1, (limit - 80) // 2)
    return value[:half] + "\n...[nội dung đã rút gọn]...\n" + value[-half:]


def _record_transcript(
    config: RepairConfig, *, speaker: str, event: str, direction: str,
    content: str, provider: str | None = None, model: str | None = None,
) -> None:
    """Append a bounded, admin-readable conversation event for user tasks."""
    if config.transcript_file is None:
        return
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "speaker": speaker,
        "event": event,
        "direction": direction,
        "provider": provider,
        "model": model or "",
        "content": _transcript_text(content),
    }
    try:
        config.transcript_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not config.transcript_file.exists():
            config.transcript_file.touch(mode=0o600)
        with config.transcript_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return


def reconcile_stale_attempts(
    state: dict,
    *,
    now: datetime | None = None,
    stale_seconds: int = 3600,
) -> list[str]:
    """Mark interrupted repair attempts terminal before they block retries.

    The supervisor can be restarted or killed while ``run_repair`` is between
    writing RUNNING and writing its final result. Keep the branch name so a
    caller can clean its isolated worktree, but never treat that old attempt
    as active again.
    """
    now = now or datetime.now(timezone.utc)
    stale_branches: list[str] = []
    for value in state.setdefault("attempts", {}).values():
        if not isinstance(value, dict) or value.get("status") != "RUNNING":
            continue
        try:
            started = datetime.fromisoformat(str(value.get("started_at")))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            age = (now - started).total_seconds()
        except (TypeError, ValueError):
            age = stale_seconds + 1
        if age <= max(0, int(stale_seconds)):
            continue
        value["status"] = "FAILED_STALE"
        value["error"] = "repair attempt became stale without a completion record"
        value["finished_at"] = now.isoformat()
        branch = value.get("branch")
        if isinstance(branch, str) and branch:
            stale_branches.append(branch)
    return stale_branches


def reconcile_stale_attempts_file(path: Path, *, stale_seconds: int = 3600) -> list[str]:
    """Persist stale-attempt reconciliation and return branches to clean."""
    state = _load_state(path)
    stale_branches = reconcile_stale_attempts(state, stale_seconds=stale_seconds)
    if stale_branches:
        _save_state(path, state)
    return stale_branches


def cleanup_stale_worktrees(repo: Path, branches: list[str]) -> list[str]:
    """Remove only matching temporary repair worktrees; keep their branches.

    Worktrees are intentionally restricted to ``/tmp/ceph-ai-repair-*/repo``
    so stale-state cleanup cannot remove a normal checkout or user worktree.
    """
    wanted = {
        branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
        for branch in branches
    }
    if not wanted:
        return []
    listing = _run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=False,
    )
    if listing.returncode != 0:
        return []
    records: list[tuple[str, str]] = []
    path: str | None = None
    branch: str | None = None
    for line in listing.stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            path = line.removeprefix("worktree ")
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ")
        elif not line and path and branch:
            records.append((path, branch))
            path = branch = None
    removed: list[str] = []
    for worktree, branch in records:
        candidate = Path(worktree)
        if (
            branch in wanted
            and candidate.name == "repo"
            and candidate.parent.name.startswith("ceph-ai-repair-")
            and candidate.parent.parent == Path("/tmp")
        ):
            result = _run(
                ["git", "worktree", "remove", "--force", str(candidate)],
                cwd=repo,
                check=False,
            )
            if result.returncode == 0:
                removed.append(str(candidate))
    return removed


def _provider_command(provider: str, worktree: Path, prompt: str, timeout: int,
                      *, claude_config_dir: Path | None = None,
                      codex_home: Path | None = None,
                      model: str = "", mode: str = "implement") -> tuple[str, list[str]]:
    if mode not in {"implement", "review", "full-access"}:
        raise RepairError(f"unsupported AI role mode: {mode!r}")
    codex = shutil.which("codex")
    if not codex:
        candidate = Path.home() / ".local" / "bin" / "codex"
        codex = str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    claude = shutil.which("claude")
    if not claude:
        candidate = Path.home() / ".local" / "bin" / "claude"
        claude = str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    if provider == "auto":
        # Claude is the configured production backend on the current staging
        # host. Prefer Codex only when its CLI account is actually usable.
        if codex:
            status_command = [codex, "login", "status"]
            if codex_home:
                status_command = ["env", f"CODEX_HOME={codex_home}", *status_command]
            status = _run(status_command, cwd=worktree, check=False, timeout=15)
            if status.returncode == 0:
                provider = "codex"
            elif claude:
                provider = "claude"
        elif claude:
            provider = "claude"
    if provider == "codex" and codex:
        if mode == "review":
            command = [codex, "exec", "--ephemeral", "--sandbox", "read-only", "-C", str(worktree)]
        elif mode == "full-access":
            # This is deliberately reserved for Telegram's separately
            # allow-listed /single-full operator mode. It runs as the service
            # user with no Codex sandbox or approval boundary.
            command = [
                codex, "exec", "--ephemeral",
                "--dangerously-bypass-approvals-and-sandbox", "-C", str(worktree),
            ]
        else:
            # Current Codex CLI makes --approve-for-me mutually exclusive with
            # --sandbox; approve-for-me itself routes commands through its
            # workspace-write automatic reviewer.
            command = [codex, "exec", "--ephemeral", "--approve-for-me", "-C", str(worktree)]
        if model.strip():
            command.extend(["--model", model.strip()])
        command.append("-")
        if codex_home:
            command = ["env", f"CODEX_HOME={codex_home}", *command]
        return provider, command
    if provider == "claude" and claude:
        permission_mode = (
            "plan" if mode == "review"
            else "bypassPermissions" if mode == "full-access"
            else "acceptEdits"
        )
        command = [claude, "-p", "--permission-mode", permission_mode, "--no-session-persistence"]
        if mode == "full-access":
            command.append("--dangerously-skip-permissions")
        if model.strip():
            command.extend(["--model", model.strip()])
        if claude_config_dir:
            # The dashboard stores its CLI session in a repo-local, gitignored
            # directory rather than root's default ~/.claude account.
            command = ["env", f"CLAUDE_CONFIG_DIR={claude_config_dir}", "DISABLE_AUTOUPDATER=1", *command]
        return provider, command
    raise RepairError(f"AI coding provider {provider!r} is unavailable or not authenticated")


_ACCOUNT_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")


def _role_account_dirs(config: RepairConfig, profile: str) -> tuple[Path, Path]:
    """Resolve the credential homes for one role without accepting arbitrary paths."""
    selected = (profile or "configured").strip()
    if selected == "configured":
        codex_home = Path(app_settings.codex_home).expanduser()
        claude_config_dir = Path(app_settings.claude_config_dir).expanduser()
        if not codex_home.is_absolute():
            codex_home = config.repo / codex_home
        if not claude_config_dir.is_absolute():
            claude_config_dir = config.repo / claude_config_dir
        return codex_home, claude_config_dir
    if not _ACCOUNT_PROFILE_RE.fullmatch(selected):
        raise RepairError("account profile chỉ được chứa chữ, số, _ hoặc - (tối đa 48 ký tự)")
    root = config.repo / ".ai-accounts" / selected
    return root / "codex", root / "claude"


def _validate_changes(worktree: Path) -> list[str]:
    output = _run(["git", "status", "--porcelain"], cwd=worktree).stdout
    # .venv is the supervisor-created symlink to the already provisioned
    # test environment, never an AI-authored candidate change.
    files = [line[3:] for line in output.splitlines() if len(line) > 3 and line[3:] != ".venv"]
    if not files:
        raise RepairError("AI did not produce a patch")
    invalid = [p for p in files if not p.startswith(ALLOWED_PREFIXES) or p.startswith(FORBIDDEN_PREFIXES)]
    if invalid:
        raise RepairError(f"AI changed paths outside the repair allowlist: {invalid}")
    diff = _candidate_diff(worktree, files, status_output=output)
    if DIFF_SECRET_RE.search(diff):
        raise RepairError("candidate diff appears to contain a credential")
    return files


def _candidate_diff(worktree: Path, files: list[str], *, status_output: str | None = None) -> str:
    """Return candidate diff, including untracked files Git diff normally hides."""
    diff = _run(["git", "diff", "--", *files], cwd=worktree).stdout
    status = status_output if status_output is not None else _run(
        ["git", "status", "--porcelain"], cwd=worktree,
    ).stdout
    untracked = {
        line[3:] for line in status.splitlines()
        if line.startswith("?? ") and line[3:] in files
    }
    for path in sorted(untracked):
        # --no-index returns 1 for a difference, which is the expected
        # outcome for a new candidate file.
        diff += _run(
            ["git", "diff", "--no-index", "--", "/dev/null", str(worktree / path)],
            cwd=worktree, check=False,
        ).stdout
    return diff


def _worktree_status(worktree: Path) -> str:
    return _run(["git", "status", "--porcelain"], cwd=worktree).stdout


def _review_verdict(output: str) -> str:
    """Require an explicit, machine-checkable reviewer decision."""
    verdicts = re.findall(r"(?im)^\s*VERDICT\s*:\s*(PASS|NEEDS_CHANGES)\s*$", output or "")
    if len(verdicts) != 1:
        raise RepairError("reviewer phải trả về đúng một dòng VERDICT: PASS hoặc VERDICT: NEEDS_CHANGES")
    return verdicts[0]


def _changed_test_files(files: list[str]) -> list[str]:
    return sorted({path for path in files if path.startswith("tests/") and path.endswith(".py")})


def _require_changed_tests(files: list[str]) -> None:
    if not _changed_test_files(files):
        raise RepairError("proactive improvement phải thêm hoặc cập nhật ít nhất một regression test trong tests/")


def _validate_proactive_test_changes(worktree: Path, files: list[str]) -> None:
    """Require additive regression coverage and reject direct test weakening."""
    tests = _changed_test_files(files)
    _require_changed_tests(files)
    diff = _candidate_diff(worktree, tests)
    added_test = re.search(r"(?m)^\+\s*(?:async\s+)?def\s+test_[A-Za-z0-9_]+", diff)
    weakened_test = re.search(
        r"(?m)^-\s*(?:(?:async\s+)?def\s+test_[A-Za-z0-9_]+|"
        r"(?:self\.)?assert(?:[A-Z][A-Za-z0-9_]*)?\b)",
        diff,
    )
    if weakened_test:
        raise RepairError("proactive improvement không được xóa test hoặc assertion hiện có")
    if not added_test:
        raise RepairError("proactive improvement phải bổ sung ít nhất một test_* regression mới")


def _focused_test_command(files: list[str]) -> str | None:
    tests = _changed_test_files(files)
    if not tests:
        return None
    return "PYTHONPATH=. .venv/bin/pytest -q " + " ".join(shlex.quote(path) for path in tests)


_INFRA_TEST_RE = re.compile(
    r"(?i)(sqlite3\.OperationalError: no such table|database is locked|"
    r"connection refused|temporary failure in name resolution|no space left on device|"
    r"systemerror: AST constructor recursion depth mismatch|INTERNALERROR>)"
)


def _test_failure_kind(output: str) -> str:
    return "INFRASTRUCTURE" if _INFRA_TEST_RE.search(output or "") else "CANDIDATE"


def run_repair(evidence: str, config: RepairConfig, *, force: bool = False) -> RepairResult:
    # Repeated polls of one traceback often contain different timestamps,
    # Paramiko chatter and object addresses. Fingerprint the concise exception
    # identity so the same application bug is attempted only once.
    fingerprint_input = (
        summarize_evidence(clean_evidence(evidence))
        if config.task_kind == "application-repair"
        else f"{config.task_kind}\n{evidence}"
    )
    fp = fingerprint(fingerprint_input)
    state = _load_state(config.state_file)
    previous = state.setdefault("attempts", {}).get(fp)
    previous_attempts = int((previous or {}).get("attempt_count") or (1 if previous else 0))
    if previous and not force:
        previous_status = str(previous.get("status") or "")
        stale_running = False
        if previous_status == "RUNNING":
            try:
                started = datetime.fromisoformat(str(previous.get("started_at")))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                stale_running = (
                    datetime.now(timezone.utc) - started
                ).total_seconds() > config.running_stale_seconds
            except ValueError:
                stale_running = True
        terminal = previous_status in {"PUSHED", "COMMITTED", "STAGING_VERIFIED", "PROMOTED"}
        exhausted = previous_attempts >= config.max_pipeline_attempts
        if terminal or exhausted or (previous_status == "RUNNING" and not stale_running):
            return RepairResult(status="SKIPPED_DUPLICATE", fingerprint=fp, branch=previous.get("branch"))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"{config.branch_prefix}{stamp}-{fp[:8]}"
    result = RepairResult(status="FAILED", fingerprint=fp, branch=branch)
    notifier = RepairProgressNotifier(evidence, branch, enabled=config.notify_telegram)
    notifier.start()
    state["attempts"][fp] = {
        "status": "RUNNING", "branch": branch,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "attempt_count": previous_attempts + 1,
    }
    _save_state(config.state_file, state)
    worktree_root = Path(tempfile.mkdtemp(prefix="ceph-ai-repair-"))
    planner_worktree = worktree_root / "planner"
    worktree = worktree_root / "repo"
    try:
        if not 0 <= int(config.max_review_rounds) <= 5:
            raise RepairError("max_review_rounds must be between 0 and 5")
        _run(["git", "fetch", config.remote, config.base_branch], cwd=config.repo)
        notifier.update(10, "Đã lấy source mới nhất, đang tạo worktree phân tích cô lập")
        _run(["git", "worktree", "add", "--detach", str(planner_worktree), f"{config.remote}/{config.base_branch}"], cwd=config.repo)
        os.symlink(config.repo / ".venv", planner_worktree / ".venv", target_is_directory=True)
        planner_provider_spec = config.planner_provider or config.provider
        implementer_provider_spec = config.implementer_provider or config.provider
        planner_codex_home, planner_claude_config_dir = _role_account_dirs(
            config, config.planner_account_profile,
        )
        implementer_codex_home, implementer_claude_config_dir = _role_account_dirs(
            config, config.implementer_account_profile,
        )
        planner_prompt = f"""You are the Planner/Reviewer for a Ceph AIOps self-repair pipeline.

Work read-only in this isolated worktree. Analyze the failure, inspect the relevant source and tests,
and produce a concrete implementation plan for another AI. Do not edit files, commit, push, deploy,
or change configuration. Include likely root cause, exact files/functions, test plan, and risks.

Observed failure (credentials already redacted):
---
{evidence}
---
"""
        if config.task_instructions:
            planner_prompt += f"""
Additional task constraints:
---
{config.task_instructions}
---
"""
        planner_provider, planner_command = _provider_command(
            planner_provider_spec, planner_worktree, planner_prompt, config.timeout_seconds,
            claude_config_dir=planner_claude_config_dir,
            codex_home=planner_codex_home,
            model=config.planner_model, mode="review",
        )
        result.planner_provider = planner_provider
        _record_transcript(
            config, speaker="Planner/Reviewer", event="plan", direction="to_ai",
            content=planner_prompt, provider=planner_provider, model=config.planner_model,
        )
        notifier.update(15, f"{planner_provider} đang phân tích và lập kế hoạch (Planner/Reviewer)")
        planner = _run(
            planner_command, cwd=planner_worktree, timeout=config.timeout_seconds,
            input_text=planner_prompt, check=False,
        )
        _record_transcript(
            config, speaker="Planner/Reviewer", event="plan", direction="from_ai",
            content=planner.stdout, provider=planner_provider, model=config.planner_model,
        )
        if planner.returncode != 0:
            raise RepairError(f"{planner_provider} planner failed ({planner.returncode}):\n{planner.stdout[-6000:]}")
        if _worktree_status(planner_worktree).strip():
            raise RepairError("Planner/Reviewer phải chạy read-only nhưng đã làm thay đổi worktree")
        plan = planner.stdout[-16_000:].strip()
        if not plan:
            raise RepairError("Planner/Reviewer không trả về kế hoạch")
        if config.allow_no_change and "VERDICT: NO_CHANGE_NEEDED" in plan:
            result.status = "NO_CHANGE"
            result.error = None
            return result

        notifier.update(25, "Đã có kế hoạch; đang tạo worktree Implementer")
        _run(["git", "worktree", "add", "-b", branch, str(worktree), f"{config.remote}/{config.base_branch}"], cwd=config.repo)
        # Reuse the tested environment without copying credentials into the worktree.
        os.symlink(config.repo / ".venv", worktree / ".venv", target_is_directory=True)
        default_instructions = """You are repairing the Ceph AIOps application in an isolated Git worktree.

Find the root cause and make the smallest production-quality code fix. Add or update a regression test.
Do not edit .env, credentials, GitHub workflows, migrations, deployment scripts, or generated/static assets.
Do not commit, push, deploy, or weaken/delete tests. You may inspect files and run focused tests.
Finish only after the working tree contains the proposed source and test changes."""
        prompt = f"""{config.task_instructions or default_instructions}

Planner/Reviewer analysis and plan:
---
{plan}
---

Observed application failure (credentials already redacted):
---
{evidence}
---
"""
        feedback = ""
        for ai_attempt in range(1, max(1, config.max_ai_attempts) + 1):
            attempt_prompt = prompt + feedback
            provider, command = _provider_command(
                implementer_provider_spec, worktree, attempt_prompt, config.timeout_seconds,
                claude_config_dir=implementer_claude_config_dir,
                codex_home=implementer_codex_home,
                model=config.implementer_model, mode="implement",
            )
            result.provider = provider
            result.implementer_provider = provider
            _record_transcript(
                config, speaker="Implementer", event="implementation", direction="to_ai",
                content=attempt_prompt, provider=provider, model=config.implementer_model,
            )
            notifier.update(15 + ai_attempt * 10, f"{provider} đang sửa code, vòng {ai_attempt}/{config.max_ai_attempts}")
            ai = _run(command, cwd=worktree, timeout=config.timeout_seconds,
                      input_text=attempt_prompt, check=False)
            _record_transcript(
                config, speaker="Implementer", event="implementation", direction="from_ai",
                content=ai.stdout, provider=provider, model=config.implementer_model,
            )
            if ai.returncode != 0:
                raise RepairError(f"{provider} failed ({ai.returncode}):\n{ai.stdout[-6000:]}")
            result.changed_files = _validate_changes(worktree)
            if config.require_changed_tests:
                _validate_proactive_test_changes(worktree, result.changed_files)
            focused_command = _focused_test_command(result.changed_files)
            if focused_command:
                notifier.update(40, "Patch hợp lệ; đang chạy test theo phạm vi thay đổi")
                focused = _run(["bash", "-lc", focused_command], cwd=worktree,
                               timeout=config.timeout_seconds, check=False)
                if focused.returncode != 0:
                    result.test_output = focused.stdout[-12_000:]
                    if _test_failure_kind(focused.stdout) == "INFRASTRUCTURE":
                        raise RepairError(f"test infrastructure failed:\n{focused.stdout[-6000:]}")
                    if ai_attempt < config.max_ai_attempts:
                        feedback = (
                            "\nThe previous patch failed its focused tests. Fix the patch; do not weaken tests.\n"
                            f"TEST OUTPUT:\n{focused.stdout[-6000:]}\n"
                        )
                        continue
                    raise RepairError(f"focused test gate failed ({focused.returncode}):\n{focused.stdout[-6000:]}")
            notifier.update(55, "Test phạm vi đã đạt; đang chạy regression gate")
            tests = _run(["bash", "-lc", config.test_command], cwd=worktree,
                         timeout=config.timeout_seconds, check=False)
            result.test_output = tests.stdout[-12_000:]
            if tests.returncode == 0:
                break
            if _test_failure_kind(tests.stdout) == "INFRASTRUCTURE":
                raise RepairError(f"test infrastructure failed:\n{tests.stdout[-6000:]}")
            if ai_attempt < config.max_ai_attempts:
                feedback = (
                    "\nThe previous patch failed regression tests. Fix the implementation while preserving existing behavior; "
                    "do not delete or weaken tests.\n"
                    f"TEST OUTPUT:\n{tests.stdout[-6000:]}\n"
                )
                continue
            raise RepairError(f"test gate failed ({tests.returncode}):\n{tests.stdout[-6000:]}")

        # Review the tested candidate with a read-only agent. A reviewer may
        # request bounded corrections, but it can never write the worktree.
        for review_round in range(1, int(config.max_review_rounds) + 1):
            result.review_rounds = review_round
            status_before = _worktree_status(worktree)
            review_prompt = f"""You are the independent Reviewer in a two-agent repair pipeline.

Inspect the current candidate diff, relevant source, tests, and the original evidence. Work read-only:
do not edit files, commit, push, deploy, or alter configuration. Check correctness, regression risk,
security, scope, and whether the tests actually cover the fix.

Original Planner/Reviewer plan:
---
{plan}
---
Candidate implementation output:
---
{result.test_output[-6000:]}
---
Original failure:
---
{evidence}
---

End your response with exactly one standalone line:
VERDICT: PASS
or
VERDICT: NEEDS_CHANGES
If changes are needed, list precise actionable corrections before that line.
"""
            reviewer_provider, reviewer_command = _provider_command(
                planner_provider_spec, worktree, review_prompt, config.timeout_seconds,
                claude_config_dir=planner_claude_config_dir,
                codex_home=planner_codex_home,
                model=config.planner_model, mode="review",
            )
            result.planner_provider = result.planner_provider or reviewer_provider
            _record_transcript(
                config, speaker="Planner/Reviewer", event="review", direction="to_ai",
                content=review_prompt, provider=reviewer_provider, model=config.planner_model,
            )
            notifier.update(58, f"{reviewer_provider} đang review candidate ({review_round}/{config.max_review_rounds})")
            review = _run(
                reviewer_command, cwd=worktree, timeout=config.timeout_seconds,
                input_text=review_prompt, check=False,
            )
            _record_transcript(
                config, speaker="Planner/Reviewer", event="review", direction="from_ai",
                content=review.stdout, provider=reviewer_provider, model=config.planner_model,
            )
            if review.returncode != 0:
                raise RepairError(f"{reviewer_provider} reviewer failed ({review.returncode}):\n{review.stdout[-6000:]}")
            if _worktree_status(worktree) != status_before:
                raise RepairError("Reviewer phải chạy read-only nhưng đã làm thay đổi candidate worktree")
            verdict = _review_verdict(review.stdout)
            if verdict == "PASS":
                break
            if review_round == int(config.max_review_rounds):
                raise RepairError(f"reviewer yêu cầu sửa nhưng đã hết {config.max_review_rounds} vòng:\n{review.stdout[-6000:]}")
            feedback = (
                "\nThe independent reviewer found issues. Apply only the necessary corrections, preserve tests, "
                "then rerun the focused and regression gates.\nREVIEW FEEDBACK:\n"
                f"{review.stdout[-8000:]}\n"
            )
            provider, command = _provider_command(
                implementer_provider_spec, worktree, prompt + feedback, config.timeout_seconds,
                claude_config_dir=implementer_claude_config_dir,
                codex_home=implementer_codex_home,
                model=config.implementer_model, mode="implement",
            )
            result.provider = provider
            result.implementer_provider = provider
            _record_transcript(
                config, speaker="Implementer", event="review-fix", direction="to_ai",
                content=prompt + feedback, provider=provider, model=config.implementer_model,
            )
            fix = _run(
                command, cwd=worktree, timeout=config.timeout_seconds,
                input_text=prompt + feedback, check=False,
            )
            _record_transcript(
                config, speaker="Implementer", event="review-fix", direction="from_ai",
                content=fix.stdout, provider=provider, model=config.implementer_model,
            )
            if fix.returncode != 0:
                raise RepairError(f"{provider} reviewer-fix failed ({fix.returncode}):\n{fix.stdout[-6000:]}")
            result.changed_files = _validate_changes(worktree)
            if config.require_changed_tests:
                _validate_proactive_test_changes(worktree, result.changed_files)
            correction_focused_command = _focused_test_command(result.changed_files)
            if correction_focused_command:
                notifier.update(60, "Implementer đã sửa theo review; đang chạy lại test theo phạm vi thay đổi")
                correction_focused = _run(
                    ["bash", "-lc", correction_focused_command], cwd=worktree,
                    timeout=config.timeout_seconds, check=False,
                )
                if correction_focused.returncode != 0:
                    result.test_output = correction_focused.stdout[-12_000:]
                    if _test_failure_kind(correction_focused.stdout) == "INFRASTRUCTURE":
                        raise RepairError(
                            f"test infrastructure failed after review fix:\n{correction_focused.stdout[-6000:]}"
                        )
                    raise RepairError(
                        f"focused test gate failed after review fix ({correction_focused.returncode}):\n"
                        f"{correction_focused.stdout[-6000:]}"
                    )
            notifier.update(62, "Test phạm vi đã đạt; đang chạy lại regression gate")
            correction_tests = _run(
                ["bash", "-lc", config.test_command], cwd=worktree,
                timeout=config.timeout_seconds, check=False,
            )
            result.test_output = correction_tests.stdout[-12_000:]
            if correction_tests.returncode != 0:
                kind = _test_failure_kind(correction_tests.stdout)
                if kind == "INFRASTRUCTURE":
                    raise RepairError(f"test infrastructure failed after review fix:\n{correction_tests.stdout[-6000:]}")
                raise RepairError(f"test gate failed after review fix ({correction_tests.returncode}):\n{correction_tests.stdout[-6000:]}")

        _run(["git", "add", "--", *result.changed_files], cwd=worktree)
        notifier.update(65, "Test đã đạt; đang tạo commit")
        _run(["git", "commit", "-m", f"fix(ai-repair): resolve error {fp}"], cwd=worktree)
        result.commit = _run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()
        if config.push:
            notifier.update(75, "Đã commit; đang push branch sửa lỗi")
            _run(["git", "push", "-u", config.remote, branch], cwd=worktree, timeout=120)
        result.status = "PUSHED" if config.push else "COMMITTED"
        if config.deploy_staging:
            if not config.push:
                raise RepairError("staging deployment requires --push")
            notifier.update(85, "Đang deploy candidate và smoke test staging")
            deploy = config.repo / "scripts/deploy/ai_repair_candidate.sh"
            deploy_env = os.environ.copy()
            if config.candidate_test_command:
                deploy_env["AI_REPAIR_CANDIDATE_TEST_COMMAND"] = config.candidate_test_command
            _run(["bash", str(deploy), branch], cwd=config.repo,
                 env=deploy_env,
                 timeout=config.timeout_seconds, check=True)
            result.status = "STAGING_VERIFIED"
        if config.promote_main:
            if not config.deploy_staging or not result.commit:
                raise RepairError("main promotion requires a staging-verified candidate")
            notifier.update(95, "Staging đã đạt; đang promote commit lên main")
            _run(["git", "push", config.remote, f"{result.commit}:refs/heads/{config.base_branch}"],
                 cwd=worktree, timeout=120)
            result.status = "PROMOTED"
    except Exception as exc:
        result.status = "FAILED"
        result.error = str(exc)
    finally:
        if planner_worktree.exists():
            _run(["git", "worktree", "remove", "--force", str(planner_worktree)], cwd=config.repo, check=False)
        if worktree.exists():
            _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=config.repo, check=False)
        shutil.rmtree(worktree_root, ignore_errors=True)
        state["attempts"][fp] = {
            **asdict(result), "attempt_count": previous_attempts + 1,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_state(config.state_file, state)
        notifier.finish(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="AI-assisted, test-gated repair branch generator")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--log", action="append", type=Path, default=[])
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--provider", choices=("auto", "codex", "claude"))
    parser.add_argument("--planner-provider", choices=("auto", "codex", "claude"))
    parser.add_argument("--planner-model", default=None)
    parser.add_argument("--planner-account-profile", default="configured")
    parser.add_argument("--implementer-provider", choices=("auto", "codex", "claude"))
    parser.add_argument("--implementer-model", default=None)
    parser.add_argument("--implementer-account-profile", default="configured")
    parser.add_argument("--max-review-rounds", type=int, default=None)
    parser.add_argument("--instructions-file", type=Path)
    parser.add_argument("--test-command", default=None)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--state-file", type=Path, default=Path("/var/lib/ceph-ai/code-repair-state.json"))
    parser.add_argument("--task-kind", default="application-repair")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--deploy-staging", action="store_true")
    parser.add_argument("--promote-main", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    logs = args.log or list(Path("/var/log").glob("ceph-ai-*.log"))
    evidence = args.evidence_file.read_text(errors="replace") if args.evidence_file else extract_latest_error(logs)
    if not evidence:
        print(json.dumps({"status": "NO_ERROR_FOUND"}))
        return 0
    config = RepairConfig(
                          repo=args.repo.resolve(),
                          provider=args.provider or app_settings.code_repair_provider,
                          planner_provider=args.planner_provider or app_settings.code_repair_planner_provider,
                          planner_model=args.planner_model if args.planner_model is not None else app_settings.code_repair_planner_model,
                          planner_account_profile=args.planner_account_profile,
                          implementer_provider=args.implementer_provider or app_settings.code_repair_implementer_provider,
                          implementer_model=args.implementer_model if args.implementer_model is not None else app_settings.code_repair_implementer_model,
                          implementer_account_profile=args.implementer_account_profile,
                          max_review_rounds=(args.max_review_rounds if args.max_review_rounds is not None else DEFAULT_REPAIR_REVIEW_ROUNDS),
                          task_instructions=(args.instructions_file.read_text(errors="replace") if args.instructions_file else None),
                          task_kind=args.task_kind,
                          test_command=args.test_command or app_settings.code_repair_test_command,
                          timeout_seconds=args.timeout,
                          push=args.push, deploy_staging=args.deploy_staging,
                          promote_main=args.promote_main, state_file=args.state_file)
    result = run_repair(evidence, config, force=args.force)
    print(json.dumps(asdict(result), ensure_ascii=False, default=str))
    return 0 if result.status in {
        "PUSHED", "COMMITTED", "STAGING_VERIFIED", "PROMOTED", "SKIPPED_DUPLICATE"
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
