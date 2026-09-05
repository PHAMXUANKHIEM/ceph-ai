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


ALLOWED_PREFIXES = ("config/", "dashboard/", "scripts/", "shared/", "tests/", "watcher/", "worker/")
FORBIDDEN_PREFIXES = (".env", ".git", ".github/", ".codex", "alembic/versions/", "scripts/deploy/")
ERROR_RE = re.compile(r"(Traceback \(most recent call last\):|\b(?:CRITICAL|ERROR)\b)")
SECRET_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|secret(?:[_-]?key)?|password|token|"
    r"access[_-]?token|refresh[_-]?token|authorization)\b\s*[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
BEARER_RE = re.compile(r"(?i)(\bauthorization\b\s*[:=]\s*bearer\s+)\S+")
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL,
)
DIFF_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|secret|password|token)\s*[=:]\s*"
    r"[\"'][A-Za-z0-9_./+:-]{16,}[\"']"
)
MAX_REVIEW_ROUNDS = 5


class RepairError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepairConfig:
    repo: Path
    remote: str = "origin"
    base_branch: str = "main"
    branch_prefix: str = "ai-repair/"
    provider: str = "auto"
    # The two-agent path deliberately uses independent CLI processes.  This
    # means `codex/codex` is valid, as are `claude/codex` and
    # `codex:some-model/claude:another-model` when the installed CLIs expose
    # those model ids.  Keeping the old `provider` field preserves the
    # single-provider command-line/API contract.
    planner_provider: str | None = None
    implementer_provider: str | None = None
    planner_model: str = ""
    implementer_model: str = ""
    max_review_rounds: int = 2
    test_command: str = "PYTHONPATH=. .venv/bin/pytest -q"
    timeout_seconds: int = 1800
    push: bool = False
    deploy_staging: bool = False
    promote_main: bool = False
    state_file: Path = Path("/var/lib/ceph-ai/code-repair-state.json")


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

    def __init__(self, evidence: str, branch: str, *, interval_seconds: int = 600) -> None:
        self.evidence = " ".join(evidence.split())[:700]
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
        block = redact_evidence(block)
        candidates.append((path.stat().st_mtime, f"Source log: {path.name}\n{block}"))
    return max(candidates, default=(0, ""), key=lambda item: item[0])[1] or None


def redact_evidence(text: str) -> str:
    """Remove common credentials before evidence reaches an AI or notifier."""
    redacted = PRIVATE_KEY_RE.sub("<private-key-redacted>", text)
    redacted = BEARER_RE.sub(r"\1<redacted>", redacted)
    return SECRET_RE.sub(lambda m: m.group(1) + "<redacted>", redacted)


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


def _provider_and_inline_model(provider: str) -> tuple[str, str]:
    """Split `provider:model-id` without making model ids mandatory.

    Model ids exposed by Codex/Claude can contain punctuation, so only the
    first colon is significant.  A plain provider remains fully backwards
    compatible with the old repair command.
    """
    name, separator, inline_model = provider.partition(":")
    return name.strip().lower(), inline_model.strip() if separator else ""


def _provider_command(
    provider: str,
    worktree: Path,
    prompt: str,
    timeout: int,
    *,
    claude_config_dir: Path | None = None,
    codex_home: Path | None = None,
    model: str = "",
    mode: str = "implement",
) -> tuple[str, list[str]]:
    if mode not in {"implement", "review"}:
        raise RepairError(f"unknown AI coding mode: {mode!r}")
    provider, inline_model = _provider_and_inline_model(provider)
    selected_model = model.strip() or inline_model
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
        if mode == "review":
            # The reviewer must not be able to mutate the candidate.  The
            # current Codex CLI makes --approve-for-me mutually exclusive
            # with --sandbox, so read-only is intentionally a separate mode.
            command = [codex, "exec", "--ephemeral", "--sandbox", "read-only", "-C", str(worktree), "-"]
        else:
            # approve-for-me routes implementation commands through Codex's
            # workspace-write automatic reviewer.
            command = [codex, "exec", "--ephemeral", "--approve-for-me", "-C", str(worktree), "-"]
        if selected_model:
            command[-1:-1] = ["--model", selected_model]
        if codex_home:
            command = ["env", f"CODEX_HOME={codex_home}", *command]
        return provider, command
    if provider == "claude" and claude:
        permission_mode = "plan" if mode == "review" else "acceptEdits"
        command = [claude, "-p", "--permission-mode", permission_mode, "--no-session-persistence"]
        if selected_model:
            command.extend(["--model", selected_model])
        command.append(prompt)
        if claude_config_dir:
            # The dashboard stores its CLI session in a repo-local, gitignored
            # directory rather than root's default ~/.claude account.
            command = ["env", f"CLAUDE_CONFIG_DIR={claude_config_dir}", "DISABLE_AUTOUPDATER=1", *command]
        return provider, command
    raise RepairError(f"AI coding provider {provider!r} is unavailable or not authenticated")


