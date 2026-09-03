"""Server-side Claude Code authentication and non-interactive inference."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path

from config.settings import settings
from shared.ai_observability import record_ai_usage


class ClaudeCLIError(RuntimeError):
    pass


CLAUDE_INSTALL_COMMAND = "curl -fsSL https://claude.ai/install.sh | bash"
_install_lock = asyncio.Lock()
_login_lock = asyncio.Lock()
_login_process: asyncio.subprocess.Process | None = None
_login_url: str | None = None
_login_config_dir: Path | None = None
_login_drain_task: asyncio.Task[tuple[bytes, bytes | None]] | None = None
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_URL_RE = re.compile(r"https?://[^\s<>\]\[()]+")


def claude_executable() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "claude"
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _config_dir(config_dir: Path | None = None) -> Path:
    value = config_dir or Path(settings.claude_config_dir).expanduser()
    if not value.is_absolute():
        value = Path(__file__).resolve().parent.parent / value
    value.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        value.chmod(0o700)
    except OSError:
        pass
    return value


def _env(config_dir: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(_config_dir(config_dir))
    env["DISABLE_AUTOUPDATER"] = "1"
    return env


async def install_claude_cli() -> dict:
    async with _install_lock:
        existing = claude_executable()
        if existing:
            return {"installed": True, "path": existing, "already_installed": True}
        if shutil.which("curl") is None or shutil.which("sh") is None:
            raise ClaudeCLIError("Server cần có curl và sh để cài Claude Code")
        process = await asyncio.create_subprocess_exec(
            "sh", "-c", CLAUDE_INSTALL_COMMAND,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 180)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ClaudeCLIError("Cài Claude Code quá 3 phút và đã bị dừng") from exc
        installed = claude_executable()
        text = output.decode(errors="replace")[-6000:]
        if process.returncode or not installed:
            raise ClaudeCLIError(f"Cài Claude Code thất bại: {text.strip() or process.returncode}")
        return {"installed": True, "path": installed, "already_installed": False}


async def claude_status(config_dir: Path | None = None) -> dict:
    executable = claude_executable()
    if not executable:
        return {"installed": False, "authenticated": False}
    process = await asyncio.create_subprocess_exec(
        executable, "auth", "status", "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_env(config_dir),
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 12)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ClaudeCLIError("Claude CLI không phản hồi khi kiểm tra đăng nhập") from exc
    clean = _ANSI_RE.sub("", output.decode(errors="replace")).strip()
    try:
        data = json.loads(clean)
    except json.JSONDecodeError:
        data = {}
    authenticated = bool(data.get("loggedIn") or data.get("authenticated"))
    return {
        "installed": True,
        "authenticated": authenticated,
        "email": data.get("email"),
        "auth_method": data.get("authMethod") or data.get("auth_method"),
        "error": None if authenticated or process.returncode in (0, 1) else clean[-1000:],
        # Some Claude CLI/account variants expose quota windows in auth
        # status; keep them when present. Others deliberately omit them.
        "rate_limits": data.get("rateLimits") or data.get("rate_limits"),
    }


async def start_claude_login(config_dir: Path | None = None) -> dict:
    """Start Claude's OAuth login and return the browser URL it prints."""
    global _login_process, _login_url, _login_config_dir, _login_drain_task
    target_config_dir = _config_dir(config_dir)
    async with _login_lock:
        if _login_process and _login_process.returncode is None and _login_url:
            if _login_config_dir != target_config_dir:
                raise ClaudeCLIError("Đang có phiên đăng nhập Claude khác; hãy hoàn tất hoặc chờ phiên đó kết thúc")
            return {"verification_url": _login_url}
        executable = claude_executable()
        if not executable:
            raise ClaudeCLIError("Chưa cài Claude Code CLI trên server")
        _login_process = await asyncio.create_subprocess_exec(
            executable, "auth", "login",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**_env(target_config_dir), "BROWSER": "true"},
        )
        _login_config_dir = target_config_dir
        output = ""
        assert _login_process.stdout
        try:
            while len(output) < 16000:
                chunk = await asyncio.wait_for(_login_process.stdout.read(512), 30)
                if not chunk:
                    break
                output += chunk.decode(errors="replace")
                urls = _URL_RE.findall(_ANSI_RE.sub("", output))
                if urls:
                    _login_url = urls[-1].rstrip(".,;'")
                    _login_drain_task = asyncio.create_task(_login_process.communicate())
                    return {"verification_url": _login_url}
        except asyncio.TimeoutError as exc:
            _login_process.terminate()
            await _login_process.wait()
            _login_process = None
            raise ClaudeCLIError("Claude CLI không in URL đăng nhập trong 30 giây") from exc
        rc = await _login_process.wait()
        _login_process = None
        raise ClaudeCLIError(f"Không lấy được URL đăng nhập Claude (exit {rc}): {_ANSI_RE.sub('', output)[-2000:]}")


