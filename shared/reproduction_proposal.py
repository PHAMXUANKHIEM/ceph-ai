"""FL6.2: an AI-written, machine-checked proposal to reproduce a fault on the lab.

The AI reads the official Ceph text for a fault family plus the production
evidence that was not enough, and answers with JSON. It never writes a shell
command: it picks one fault kind the Failure Lab runner already implements
(each with its own undo), or asks for a new kind, which becomes a code change
for review and can never run directly.

``validate`` is deterministic and fail-closed: an unknown kind, parameters, a
time limit outside the runner's bounds, health codes or actions outside the
catalog, a citation that was not given to the AI, or a cause without one is a
refusal with a reason. A valid proposal still only runs after an operator
approves it on Telegram (FL6.3) and only on the lab cluster.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

NEW_KIND = "NEW_KIND_NEEDED"
MIN_SECONDS, MAX_SECONDS = 60, 600
# What each implemented fault kind (shared.failure_lab_fault.PREPARE) can raise.
KIND_CODES: dict[str, frozenset[str]] = {
    "stop_osd": frozenset({"OSD_DOWN", "OSD_HOST_DOWN", "PG_DEGRADED", "PG_AVAILABILITY", "OBJECT_DEGRADED",
                           "OBJECT_MISPLACED", "SLOW_OPS"}),
    "nearfull_ratio": frozenset({"OSD_NEARFULL", "POOL_NEARFULL"}),
}
KIND_DESCRIPTIONS = {
    "stop_osd": "dừng daemon một OSD đang up (runner chọn OSD, chỉ khi `ceph osd ok-to-stop` đồng ý), "
                "rồi khởi động lại; không tham số",
    "nearfull_ratio": "hạ ngưỡng nearfull của cụm xuống ngay dưới mức dùng của OSD đầy nhất, rồi trả lại giá trị "
                      "cũ; không tham số",
}
_JSON_RE = re.compile(r"\{.*\}", re.S)


@dataclass
class Proposal:
    family: str
    fault_kind: str
    parameters: dict
    expected_health_codes: list[str]
    cause: str
    acceptable_action_ids: list[str]
    max_seconds: int
    citations: list[str]
    reasoning: str = ""
    new_kind_request: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def build_prompt(family: str, examples: list[str], citations: list[dict], action_ids: list[str]) -> str:
    kinds = "\n".join(f"- {kind}: {text}" for kind, text in KIND_DESCRIPTIONS.items())
    docs = "\n\n".join(f"[{item['url']}]\n{item['text']}" for item in citations) or "(không có tài liệu chính thức)"
    evidence = "\n".join(f"- {item}" for item in examples) or "- (không có ví dụ)"
    return f"""Bạn thiết kế một lượt TÁI HIỆN LỖI CÓ KIỂM SOÁT trên cụm Ceph LAB (staging), không phải production.
Họ lỗi production đang "chưa đủ bằng chứng": {family}

Bằng chứng production đã thu (không đủ để kết luận):
{evidence}

Tài liệu Ceph chính thức (chỉ được trích dẫn các URL dưới đây):
{docs}

Các kiểu lỗi bộ chạy Failure Lab ĐÃ CÓ (mỗi kiểu tự gỡ lỗi sau khi chạy):
{kinds}

Quy tắc:
- KHÔNG viết lệnh shell hay lệnh ceph. Chỉ chọn một kiểu lỗi ở trên.
- Nếu không kiểu nào tái hiện được họ lỗi này, đặt "fault_kind": "{NEW_KIND}" và mô tả kiểu lỗi cần thêm
  trong "new_kind_request" (tên, cơ chế gây lỗi, cách gỡ, rủi ro); đề xuất này sẽ thành việc viết code + review.
- "expected_health_codes": mã health mà lỗi này phải tạo ra.
- "cause": nguyên nhân thật mà lượt tái hiện sẽ chứng minh, dựa trên tài liệu.
- "acceptable_action_ids": chọn trong danh sách sau: {", ".join(action_ids)}.
- "max_seconds": thời gian giữ lỗi, từ {MIN_SECONDS} đến {MAX_SECONDS}.
- "citations": URL tài liệu đã dùng, lấy đúng từ danh sách trên; không có thì để [].

