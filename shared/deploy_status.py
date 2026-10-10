"""Answer "is a deploy running?" from the deploy runner's own files, without an AI call.

The natural-language router sends deploy questions (intent ``deploy_status``)
here. Everything comes from /var/lib/ceph-ai/deploy-requests, which the
release notifier and scripts/deploy/deploy_request_runner.py maintain:
status.json (last run), pending.json (queued request), approved/<sha>.json
(operator-approved merges) and held/ (approvals deliberately held back).
Read-only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.ci_control import read_deploy_status

DEPLOY_REQUEST_DIR = Path("/var/lib/ceph-ai/deploy-requests")
_STATE_TEXT = {
    "checking": "🔎 đang kiểm tra yêu cầu",
    "running": "🚀 ĐANG DEPLOY",
    "succeeded": "✅ deploy gần nhất thành công",
    "failed": "❌ deploy gần nhất THẤT BẠI",
    "refused": "⛔ yêu cầu deploy gần nhất bị từ chối",
}


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _local(value: object) -> str:
    parsed = _parse_time(value)
    return parsed.astimezone().strftime("%H:%M %d/%m") if parsed else "?"


def _approvals(directory: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((directory / "approved").glob("*.json")) if (directory / "approved").exists() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            rows.append({**record, "merge_sha": str(record.get("merge_sha") or path.stem)})
    return rows


def deploy_status(directory: Path = DEPLOY_REQUEST_DIR) -> dict[str, Any]:
    """Structured view: last run, queued request, approved merges newer than the last success, held approvals."""
    status = read_deploy_status(directory) or {}
    last_success = _parse_time(status.get("updated_at")) if status.get("state") == "succeeded" else None
    waiting = [row for row in _approvals(directory)
               if row["merge_sha"] != status.get("sha")
               and (last_success is None or (_parse_time(row.get("approved_at")) or last_success) > last_success)]
    held = len(list((directory / "held").glob("*.json"))) if (directory / "held").exists() else 0
    return {"status": status, "queued": status.get("queued"), "approved_not_deployed": waiting, "held": held}


def deploy_status_text(directory: Path = DEPLOY_REQUEST_DIR) -> str:
    view = deploy_status(directory)
    status = view["status"]
    lines = ["📦 Trạng thái deploy Ceph AI"]
    if status.get("state"):
        lines.append(f"{_STATE_TEXT.get(status['state'], status['state'])}: {str(status.get('sha') or '?')[:8]} "
                     f"lúc {_local(status.get('updated_at'))}"
                     + (f" ({status['message']})" if status.get("message") else ""))
    else:
        lines.append("Chưa có lượt deploy nào được ghi nhận.")
    queued = view["queued"]
    if queued:
        lines.append(f"⏳ Đang xếp hàng: {str(queued.get('sha') or '?')[:8]}, yêu cầu bởi "
                     f"{queued.get('requested_by') or '?'} lúc {_local(queued.get('requested_at'))}")
    waiting = view["approved_not_deployed"]
    if waiting:
        lines.append(f"🕒 {len(waiting)} bản đã duyệt merge, chưa deploy (chờ CI trên main xanh):")
        lines += [f"  • PR #{row.get('pr', '?')} {str(row.get('title') or '')[:70]}" for row in waiting[:8]]
    elif not queued and status.get("state") not in ("checking", "running"):
        lines.append("Không có bản nào đang chờ deploy.")
    if view["held"]:
        lines.append(f"⏸ {view['held']} bản duyệt đang bị giữ lại (thư mục held).")
    return "\n".join(lines)
