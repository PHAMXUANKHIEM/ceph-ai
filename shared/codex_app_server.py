"""Small async client for ``codex app-server``'s JSONL protocol.

The dashboard deliberately talks to the supported app-server boundary and
never reads ``auth.json`` or handles ChatGPT refresh tokens itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pty
import re
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from config.settings import settings
from shared.ai_observability import record_ai_usage


logger = logging.getLogger(__name__)


class CodexAppServerError(RuntimeError):
    pass


ToolHandler = Callable[[str, dict], Awaitable[tuple[str, bool]]]

CODEX_INSTALL_COMMAND = "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
# Codex emits one JSON object per line. Account/model metadata and tool results
# can exceed asyncio's default 64 KiB StreamReader limit; a bounded larger
# limit prevents the reader task from dying and leaving the app-server stuck.
CODEX_APP_SERVER_STREAM_LIMIT = 4 * 1024 * 1024
_install_lock = asyncio.Lock()
_device_login_lock = asyncio.Lock()
_device_login_process: asyncio.subprocess.Process | None = None
_device_login_result: dict | None = None
_device_login_drain_task: asyncio.Task | None = None

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_URL_RE = re.compile(r"https?://[^\s<>\]\[()]+")
_DEVICE_CODE_RE = re.compile(
    r"\b(?=[A-Z0-9-]*\d)[A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8})+\b",
    re.IGNORECASE,
)


def codex_executable() -> str | None:
    """Locate Codex both on PATH and in the standalone installer's default bin dir."""
    found = shutil.which("codex")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "codex"
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


async def install_codex_cli() -> dict:
    """Run OpenAI's official standalone installer once, with bounded output/time."""
    async with _install_lock:
        existing = codex_executable()
        if existing:
            return {"installed": True, "path": existing, "already_installed": True}
        if shutil.which("curl") is None or shutil.which("sh") is None:
            raise CodexAppServerError("Server cần có curl và sh để cài Codex CLI")
        process = await asyncio.create_subprocess_exec(
            "sh", "-c", CODEX_INSTALL_COMMAND,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 180)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise CodexAppServerError("Cài Codex quá 3 phút và đã bị dừng") from exc
        text = output.decode(errors="replace")[-6000:]
        installed = codex_executable()
        if process.returncode != 0 or not installed:
            detail = text.strip() or f"installer exit {process.returncode}"
            raise CodexAppServerError(f"Cài Codex thất bại: {detail}")
        return {"installed": True, "path": installed, "already_installed": False, "output": text}


async def start_cli_device_login(codex_home: Path | None = None) -> dict:
    """Start the real server-side CLI device flow and return its URL/code.

    The child stays alive after this function returns; Codex CLI itself exits
    once the operator grants access. Repeated clicks reuse the still-valid
    flow instead of spawning competing login processes for the same home.
    """
    global _device_login_process, _device_login_result, _device_login_drain_task
    async with _device_login_lock:
        if _device_login_process and _device_login_process.returncode is None and _device_login_result:
            return _device_login_result

        executable = codex_executable()
        if executable is None:
            raise CodexAppServerError("Chưa cài Codex CLI trên server")
        env = os.environ.copy()
        target_home = codex_home or codex_app_server._codex_home()
        target_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        env["CODEX_HOME"] = str(target_home)
        # Codex buffers this short prompt when stdout is a regular pipe. Give
        # it a pseudo-terminal so the URL/code are flushed immediately, just
        # as they are when the command is run in an interactive shell.
        master_fd, slave_fd = pty.openpty()
        try:
            try:
                _device_login_process = await asyncio.create_subprocess_exec(
                    executable, "login", "--device-auth",
                    stdout=slave_fd,
                    stderr=slave_fd,
                    env=env,
                )
            except BaseException:
                os.close(master_fd)
                raise
        finally:
            os.close(slave_fd)

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        master_pipe = os.fdopen(master_fd, "rb", buffering=0)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, master_pipe)

        output = ""
        try:
            while len(output) < 16_000:
                line = await asyncio.wait_for(reader.readline(), 15)
                if not line:
                    break
                output += line.decode(errors="replace")
                clean = _ANSI_ESCAPE_RE.sub("", output)
                urls = _URL_RE.findall(clean)
                codes = _DEVICE_CODE_RE.findall(clean.upper())
                if urls and codes:
                    _device_login_result = {
                        "loginId": f"cli-{_device_login_process.pid}",
                        "verificationUrl": urls[-1].rstrip(".,;"),
                        "userCode": codes[-1].upper(),
                    }
                    # Continue draining output while the CLI waits, otherwise
                    # a full pipe could prevent it from completing login.
                    async def drain_login_output() -> None:
                        try:
                            while await reader.read(4096):
                                pass
                        except OSError:
                            # Linux PTYs report EIO when the slave closes.
                            pass

                    _device_login_drain_task = asyncio.create_task(drain_login_output())
                    return _device_login_result
        except asyncio.TimeoutError as exc:
            _device_login_process.terminate()
            await _device_login_process.wait()
            master_pipe.close()
            _device_login_process = None
            raise CodexAppServerError("Codex CLI không in device code trong 15 giây") from exc

        return_code = await _device_login_process.wait()
        master_pipe.close()
        _device_login_process = None
        detail = _ANSI_ESCAPE_RE.sub("", output).strip()[-3000:]
        raise CodexAppServerError(
            f"Không đọc được device code từ Codex CLI (exit {return_code}): {detail or 'không có output'}"
        )