def _candidate_entries(worktree: Path) -> list[tuple[str, str]]:
    """Return status/path pairs, expanding every untracked file.

    Without `--untracked-files=all`, Git can report an untracked directory as
    one path.  Staging that directory later would bypass the path and secret
    checks for files nested inside it.
    """
    output = _run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=worktree,
    ).stdout
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        if len(line) <= 3:
            continue
        status, path = line[:2], line[3:]
        # Git quotes unusual paths using its C-style quoting.  Failing closed
        # is safer than checking a path different from the one Git will add.
        if path.startswith('"'):
            raise RepairError("AI changed a path with unsafe Git quoting")
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        if path != ".venv":
            entries.append((status, path))
    return entries


def _candidate_paths(worktree: Path) -> list[str]:
    """Return candidate changes, ignoring the supervisor's `.venv` symlink."""
    return [path for _, path in _candidate_entries(worktree)]


def _validate_candidate_path(worktree: Path, relative_path: str) -> Path:
    """Validate path scope and reject sensitive files/symlink escapes."""
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise RepairError(f"AI changed an unsafe relative path: {relative_path}")
    if any(part == ".env" or part.startswith(".env.") for part in path.parts):
        raise RepairError(f"AI changed a dotenv file: {relative_path}")
    if path.name in {"auth.json", "id_rsa", "id_ed25519"} or path.suffix.lower() in {
        ".pem", ".key", ".p12", ".pfx",
    }:
        raise RepairError(f"AI changed a credential file: {relative_path}")
    absolute = (worktree / path).resolve(strict=False)
    worktree_real = worktree.resolve()
    try:
        absolute.relative_to(worktree_real)
    except ValueError as exc:
        raise RepairError(f"AI changed a path outside the worktree: {relative_path}") from exc
    cursor = worktree / path
    while cursor != worktree:
        if cursor.is_symlink():
            raise RepairError(f"AI changed a symlink path: {relative_path}")
        cursor = cursor.parent
    return worktree / path


def _run_agent(
    provider: str,
    worktree: Path,
    prompt: str,
    config: RepairConfig,
    *,
    model: str = "",
    mode: str = "implement",
) -> tuple[str, str]:
    """Run one isolated agent and return its provider and textual response."""
    selected_provider, command = _provider_command(
        provider,
        worktree,
        prompt,
        config.timeout_seconds,
        claude_config_dir=config.repo / ".claude-account",
        codex_home=config.repo / ".codex-account",
        model=model,
        mode=mode,
    )
    ai = _run(
        command,
        cwd=worktree,
        timeout=config.timeout_seconds,
        input_text=prompt if selected_provider == "codex" else None,
        check=False,
    )
    if ai.returncode != 0:
        raise RepairError(f"{selected_provider} {mode} failed ({ai.returncode}):\n{ai.stdout[-6000:]}")
    response = ai.stdout.strip()
    if not response:
        raise RepairError(f"{selected_provider} {mode} returned no response")
    return selected_provider, response


def _review_verdict(response: str) -> tuple[str, str]:
    """Parse the review protocol; missing/invalid verdicts fail closed."""
    matches = re.findall(r"(?im)^\s*VERDICT\s*:\s*(PASS|NEEDS_CHANGES)\s*$", response)
    if not matches:
        raise RepairError("AI reviewer did not return a valid VERDICT: PASS or NEEDS_CHANGES")
    verdict = matches[-1]
    return verdict, response[-12_000:]


def _provider_cli_value(value: str) -> str:
    """Validate a CLI provider while allowing the shorthand `provider:model`."""
    provider, _ = _provider_and_inline_model(value)
    if provider not in {"auto", "codex", "claude"}:
        raise argparse.ArgumentTypeError("provider must be auto, codex, or claude")
    return value


def _review_rounds_cli_value(value: str) -> int:
    try:
        rounds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("max-review-rounds must be an integer") from exc
    if not 0 <= rounds <= MAX_REVIEW_ROUNDS:
        raise argparse.ArgumentTypeError(
            f"max-review-rounds must be between 0 and {MAX_REVIEW_ROUNDS}"
        )
    return rounds


