"""FL6.3: an operator approves an AI reproduction proposal on Telegram.

scripts/lab/propose_reproduction.py stores a validated proposal (FL6.2) and
posts this card with "Cho phép tái hiện" / "Bỏ qua" buttons. The Telegram
gateway (dashboard/telegram_chat.py) lets only operators decide; a decision is
written back into the proposal file with who and when. APPROVED proposals are
what the Failure Lab runner may execute on the lab cluster (FL6.4); nothing is
injected here, and a decided proposal cannot be decided again.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from shared.reproduction_proposal import KIND_CODES, KIND_DESCRIPTIONS

PROPOSALS_DIR = Path("/var/lib/ceph-ai/failure-lab/proposals")
APPROVE_PREFIX = "flrepro:ok:"
SKIP_PREFIX = "flrepro:skip:"
PROPOSED, APPROVED, SKIPPED = "PROPOSED", "APPROVED", "SKIPPED"
RUNNING, DONE, FAILED = "RUNNING", "DONE", "FAILED"
_ID_RE = re.compile(r"repro-[0-9a-f]{10}")


class ReproductionError(RuntimeError):
    """Shown to the operator in Telegram as is."""


def parse(data: str, prefix: str) -> str:
    proposal_id = data[len(prefix):]
    if not _ID_RE.fullmatch(proposal_id):
        raise ReproductionError("Nút duyệt không hợp lệ.")
    return proposal_id


def _path(proposal_id: str, directory: Path) -> Path:
    if not _ID_RE.fullmatch(proposal_id):
        raise ReproductionError("Mã đề xuất không hợp lệ.")
    return directory / f"{proposal_id}.json"


def load(proposal_id: str, directory: Path | None = None) -> dict:
    try:
        record = json.loads(_path(proposal_id, directory or PROPOSALS_DIR).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReproductionError(f"Không đọc được đề xuất {proposal_id}.") from exc
    if not isinstance(record, dict):
        raise ReproductionError(f"Đề xuất {proposal_id} hỏng.")
    return record


def _write(record: dict, directory: Path) -> None:
    path = _path(str(record["id"]), directory)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _proposal(record: dict) -> dict:
    raw = record.get("proposal")
    return raw if isinstance(raw, dict) else {}


def update(proposal_id: str, directory: Path | None = None, **fields: object) -> dict:
    """Merge ``fields`` into a stored proposal (the Failure Lab runner's progress)."""
    directory = directory or PROPOSALS_DIR
    record = load(proposal_id, directory)
    record.update(fields)
    _write(record, directory)
    return record


def next_approved(directory: Path | None = None) -> str | None:
    """The oldest APPROVED proposal id, or None."""
    directory = directory or PROPOSALS_DIR
    approved = []
    for path in sorted(directory.glob("repro-*.json")) if directory.exists() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("status") == APPROVED:
            approved.append((str(record.get("decided_at") or ""), str(record.get("id"))))
    return min(approved)[1] if approved else None


def decide(proposal_id: str, *, approve: bool, actor: str, directory: Path | None = None,
           now: datetime | None = None) -> dict:
    """Record the operator's decision once; an approval re-checks that the kind is still runnable."""
    directory = directory or PROPOSALS_DIR
    record = load(proposal_id, directory)
    if record.get("status") != PROPOSED:
        raise ReproductionError(f"Đề xuất {proposal_id} đã ở trạng thái {record.get('status')}.")
    if approve and str(_proposal(record).get("fault_kind")) not in KIND_CODES:
        raise ReproductionError("Kiểu lỗi của đề xuất không còn trong bộ chạy; không duyệt được.")
    record.update({"status": APPROVED if approve else SKIPPED, "decided_by": actor,
                   "decided_at": (now or datetime.now(timezone.utc)).isoformat()})
    _write(record, directory)
    return record


def card_text(record: dict) -> str:
    proposal = _proposal(record)
    kind = str(proposal.get("fault_kind") or "")
    examples = "\n".join(f"  • {item}" for item in record.get("evidence_examples") or []) or "  • (không có)"
    docs = "\n".join(f"  • {url}" for url in proposal.get("citations") or []) or "  • (không có tài liệu)"
    return (
        f"🧪 Đề xuất tái hiện lỗi trên cụm LAB — {record.get('family')}\n"
        f"Lỗi production còn thiếu bằng chứng, ví dụ:\n{examples}\n\n"
        f"Kiểu lỗi: {kind} — {KIND_DESCRIPTIONS.get(kind, '?')}\n"
        f"Mã health kỳ vọng: {', '.join(proposal.get('expected_health_codes') or [])}\n"
        f"Nguyên nhân sẽ chứng minh: {proposal.get('cause')}\n"
        f"Hành động chấp nhận: {', '.join(proposal.get('acceptable_action_ids') or [])}\n"
        f"Giữ lỗi tối đa: {proposal.get('max_seconds')} s\n"
        f"Tài liệu:\n{docs}\n\n"
        "Chỉ chạy trên cụm lab (fsid ghim), khóa một lượt, lỗi luôn được gỡ; "
        "sửa lỗi sẽ hỏi duyệt riêng."
    )[:4000]


def buttons(proposal_id: str) -> list[tuple[str, str]]:
    return [("🧪 Cho phép tái hiện", f"{APPROVE_PREFIX}{proposal_id}"), ("⏭ Bỏ qua", f"{SKIP_PREFIX}{proposal_id}")]