async def refresh_app_server_after_cli_login() -> None:
    """Restart app-server once the external CLI login has completed."""
    global _device_login_process, _device_login_result
    async with _device_login_lock:
        process = _device_login_process
        if process is None or process.returncode is None:
            return
        await process.wait()
        _device_login_process = None
        _device_login_result = None
        # account/read may otherwise retain the pre-login auth state from the
        # app-server process started when the Settings page first loaded.
        await codex_app_server.close()


class CodexAppServer:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._start_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._notifications: asyncio.Queue[dict] = asyncio.Queue()
        self._tool_handler: ToolHandler | None = None

    def _codex_home(self) -> Path:
        value = Path(settings.codex_home).expanduser()
        if not value.is_absolute():
            value = Path(__file__).resolve().parent.parent / value
        value.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            value.chmod(0o700)
        except OSError:
            pass
        return value

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if (
                self._process
                and self._process.returncode is None
                and self._reader_task
                and not self._reader_task.done()
            ):
                return
            if self._process or self._reader_task:
                # A failed reader can leave the child process alive. Do not
                # reuse that half-dead pair; the next request must get a clean
                # JSONL connection.
                await self.close()
            env = os.environ.copy()
            env["CODEX_HOME"] = str(self._codex_home())
            try:
                executable = codex_executable()
                if executable is None:
                    raise CodexAppServerError("Chưa cài Codex CLI trên server")
                self._process = await asyncio.create_subprocess_exec(
                    executable, "app-server", "--stdio",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                    limit=CODEX_APP_SERVER_STREAM_LIMIT,
                )
            except FileNotFoundError as exc:
                raise CodexAppServerError("Chưa cài Codex CLI trên server") from exc
            self._reader_task = asyncio.create_task(self._read_loop())
            await self._request(
                "initialize",
                {
                    "clientInfo": {"name": "ceph_ai", "title": "Ceph AI", "version": "1.0"},
                    "capabilities": {"experimentalApi": True},
                },
                ensure_started=False,
            )
            await self._send({"method": "initialized", "params": {}})

    async def _send(self, message: dict) -> None:
        if not self._process or not self._process.stdin:
            raise CodexAppServerError("Codex App Server chưa chạy")
        self._process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self._process.stdin.drain()

    async def _request(
        self, method: str, params: dict | None = None, *, timeout: float = 30, ensure_started: bool = True
    ) -> dict:
        if ensure_started:
            await self._ensure_started()
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        await self._send(message)
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise CodexAppServerError(f"Codex không phản hồi cho {method}") from exc

    async def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        error = CodexAppServerError("Codex App Server đã dừng")
        reader_failed = False
        try:
            while line := await self._process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                request_id = message.get("id")
                if request_id in self._pending and ("result" in message or "error" in message):
                    future = self._pending.pop(request_id)
                    if "error" in message:
                        future.set_exception(CodexAppServerError(message["error"].get("message", "Codex error")))
                    else:
                        future.set_result(message.get("result") or {})
                    continue
                if request_id is not None and message.get("method") == "item/tool/call":
                    asyncio.create_task(self._handle_tool_request(request_id, message.get("params") or {}))
                    continue
                # Never permit Codex built-ins to escape the restricted sandbox.
                if request_id is not None and message.get("method", "").endswith("requestApproval"):
                    await self._send({"id": request_id, "result": {"decision": "decline"}})
                    continue
                await self._notifications.put(message)
        except Exception as exc:
            # readline() raises ValueError when a JSONL frame exceeds its
            # StreamReader limit. Convert all reader failures into a normal
            # app-server error so callers do not hang until their timeout.
            error = CodexAppServerError(f"Codex App Server reader lỗi: {exc}")
            reader_failed = True
            logger.exception("Codex app-server reader stopped")
        finally:
            process = self._process
            if process and process.returncode is None and reader_failed:
                process.terminate()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()

    async def _handle_tool_request(self, request_id: int, params: dict) -> None:
        if self._tool_handler is None:
            text, success = "Tool không khả dụng", False
        else:
            try:
                text, success = await self._tool_handler(params.get("tool", ""), params.get("arguments") or {})
            except Exception as exc:  # tool failures are model-visible, not server-fatal
                text, success = str(exc), False
        await self._send({
            "id": request_id,
            "result": {"contentItems": [{"type": "inputText", "text": text}], "success": success},
        })

    async def account(self) -> dict:
        result = await self._request("account/read", {"refreshToken": False})
        return result.get("account") or {}

    async def start_device_login(self) -> dict:
        return await self._request("account/login/start", {"type": "chatgptDeviceCode"})

    async def logout(self) -> None:
        await self._request("account/logout")

    async def models(self) -> list[dict]:
        """Return the model picker catalog exposed by the logged-in account."""
        result = await self._request("model/list", {"includeHidden": False})
        return result.get("data") or []

    async def rate_limits(self) -> dict:
        return await self._request("account/rateLimits/read", {})

    async def close(self) -> None:
        process, task = self._process, self._reader_task
        self._process = None
        self._reader_task = None
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def run_turn(
        self, prompt: str, dynamic_tools: list[dict], tool_handler: ToolHandler, timeout: float = 120
    ) -> dict:
        async with self._turn_lock:
            await self._ensure_started()
            while not self._notifications.empty():
                self._notifications.get_nowait()
            self._tool_handler = tool_handler
            tools = [
                {
                    "type": "function",
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "inputSchema": tool["function"].get("parameters", {"type": "object"}),
                }
                for tool in dynamic_tools
            ]
            thread_params = {
                "cwd": "/tmp",
                "approvalPolicy": "never",
                "sandboxPolicy": {
                    "type": "readOnly",
                    "access": {"type": "restricted", "includePlatformDefaults": False, "readableRoots": []},
                },
                "dynamicTools": tools,
                "serviceName": "ceph-ai-dashboard",
            }
            if settings.codex_chat_model.strip():
                thread_params["model"] = settings.codex_chat_model.strip()
            thread = await self._request(
                "thread/start",
                thread_params,
            )
            thread_id = thread["thread"]["id"]
            await self._request(
                "turn/start",
                {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]},
            )
            final_text = ""
            try:
                while True:
                    message = await asyncio.wait_for(self._notifications.get(), timeout)
                    method = message.get("method")
                    params = message.get("params") or {}
                    if method == "item/completed":
                        item = params.get("item") or {}
                        if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
                            final_text = item.get("text") or final_text
                    elif method == "error":
                        raise CodexAppServerError((params.get("error") or {}).get("message", "Codex error"))
                    elif method == "turn/completed":
                        turn = params.get("turn") or {}
                        if turn.get("status") == "failed":
                            raise CodexAppServerError((turn.get("error") or {}).get("message", "Codex turn thất bại"))
                        record_ai_usage(turn)
                        return {"reply_text": final_text.strip() or "Codex không trả về nội dung"}
            finally:
                self._tool_handler = None


codex_app_server = CodexAppServer()
