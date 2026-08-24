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

from shared.telegram_alerts import send_code_repair_alert


ALLOWED_PREFIXES = ("config/", "dashboard/", "scripts/", "shared/", "tests/", "watcher/", "worker/")
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


class RepairError(RuntimeError):
    pass


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


@dataclass(frozen=True)
class RepairConfig:
    repo: Path
    remote: str = "origin"
    base_branch: str = "main"
    branch_prefix: str = "ai-repair/"
    provider: str = "auto"
    test_command: str = "PYTHONPATH=. .venv/bin/pytest -q"
    timeout_seconds: int = 1800
    push: bool = False
    deploy_staging: bool = False
    promote_main: bool = False
    state_file: Path = Path("/var/lib/ceph-ai/code-repair-state.json")
    task_kind: str = "application-repair"
    task_instructions: str | None = None


@dataclass
class RepairResult:
    status: str
    fingerprint: str
    branch: str | None = None
    commit: str | None = None
    provider: str | None = None
    changed_files: list[str] | None = None
    test_output: str = ""
    error: str | None = None


class RepairProgressNotifier:
    """Telegram lifecycle reporter with a ten-minute in-progress heartbeat."""

    def __init__(self, evidence: str, branch: str, *, interval_seconds: int = 600) -> None:
        self.evidence = summarize_evidence(evidence)
        self.branch = branch
        self.interval_seconds = interval_seconds
        self.percent = 5
        self.stage = "Đang chuẩn bị worktree và branch sửa lỗi"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
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
        while not self._stop.wait(self.interval_seconds):
            send_code_repair_alert(
                "⏳ AI CODE REPAIR ĐANG CHẠY\n"
                f"Lỗi: {self.evidence}\n"
                f"Đang sửa: {self.stage}\n"
                f"Tiến trình: {self.percent}%\n"
                "Hệ thống sẽ cập nhật lại sau 10 phút nếu chưa hoàn tất."
            )

    def finish(self, result: RepairResult) -> None:
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
         check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args, cwd=cwd, text=True, input=input_text, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=timeout, env=os.environ.copy(),
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


def _provider_command(provider: str, worktree: Path, prompt: str, timeout: int,
                      *, claude_config_dir: Path | None = None,
                      codex_home: Path | None = None) -> tuple[str, list[str]]:
    codex = shutil.which("codex")
    claude = shutil.which("claude")
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
        # Current Codex CLI makes --approve-for-me mutually exclusive with
        # --sandbox; approve-for-me itself routes commands through its
        # workspace-write automatic reviewer.
        command = [codex, "exec", "--ephemeral", "--approve-for-me", "-C", str(worktree), "-"]
        if codex_home:
            command = ["env", f"CODEX_HOME={codex_home}", *command]
        return provider, command
    if provider == "claude" and claude:
        command = [claude, "-p", "--permission-mode", "acceptEdits", "--no-session-persistence", prompt]
        if claude_config_dir:
            # The dashboard stores its CLI session in a repo-local, gitignored
            # directory rather than root's default ~/.claude account.
            command = ["env", f"CLAUDE_CONFIG_DIR={claude_config_dir}", "DISABLE_AUTOUPDATER=1", *command]
        return provider, command
    raise RepairError(f"AI coding provider {provider!r} is unavailable or not authenticated")


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
    diff = _run(["git", "diff", "--", *files], cwd=worktree).stdout
    if DIFF_SECRET_RE.search(diff):
        raise RepairError("candidate diff appears to contain a credential")
    return files