async def submit_claude_authentication_code(authentication_code: str, config_dir: Path | None = None) -> dict:
    """Submit the browser-issued code to the active ``claude auth login`` process."""
    global _login_process, _login_url, _login_config_dir, _login_drain_task
    code = authentication_code.strip()
    if not code:
        raise ClaudeCLIError("Vui lòng nhập Authentication code")

    async with _login_lock:
        process = _login_process
        active_config_dir = _login_config_dir or _config_dir(config_dir)
        if process is None or process.returncode is not None or process.stdin is None:
            raise ClaudeCLIError("Phiên đăng nhập Claude không còn hiệu lực; hãy bắt đầu lại")
        try:
            process.stdin.write((code + "\n").encode())
            await process.stdin.drain()
            process.stdin.close()
            drain_task = _login_drain_task
            if drain_task is not None:
                output, _ = await asyncio.wait_for(asyncio.shield(drain_task), 60)
            else:
                output, _ = await asyncio.wait_for(process.communicate(), 60)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ClaudeCLIError("Claude không xác nhận Authentication code trong 60 giây") from exc
        except (BrokenPipeError, ConnectionError) as exc:
            raise ClaudeCLIError("Phiên đăng nhập Claude đã kết thúc; hãy bắt đầu lại") from exc
        finally:
            _login_process = None
            _login_url = None
            _login_config_dir = None
            _login_drain_task = None

    if process.returncode:
        # Do not include CLI output here: some versions echo terminal input,
        # which could expose the one-time authentication code in the UI/logs.
        raise ClaudeCLIError("Authentication code không được Claude chấp nhận")
    status = await claude_status(config_dir=active_config_dir)
    if not status.get("authenticated"):
        raise ClaudeCLIError("Claude CLI chưa xác nhận đăng nhập; hãy thử tạo phiên mới")
    return status


async def claude_logout(config_dir: Path | None = None) -> None:
    executable = claude_executable()
    if not executable:
        return
    process = await asyncio.create_subprocess_exec(
        executable, "auth", "logout", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=_env(config_dir),
    )
    output, _ = await asyncio.wait_for(process.communicate(), 15)
    if process.returncode:
        raise ClaudeCLIError(_ANSI_RE.sub("", output.decode(errors="replace")).strip())


async def run_claude_prompt(prompt: str, *, timeout: float = 120) -> str:
    executable = claude_executable()
    if not executable:
        raise ClaudeCLIError("Chưa cài Claude Code CLI trên server")
    # Keep the prompt out of argv: it can contain operator data and can exceed
    # the OS argument-size limit. Claude accepts the prompt on stdin when -p
    # is supplied without a positional prompt argument.
    command = [executable, "-p", "--output-format", "json", "--tools", ""]
    model = settings.claude_chat_model.strip()
    if model and model != "default":
        command.extend(["--model", model])
    effort = settings.claude_chat_effort.strip().lower()
    if effort and effort != "auto":
        command.extend(["--effort", effort])
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_env(), cwd="/tmp",
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(prompt.encode()), timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ClaudeCLIError("Claude phản hồi quá thời gian cho phép") from exc
    if process.returncode:
        detail = _ANSI_RE.sub("", (stderr or stdout).decode(errors="replace")).strip()
        raise ClaudeCLIError(detail[-3000:] or f"Claude CLI exit {process.returncode}")
    raw = stdout.decode(errors="replace").strip()
    try:
        data = json.loads(raw)
        record_ai_usage(data)
        return str(data.get("result") or data.get("text") or "").strip()
    except json.JSONDecodeError:
        return _ANSI_RE.sub("", raw).strip()