def _validate_changes(worktree: Path) -> list[str]:
    entries = _candidate_entries(worktree)
    files = [path for _, path in entries]
    if not files:
        raise RepairError("AI did not produce a patch")
    invalid = [
        path for path in files
        if not path.startswith(ALLOWED_PREFIXES) or path.startswith(FORBIDDEN_PREFIXES)
    ]
    if invalid:
        raise RepairError(f"AI changed paths outside the repair allowlist: {invalid}")
    candidate_paths = {
        relative_path: _validate_candidate_path(worktree, relative_path)
        for _, relative_path in entries
    }
    # Include both the index and the working tree. An agent is instructed not
    # to stage, but validation must remain safe if it does; plain `git diff`
    # omits already-staged additions/changes.
    diff = _run(["git", "diff", "HEAD", "--", *files], cwd=worktree).stdout
    if DIFF_SECRET_RE.search(diff):
        raise RepairError("candidate diff appears to contain a credential")
    for status, relative_path in entries:
        candidate = candidate_paths[relative_path]
        # `git diff` does not include untracked files. Scan their complete
        # content before the later recursive `git add` can stage them.
        if "?" in status and candidate.is_file():
            try:
                content = candidate.read_text(errors="replace")
            except OSError as exc:
                raise RepairError(f"cannot inspect candidate file: {relative_path}") from exc
            # SECRET_RE intentionally accepts unquoted values for log
            # redaction, but that would falsely reject normal code such as
            # `token = settings.token`. For source files, reject literal
            # quoted credentials and PEM blocks instead.
            if PRIVATE_KEY_RE.search(content) or DIFF_SECRET_RE.search(content):
                raise RepairError(f"candidate file appears to contain a credential: {relative_path}")
    return files