def run_repair(evidence: str, config: RepairConfig, *, force: bool = False) -> RepairResult:
    fingerprint_input = evidence if config.task_kind == "application-repair" else f"{config.task_kind}\n{evidence}"
    fp = fingerprint(fingerprint_input)
    state = _load_state(config.state_file)
    previous = state.setdefault("attempts", {}).get(fp)
    if previous and not force:
        return RepairResult(status="SKIPPED_DUPLICATE", fingerprint=fp, branch=previous.get("branch"))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"{config.branch_prefix}{stamp}-{fp[:8]}"
    result = RepairResult(status="FAILED", fingerprint=fp, branch=branch)
    notifier = RepairProgressNotifier(evidence, branch)
    notifier.start()
    state["attempts"][fp] = {"status": "RUNNING", "branch": branch, "started_at": stamp}
    _save_state(config.state_file, state)
    worktree_root = Path(tempfile.mkdtemp(prefix="ceph-ai-repair-"))
    worktree = worktree_root / "repo"
    try:
        _run(["git", "fetch", config.remote, config.base_branch], cwd=config.repo)
        notifier.update(10, "Đã lấy source mới nhất, đang tạo worktree cô lập")
        _run(["git", "worktree", "add", "-b", branch, str(worktree), f"{config.remote}/{config.base_branch}"], cwd=config.repo)
        # Reuse the tested environment without copying credentials into the worktree.
        os.symlink(config.repo / ".venv", worktree / ".venv", target_is_directory=True)
        default_instructions = """You are repairing the Ceph AIOps application in an isolated Git worktree.

Find the root cause and make the smallest production-quality code fix. Add or update a regression test.
Do not edit .env, credentials, GitHub workflows, migrations, deployment scripts, or generated/static assets.
Do not commit, push, deploy, or weaken/delete tests. You may inspect files and run focused tests.
Finish only after the working tree contains the proposed source and test changes."""
        prompt = f"""{config.task_instructions or default_instructions}

Observed application failure (credentials already redacted):
---
{evidence}
---
"""
        provider, command = _provider_command(
            config.provider, worktree, prompt, config.timeout_seconds,
            claude_config_dir=config.repo / ".claude-account",
            codex_home=config.repo / ".codex-account",
        )
        result.provider = provider
        notifier.update(20, f"{provider} đang phân tích lỗi và sửa code")
        ai = _run(command, cwd=worktree, timeout=config.timeout_seconds,
                  input_text=prompt if provider == "codex" else None, check=False)
        if ai.returncode != 0:
            raise RepairError(f"{provider} failed ({ai.returncode}):\n{ai.stdout[-6000:]}")
        result.changed_files = _validate_changes(worktree)
        notifier.update(45, "Patch đã tạo; guardrail hợp lệ, đang chạy test")
        tests = _run(["bash", "-lc", config.test_command], cwd=worktree,
                     timeout=config.timeout_seconds, check=False)
        result.test_output = tests.stdout[-12_000:]
        if tests.returncode != 0:
            raise RepairError(f"test gate failed ({tests.returncode}):\n{tests.stdout[-6000:]}")
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
            _run(["bash", str(deploy), branch], cwd=config.repo,
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
        if worktree.exists():
            _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=config.repo, check=False)
        shutil.rmtree(worktree_root, ignore_errors=True)
        state["attempts"][fp] = {**asdict(result), "finished_at": datetime.now(timezone.utc).isoformat()}
        _save_state(config.state_file, state)
        notifier.finish(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="AI-assisted, test-gated repair branch generator")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--log", action="append", type=Path, default=[])
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--provider", choices=("auto", "codex", "claude"), default="auto")
    parser.add_argument("--test-command", default="PYTHONPATH=. .venv/bin/pytest -q")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--state-file", type=Path, default=Path("/var/lib/ceph-ai/code-repair-state.json"))
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
    config = RepairConfig(repo=args.repo.resolve(), provider=args.provider,
                          test_command=args.test_command, timeout_seconds=args.timeout,
                          push=args.push, deploy_staging=args.deploy_staging,
                          promote_main=args.promote_main, state_file=args.state_file)
    result = run_repair(evidence, config, force=args.force)
    print(json.dumps(asdict(result), ensure_ascii=False, default=str))
    return 0 if result.status in {
        "PUSHED", "COMMITTED", "STAGING_VERIFIED", "PROMOTED", "SKIPPED_DUPLICATE"
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
