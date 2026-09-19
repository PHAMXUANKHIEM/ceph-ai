"""Send the previous nightly AI improvement result to Telegram at 08:30."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from shared.telegram_client import TelegramSendError, send_telegram_message

NIGHTLY_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_TELEGRAM_CHARS = 3_800
logger = logging.getLogger(__name__)


def _load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _report_state_path(nightly_state_path: Path) -> Path:
    """Keep delivery bookkeeping separate from the writer's nightly state."""
    return nightly_state_path.with_name("nightly-ai-improvement-report.json")


def _clip(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _state_run_date(state: dict) -> str:
    """Find the local date even when FAILED state remains retryable."""
    explicit = str(state.get("last_run_date") or "")
    if explicit:
        return explicit
    for field in ("finished_at", "started_at"):
        value = state.get(field)
        if not value:
            continue
        try:
            timestamp = datetime.fromisoformat(str(value))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return timestamp.astimezone(NIGHTLY_TIMEZONE).date().isoformat()
        except (TypeError, ValueError):
            continue
    return ""


def _recommendation(state: dict) -> str:
    status = str(state.get("status") or "UNKNOWN")
    if status in {"PATCH_READY", "COMMITTED", "PUSHED", "STAGING_VERIFIED", "PROMOTED"}:
        return (
            "1) Review diff của candidate và test output. "
            "2) Nếu đạt, commit/push thủ công theo quy trình review. "
            "3) Hôm nay ưu tiên kiểm tra regression và rollback plan trước khi merge."
        )
    if status == "NO_CHANGE":
        return (
            "1) Chọn một đề xuất bounded có test rõ ràng từ analyst. "
            "2) Ưu tiên cải thiện có số liệu đo trước/sau. "
            "3) Không đổi provider hoặc production default nếu chưa canary."
        )
    if status == "BLOCKED_DIRTY_CHECKOUT":
        return (
            "1) Review các thay đổi chưa commit trong checkout. "
            "2) Tách hoặc commit chúng theo quy trình của operator. "
            "3) Chạy lại nightly sau khi checkout sạch."
        )
    if status == "RUNNING":
        return "Job nightly vẫn đang chạy; chờ kết quả cuối trước khi review hoặc merge candidate."
    return (
        "1) Kiểm tra lỗi nightly và provider authentication. "
        "2) Sửa nguyên nhân gốc rồi chạy lại test bounded. "
        "3) Không merge candidate khi chưa có test pass."
    )


def build_morning_report(state: dict, *, now: datetime | None = None) -> str:
    """Build one bounded, operator-readable report from nightly state."""
    current = now or datetime.now(timezone.utc)
    local = current.astimezone(NIGHTLY_TIMEZONE)
    run_date = _state_run_date(state)
    status = str(state.get("status") or "NO_RECORD")
    lines = [
        "☀️ AI NIGHTLY IMPROVEMENT — BÁO CÁO 08:30",
        f"Ngày báo cáo: {local.strftime('%d/%m/%Y %H:%M')} · Kết quả job: {run_date or 'không có record'}",
        f"Trạng thái: {status}",
    ]

    changed_files = state.get("changed_files") or []
    if changed_files:
        files = ", ".join(_clip(item, 100) for item in changed_files[:12])
        lines.append(f"Đã thay đổi ({len(changed_files)} file): {files}")
    else:
        lines.append("Đã thay đổi: không có patch được ghi nhận.")
    if state.get("candidate_worktree"):
        lines.append(f"Candidate: {_clip(state['candidate_worktree'], 260)}")
    if state.get("analysis_reports") is not None:
        lines.append(
            f"Analyst: {state.get('analysis_reports', 0)} report, "
            f"{len(state.get('analysis_failures') or [])} lỗi analyst"
        )
    if state.get("error"):
        lines.append(f"Lỗi: {_clip(state['error'], 700)}")

    recommendation = _recommendation(state)
    recommendation_lines = ["Đề xuất nên làm hôm nay:", recommendation]
    # Reserve space for the recommendation first. Analyst previews are useful
    # context, but must never push the operator's next action out of Telegram.
    mandatory = "\n".join(lines + recommendation_lines)
    remaining = MAX_TELEGRAM_CHARS - len(mandatory)
    previews = state.get("analysis_report_previews") or []
    if previews and remaining > len("\nĐề xuất từ analyst:\n") + 40:
        preview_lines = ["Đề xuất từ analyst:"]
        remaining -= len("\n".join(preview_lines)) + 1
        for index, preview in enumerate(previews[:3], 1):
            label = f"{index}. "
            budget = min(650, remaining - len(label) - 1)
            if budget < 40:
                break
            item = f"{label}{_clip(preview, budget)}"
            preview_lines.append(item)
            remaining -= len(item) + 1
        lines.extend(preview_lines)
    lines.extend(recommendation_lines)
    return "\n".join(lines)


def _notify_bootstrap_failure(exc: Exception) -> None:
    enabled = os.getenv("TELEGRAM_CODE_REPAIR_ENABLED", "true").strip().lower()
    token = os.getenv("TELEGRAM_CODE_REPAIR_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CODE_REPAIR_CHAT_ID", "").strip()
    if enabled in {"0", "false", "no", "off"} or not token or not chat_id:
        return
    try:
        send_telegram_message(
            token, chat_id,
            "⚠️ AI MORNING REPORT KHÔNG KHỞI ĐỘNG\n"
            f"Lỗi cấu hình/khởi động: {str(exc)[:900]}",
        )
    except TelegramSendError:
        logger.exception("could not send morning report bootstrap failure alert")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    try:
        from config.settings import settings
        from shared.telegram_alerts import send_code_repair_alert

        now = datetime.now(timezone.utc)
        state_path = Path(settings.ai_nightly_improvement_state_file)
        state = _load_state(state_path)
        report_state_path = _report_state_path(state_path)
        delivery_state = _load_state(report_state_path)
        report_date = now.astimezone(NIGHTLY_TIMEZONE).date().isoformat()
        if delivery_state.get("morning_report_date") == report_date:
            logger.info("morning nightly report already sent for %s", report_date)
            return 0
        sent = send_code_repair_alert(build_morning_report(state, now=now))
        if sent:
            delivery_state["morning_report_date"] = report_date
            delivery_state["morning_report_sent_at"] = now.isoformat()
            _save_state(report_state_path, delivery_state)
            logger.info("morning nightly report sent for %s", report_date)
        else:
            logger.warning("morning nightly report was not sent; Telegram channel is unavailable or disabled")
        return 0
    except Exception as exc:
        logger.exception("morning nightly report failed")
        _notify_bootstrap_failure(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