Trả lời DUY NHẤT một object JSON với các khóa: family, fault_kind, parameters, expected_health_codes, cause,
acceptable_action_ids, max_seconds, citations, reasoning, new_kind_request.
"""


def parse(answer: str) -> dict:
    """The JSON object in the AI's final answer ({} if there is none)."""
    match = _JSON_RE.search(answer or "")
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _strings(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def validate(raw: dict, *, family: str, given_urls: set[str], action_ids: set[str],
             family_prefixes: tuple[str, ...]) -> tuple[Proposal | None, list[str]]:
    """(proposal, []) when it may be offered to the operator, else (None or a new-kind request, reasons)."""
    errors: list[str] = []
    kind = str(raw.get("fault_kind") or "")
    codes = sorted({code.upper() for code in _strings(raw.get("expected_health_codes"))})
    actions = sorted(set(_strings(raw.get("acceptable_action_ids"))))
    citations = _strings(raw.get("citations"))
    try:
        seconds = int(raw.get("max_seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0
    raw_parameters, raw_request = raw.get("parameters"), raw.get("new_kind_request")
    parameters: dict = raw_parameters if isinstance(raw_parameters, dict) else {}
    request: dict = raw_request if isinstance(raw_request, dict) else {}
    proposal = Proposal(
        family=family, fault_kind=kind, parameters=parameters,
        expected_health_codes=codes, cause=str(raw.get("cause") or "").strip()[:600], acceptable_action_ids=actions,
        max_seconds=seconds, citations=citations, reasoning=str(raw.get("reasoning") or "").strip()[:1200],
        new_kind_request=request,
    )
    if str(raw.get("family") or family) != family:
        errors.append("family differs from the requested one")
    if kind == NEW_KIND:
        if not proposal.new_kind_request:
            errors.append("a new kind needs new_kind_request")
        return (proposal if not errors else None), (errors or ["new fault kind requested: code change and review, never run"])
    errors += _kind_errors(proposal, family, family_prefixes)
    errors += _catalog_errors(proposal, action_ids, given_urls)
    return (None if errors else proposal), errors


def _kind_errors(proposal: Proposal, family: str, family_prefixes: tuple[str, ...]) -> list[str]:
    """Can the chosen runner kind raise the expected codes of this family?"""
    kind, codes, errors = proposal.fault_kind, proposal.expected_health_codes, []
    if kind not in KIND_CODES:
        errors.append(f"unknown fault kind {kind!r}")
    if proposal.parameters:
        errors.append("the implemented kinds take no parameters")
    if not codes:
        errors.append("no expected health codes")
    elif kind in KIND_CODES and not set(codes) <= KIND_CODES[kind]:
        errors.append(f"{kind} cannot raise {sorted(set(codes) - KIND_CODES[kind])}")
    if codes and not any(code.startswith(prefix.rstrip(":")) for code in codes for prefix in family_prefixes):
        errors.append(f"no expected code belongs to {family}")
    return errors


def _catalog_errors(proposal: Proposal, action_ids: set[str], given_urls: set[str]) -> list[str]:
    """Actions, time limit, citations and cause stay inside what was given and allowed."""
    actions, errors = set(proposal.acceptable_action_ids), []
    if not actions or not actions <= action_ids:
        errors.append(f"actions outside the catalog: {sorted(actions - action_ids) or 'none given'}")
    if not MIN_SECONDS <= proposal.max_seconds <= MAX_SECONDS:
        errors.append(f"max_seconds must be {MIN_SECONDS}-{MAX_SECONDS}")
    if not set(proposal.citations) <= given_urls:
        errors.append("a citation was not among the documents given")
    if len(proposal.cause) < 20:
        errors.append("the cause is missing or too short")
    return errors
