"""AI-assisted extraction of capability drafts from operator-supplied Ceph docs."""
from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime
from urllib.parse import urlparse

from config.settings import settings
from shared import capability_matrix, db
from shared.ai_redaction import redact_text
from shared.models import CapabilityMatrixProposal
from shared.router_client import build_router_client

ALLOWED_DOC_HOSTS = {"docs.ceph.com", "download.ceph.com"}
AI_TIMEOUT_SECONDS = 45

def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def validate_doc_url(url: str) -> str:
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOC_HOSTS:
        raise ValueError("Chỉ chấp nhận HTTPS từ docs.ceph.com hoặc download.ceph.com")
    return value


async def generate(*, doc_url: str, release_notes: str, actor: str) -> list[CapabilityMatrixProposal]:
    doc_url = validate_doc_url(doc_url)
    text = release_notes.strip()
    if not 100 <= len(text) <= 30000:
        raise ValueError("Nội dung release notes phải từ 100 đến 30000 ký tự")
    allowed = capability_matrix.gated_command_ids()
    source_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    client = build_router_client(settings.router_api_key, settings.router_base_url)
    response = await client.chat.completions.create(
        model=settings.router_model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "Extract only explicitly supported Ceph capabilities. Return JSON {proposals:[{command_id,inner_command,min_major,max_major,evidence_excerpt,rationale}]}. Never infer missing versions."},
            {"role": "user", "content": redact_text(f"Allowed command_id values: {allowed}\nOfficial source: {doc_url}\nRelease notes:\n{text}")},
        ],
        max_tokens=2000,
        timeout=AI_TIMEOUT_SECONDS,
    )
    payload = json.loads(response.choices[0].message.content or "{}")
    proposals = payload.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError("AI không trả về danh sách proposal hợp lệ")
    created = []
    with db.SessionLocal() as session:
        for item in proposals[:20]:
            if not isinstance(item, dict):
                continue
            command_id = str(item.get("command_id") or "")
            excerpt = str(item.get("evidence_excerpt") or "").strip()
            if command_id not in allowed or len(excerpt) < 10 or _normalized(excerpt) not in _normalized(redact_text(text)):
                continue
            min_major = item.get("min_major")
            max_major = item.get("max_major")
            if isinstance(min_major, bool) or not isinstance(min_major, int):
                continue
            if max_major is not None and (isinstance(max_major, bool) or not isinstance(max_major, int) or max_major < min_major):
                continue
            row = CapabilityMatrixProposal(command_id=command_id,
                inner_command=str(item.get("inner_command") or command_id)[:2000], min_major=min_major,
                max_major=max_major, doc_url=doc_url, evidence_excerpt=excerpt[:4000],
                source_sha256=source_sha256,
                rationale=str(item.get("rationale") or "AI extraction")[:4000], proposed_by=actor,
                status="PENDING", created_at=datetime.utcnow())
            session.add(row); created.append(row)
        session.commit()
        for row in created: session.refresh(row); session.expunge(row)
    return created


def list_proposals():
    with db.SessionLocal() as session:
        rows = session.query(CapabilityMatrixProposal).order_by(CapabilityMatrixProposal.created_at.desc()).limit(100).all()
        session.expunge_all(); return rows


def review(proposal_id: str, *, approve: bool, actor: str):
    with db.SessionLocal() as session:
        row = session.query(CapabilityMatrixProposal).filter_by(id=proposal_id).with_for_update().one_or_none()
        if row is None or row.status != "PENDING": return None
        values = {key: getattr(row, key) for key in ("command_id", "inner_command", "doc_url", "min_major", "max_major")}
        rationale = row.rationale; excerpt = row.evidence_excerpt
        if approve:
            entry = capability_matrix.create_entry(**values, verified_by=actor, session=session,
                notes=f"AI-assisted draft; operator approved. Evidence: {excerpt[:1000]}. Rationale: {rationale[:1000]}")
            row.created_entry_id = entry.id
        row.status = "APPROVED" if approve else "REJECTED"; row.reviewed_by = actor; row.reviewed_at = datetime.utcnow()
        session.commit(); session.refresh(row)
        session.expunge(row); return row