def run_repair(evidence: str, config: RepairConfig, *, force: bool = False) -> RepairResult:
    evidence = redact_evidence(evidence)
    fp = fingerprint(evidence)
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
    planner_worktree = worktree_root / "planner"
    worktree = worktree_root / "repo"
    try:
        if not 0 <= config.max_review_rounds <= MAX_REVIEW_ROUNDS:
            raise RepairError(
                f"max_review_rounds must be between 0 and {MAX_REVIEW_ROUNDS}"
            )
        _run(["git", "fetch", config.remote, config.base_branch], cwd=config.repo)
        notifier.update(10, "Đã lấy source mới nhất, đang tạo worktree cô lập")
        # Planning happens in a disposable read-only worktree.  Even if a
        # CLI ignores its permission mode and writes files, those files can
        # never leak into the candidate branch.
        _run(
            ["git", "worktree", "add", "--detach", str(planner_worktree),
             f"{config.remote}/{config.base_branch}"],
            cwd=config.repo,
        )
        os.symlink(config.repo / ".venv", planner_worktree / ".venv", target_is_directory=True)
        planner_spec = config.planner_provider or config.provider
        implementer_spec = config.implementer_provider or config.provider
        planner_prompt = f"""You are the planning and architecture agent for the Ceph AIOps application.

Observed application failure (credentials already redacted):
---
{evidence}
---

Inspect the repository and tests in this isolated worktree. Determine the
most likely root cause and write a concise implementation plan for another AI
agent. Identify exact files/functions, a minimal safe fix, regression tests,
and any compatibility or security risks. Do not edit files, commit, push,
deploy, or invent credentials. Return plain text only.
"""
        planner_provider, plan = _run_agent(
            planner_spec, planner_worktree, planner_prompt, config,
            model=config.planner_model, mode="review",
        )
        result.planner_provider = planner_provider
        if _candidate_paths(planner_worktree):
            raise RepairError("planning agent modified its read-only worktree")
        notifier.update(20, f"{planner_provider} đã lập kế hoạch; đang tạo worktree implement")
        _run(["git", "worktree", "add", "-b", branch, str(worktree), f"{config.remote}/{config.base_branch}"], cwd=config.repo)
        # Reuse the tested environment without copying credentials into the worktree.
        os.symlink(config.repo / ".venv", worktree / ".venv", target_is_directory=True)
        implement_prompt = f"""You are the implementation agent repairing the Ceph AIOps application in an isolated Git worktree.

Observed application failure (credentials already redacted):
---
{evidence}
---

Architecture agent's plan (verify it against the actual source before using it):
---
{plan[-12000:]}
---

Find the root cause and make the smallest production-quality code fix. Add or update a regression test.
Do not edit .env, credentials, GitHub workflows, migrations, deployment scripts, or generated/static assets.
Do not commit, push, deploy, or weaken/delete tests. You may inspect files and run focused tests.
Finish only after the working tree contains the proposed source and test changes.
"""
        implementer_provider, implementation_response = _run_agent(
            implementer_spec, worktree, implement_prompt, config,
            model=config.implementer_model, mode="implement",
        )
        result.provider = implementer_provider  # backwards-compatible summary field
        result.implementer_provider = implementer_provider
        result.changed_files = _validate_changes(worktree)
        notifier.update(40, f"{implementer_provider} đã tạo patch; đang review độc lập")

        # The planner doubles as a reviewer only by role, not by shared
        # conversation: every review is a fresh process with the candidate
        # diff available on disk.  This prevents hidden context from making
        # the reviewer rubber-stamp the implementer's work.
        review_provider = planner_spec
        review_passed = config.max_review_rounds == 0
        for review_number in range(1, config.max_review_rounds + 1):
            result.review_rounds = review_number
            review_prompt = f"""You are the independent review agent for a Ceph AIOps code repair.

Observed failure:
---
{evidence}
---

Original implementation plan:
---
{plan[-8000:]}
---

The implementation agent reported:
---
{implementation_response[-4000:]}
---

Inspect the current worktree and its git diff, source, and tests. Check the
root-cause fix, regression coverage, backwards compatibility, security, and
whether the patch obeys the repair scope. Do not edit any file, commit, push,
deploy, or approve a suspicious change. If changes are needed, give precise
file/function-level instructions to the implementation agent.

End with exactly one standalone line:
VERDICT: PASS
or:
VERDICT: NEEDS_CHANGES
"""
            reviewer_provider, review_response = _run_agent(
                review_provider, worktree, review_prompt, config,
                model=config.planner_model, mode="review",
            )
            if _candidate_paths(worktree):
                raise RepairError("review agent modified the candidate worktree")
            verdict, review_feedback = _review_verdict(review_response)
            if verdict == "PASS":
                review_passed = True
                notifier.update(50, f"{reviewer_provider} đã review đạt; đang chạy test")
                break
            if review_number >= config.max_review_rounds:
                raise RepairError(
                    f"review agent requested changes after {review_number} rounds:\n{review_feedback[-6000:]}"
                )
            notifier.update(45, f"{reviewer_provider} yêu cầu sửa; đang chạy lại implementer (vòng {review_number + 1})")
            fix_prompt = f"""You are the implementation agent continuing a bounded repair loop.

Keep the existing fix and apply only the review corrections below. Re-check
the source and tests yourself. Do not edit .env, credentials, workflows,
migrations, deployment scripts, or weaken/delete tests. Do not commit, push,
or deploy.

Review feedback:
---
{review_feedback[-12000:]}
---

Finish with the corrected source and regression tests in the worktree.
"""
            implementer_provider, implementation_response = _run_agent(
                implementer_spec, worktree, fix_prompt, config,
                model=config.implementer_model, mode="implement",
            )
            result.implementer_provider = implementer_provider
            result.provider = implementer_provider
            result.changed_files = _validate_changes(worktree)

        if not review_passed:
            raise RepairError("AI review did not pass")

        notifier.update(55, "Patch đã qua review; đang chạy test")
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
        result.error = str(exc)
    finally:
        if planner_worktree.exists():
            _run(["git", "worktree", "remove", "--force", str(planner_worktree)], cwd=config.repo, check=False)
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
    parser.add_argument(
        "--provider", type=_provider_cli_value, default=None,
        help="Ghi đè provider cho cả hai vai trò; mặc định đọc Settings",
    )
    parser.add_argument(
        "--planner-provider", type=_provider_cli_value,
        help="AI phân tích/review; mặc định dùng Settings (hoặc --provider)",
    )
    parser.add_argument(
        "--implementer-provider", type=_provider_cli_value,
        help="AI sửa code; mặc định dùng Settings (hoặc --provider)",
    )
    parser.add_argument("--planner-model", default=None, help="Ghi đè model cho planner/reviewer")
    parser.add_argument("--implementer-model", default=None, help="Ghi đè model cho implementer")
    parser.add_argument(
        "--max-review-rounds", type=_review_rounds_cli_value, default=None,
        help="Ghi đè số vòng review/sửa (Settings mặc định: 2; 0 để bỏ review)",
    )
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
    if args.provider is not None:
        planner_provider = args.planner_provider or args.provider
        implementer_provider = args.implementer_provider or args.provider
    else:
        planner_provider = args.planner_provider or app_settings.code_repair_planner_provider
        implementer_provider = args.implementer_provider or app_settings.code_repair_implementer_provider
    planner_model = (
        args.planner_model if args.planner_model is not None
        else app_settings.code_repair_planner_model
    )
    implementer_model = (
        args.implementer_model if args.implementer_model is not None
        else app_settings.code_repair_implementer_model
    )
    max_review_rounds = (
        args.max_review_rounds if args.max_review_rounds is not None
        else app_settings.code_repair_max_review_rounds
    )
    config = RepairConfig(repo=args.repo.resolve(), provider=implementer_provider,
                          planner_provider=planner_provider,
                          implementer_provider=implementer_provider,
                          planner_model=planner_model,
                          implementer_model=implementer_model,
                          max_review_rounds=max_review_rounds,
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
