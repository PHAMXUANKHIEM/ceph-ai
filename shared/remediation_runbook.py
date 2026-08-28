"""Evidence-only AI runbooks built from verified RemediationCase history."""

from __future__ import annotations

import json
import hashlib
import logging
import re

import httpx
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from config.settings import settings
from shared import db
from shared.ai_observability import observe_ai_call
from shared.ai_redaction import default_redactor
from shared.claude_cli import ClaudeCLIError, run_claude_prompt
from shared.codex_app_server import CodexAppServerError, codex_app_server
from shared.models import AIRunbook, Action, Cluster, Incident, RemediationCase
from shared.router_client import build_router_client
from shared.synthetic_incidents import is_synthetic_evidence

PROMPT_VERSION = "remediation-runbook-v1"
TOOL_NAME = "write_remediation_runbook"
TIMEOUT_SECONDS = 90
MAX_TOKENS = 4096
MAX_CASES = 50
_SENSITIVE_KEY = re.compile(r"(?:password|secret|token|api.?key|keyring|credential)", re.I)
logger = logging.getLogger(__name__)


class RunbookError(Exception):
    pass


def _safe_json(raw: str | None):
    try:
        value = json.loads(raw or "null")
    except (TypeError, ValueError):
        return None

    def scrub(item):
        if isinstance(item, dict):
            return {
                key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else scrub(value)
                for key, value in item.items()
            }
        if isinstance(item, list):
            return [scrub(value) for value in item]
        return item

    return scrub(value)


def _cluster_filter(query, cluster: Cluster | None, cluster_id: str | None):
    if not cluster_id:
        return query
    if cluster is not None and cluster.is_default:
        return query.filter(or_(RemediationCase.cluster_id == cluster_id, RemediationCase.cluster_id.is_(None)))
    return query.filter(RemediationCase.cluster_id == cluster_id)


def list_fault_families(session, *, cluster_id: str | None = None) -> list[str]:
    """Return verified, non-synthetic fault families available for a scope."""
    cluster = session.get(Cluster, cluster_id) if cluster_id else None
    query = _cluster_filter(
        session.query(RemediationCase).filter(
            RemediationCase.outcome == "VERIFIED_SUCCESS",
            RemediationCase.verified_at.isnot(None),
        ),
        cluster,
        cluster_id,
    )
    values = set()
    for case in query.order_by(RemediationCase.fault_family).all():
        incident = session.get(Incident, case.incident_id)
        if incident is not None and not is_synthetic_evidence(incident.signal_evidence_json):
            values.add(case.fault_family)
    return sorted(values)


def build_source(session, *, fault_family: str, cluster_id: str | None = None,
                 limit: int = MAX_CASES) -> dict:
    """Build the bounded, redacted evidence payload supplied to the model."""
    family = fault_family.strip()
    if not family or len(family) > 64:
        raise RunbookError("fault_family không hợp lệ")
    cluster = session.get(Cluster, cluster_id) if cluster_id else None
    query = _cluster_filter(
        session.query(RemediationCase, Incident, Action)
        .join(Incident, Incident.id == RemediationCase.incident_id)
        .join(Action, Action.id == RemediationCase.action_id)
        .filter(
            RemediationCase.fault_family == family,
            RemediationCase.outcome == "VERIFIED_SUCCESS",
            RemediationCase.verified_at.isnot(None),
        ),
        cluster,
        cluster_id,
    )
    rows = []
    for case, incident, action in query.order_by(
        RemediationCase.verified_at.desc(), RemediationCase.id
    ).limit(max(1, min(limit, MAX_CASES))).all():
        if is_synthetic_evidence(incident.signal_evidence_json):
            continue
        rows.append({
            "case_id": case.id,
            "incident_id": incident.id,
            "action_id": action.id,
            "evidence_ids": [f"case:{case.id}", f"incident:{incident.id}", f"action:{action.id}"],
            "fault_family": case.fault_family,
            "ceph_code": incident.ceph_code,
            "severity": incident.severity,
            "classification": case.classification,
            "action_name": action.action_id,
            "rationale": action.rationale,
            "diagnosis": case.diagnosis,
            "diagnosis_confidence": case.diagnosis_confidence,
            "ceph_version": case.ceph_version,
            "deployment_mode": case.deployment_mode,
            "playbook_version": case.playbook_version,
            "recovery_seconds": case.recovery_seconds,
            "regressions": {
                "1h": case.regressed_1h, "24h": case.regressed_24h, "7d": case.regressed_7d,
            },
            "operator_verdict": case.operator_verdict,
            "operator_note": case.operator_note,
            "pre_state": _safe_json(case.pre_state_json),
            "post_state": _safe_json(case.post_state_json),
        })
    if not rows:
        raise RunbookError("Chưa có RemediationCase VERIFIED_SUCCESS cho fault family này")
    source = {"fault_family": family, "cluster_id": cluster_id, "case_count": len(rows), "cases": rows}
    source["source_fingerprint"] = source_fingerprint(source)
    return source


def source_fingerprint(source: dict) -> str:
    """Return a stable identity for the exact evidence sent to the model."""
    canonical = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_cached(session, source: dict) -> dict | None:
    """Return a previously validated report for this exact evidence set."""
    if not source.get("cluster_id"):
        return None
    fingerprint = source.get("source_fingerprint") or source_fingerprint(source)
    row = (
        session.query(AIRunbook)
        .filter(
            AIRunbook.cluster_id == source.get("cluster_id"),
            AIRunbook.fault_family == source["fault_family"],
            AIRunbook.source_fingerprint == fingerprint,
            AIRunbook.prompt_version == PROMPT_VERSION,
        )
        .order_by(AIRunbook.created_at.desc())
        .first()
    )
    if row is None:
        return None
    try:
        report = validate(json.loads(row.report_json), source)
    except (TypeError, ValueError, RunbookError):
        return None
    report["cached"] = True
    report["cached_at"] = row.created_at.isoformat() if row.created_at else None
    return report


def store_cached(session, source: dict, report: dict) -> None:
    """Persist a validated report; a concurrent duplicate is harmless."""
    if not source.get("cluster_id"):
        return
    fingerprint = source.get("source_fingerprint") or source_fingerprint(source)
    session.add(AIRunbook(
        cluster_id=source["cluster_id"],
        fault_family=source["fault_family"],
        source_fingerprint=fingerprint,
        prompt_version=PROMPT_VERSION,
        source_case_count=source["case_count"],
        report_json=json.dumps(report, ensure_ascii=False, sort_keys=True),
    ))
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        # Two simultaneous operators can generate the same fingerprint. The
        # unique-index race is harmless, but other integrity failures must be
        # not silently disable the cache and make every later request pay for
        # AI again.
        message = str(getattr(exc, "orig", exc)).lower()
        if "unique" not in message and "duplicate" not in message:
            raise
        logger.info("Remediation runbook cache already exists for this evidence")


def _schema() -> dict:
    array = {"type": "array", "items": {"type": "string", "maxLength": 2000}, "maxItems": 20}
    properties = {
        "title": {"type": "string", "maxLength": 200},
        "when_to_use": {"type": "string", "maxLength": 2000},
        "prechecks": array,
        "steps": array,
        "verification": array,
        "rollback": array,
        "prevention": array,
        "limitations": {"type": "string", "maxLength": 2000},
        "citations": {"type": "array", "items": {"type": "string"}, "maxItems": 60},
    }
    return {"type": "function", "function": {
        "name": TOOL_NAME, "description": "Write an evidence-only Ceph remediation runbook",
        "parameters": {"type": "object", "properties": properties,
                       "required": list(properties), "additionalProperties": False},
    }}


@observe_ai_call("remediation_runbook")
async def _call_model(source: dict) -> dict:
    source = default_redactor.redact(source)
    schema = _schema()
    system = (
        "You write concise Vietnamese Ceph remediation runbooks. Use only SOURCE_CASES_JSON. "
        "Never invent a command, threshold, cause, impact, or verification. Steps may only restate "
        "actions and evidence present in the cases. Every factual statement must be backed by one "
        "or more exact evidence_ids from the source. Include uncertainty in limitations."
    )
    user = "SOURCE_CASES_JSON:\n" + json.dumps(source, ensure_ascii=False, sort_keys=True)
    if settings.codex_chat_enabled:
        captured = {}

        async def capture(name, arguments):
            if name != TOOL_NAME:
                return "Tool không được phép", False
            captured.update(arguments)
            return "Đã ghi runbook", True

        try:
            await codex_app_server.run_turn(
                system + "\nCall the tool exactly once.\n" + user,
                [schema], capture, timeout=TIMEOUT_SECONDS,
            )
        except CodexAppServerError as exc:
            raise RunbookError(f"Codex call failed: {exc}") from exc
        if captured:
            return captured
        raise RunbookError("Codex không trả về runbook")
    if settings.claude_chat_enabled:
        try:
            raw = await run_claude_prompt(
                system + "\nReturn only JSON matching this schema:\n" +
                json.dumps(schema["function"]["parameters"], ensure_ascii=False) + "\n" + user,
                timeout=TIMEOUT_SECONDS,
            )
            return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I))
        except (ClaudeCLIError, json.JSONDecodeError) as exc:
            raise RunbookError(f"Claude call failed: {exc}") from exc
    client = build_router_client(settings.router_api_key, settings.router_base_url)
    try:
        completion = await client.chat.completions.create(
            model=settings.router_model, max_tokens=MAX_TOKENS, tools=[schema],
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            timeout=httpx.Timeout(TIMEOUT_SECONDS),
        )
    except Exception as exc:
        raise RunbookError(f"Router call failed: {str(exc) or type(exc).__name__}") from exc
    for call in completion.choices[0].message.tool_calls or []:
        if call.function.name == TOOL_NAME:
            try:
                return json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise RunbookError("AI trả về JSON runbook không hợp lệ") from exc
    raise RunbookError("AI response contained no runbook tool call")


def validate(result: dict, source: dict) -> dict:
    fields = ("title", "when_to_use", "prechecks", "steps", "verification", "rollback", "prevention", "limitations")
    if not isinstance(result, dict):
        raise RunbookError("AI runbook không đúng schema")
    for field in fields:
        value = result.get(field)
        if isinstance(value, str) and value.strip():
            continue
        if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
            continue
        raise RunbookError("AI runbook không đúng schema")
    allowed = {evidence_id for case in source["cases"] for evidence_id in case["evidence_ids"]}
    citations = result.get("citations")
    if not isinstance(citations, list) or not citations or any(
        not isinstance(value, str) or value not in allowed for value in citations
    ):
        raise RunbookError("AI runbook chứa citation không tồn tại trong evidence")
    return {
        field: (result[field].strip() if isinstance(result[field], str)
                else [item.strip() for item in result[field]])
        for field in fields
    } | {"citations": list(dict.fromkeys(citations)), "prompt_version": PROMPT_VERSION,
         "source_case_count": source["case_count"], "fault_family": source["fault_family"]}


async def generate(source: dict) -> dict:
    return validate(await _call_model(source), source)


async def generate_cached(source: dict, *, session=None) -> dict:
    """Load a matching report or make exactly one model call and cache it.

    ``session`` is injectable for callers/tests that already own a database
    transaction. HTTP callers omit it so the DB session is never held open
    while waiting on the model.
    """
    if session is None:
        with db.SessionLocal() as lookup_session:
            cached = get_cached(lookup_session, source)
    else:
        cached = get_cached(session, source)
    if cached is not None:
        return cached
    report = await generate(source)
    if session is None:
        with db.SessionLocal() as store_session:
            store_cached(store_session, source, report)
    else:
        store_cached(session, source, report)
    return report


def to_markdown(report: dict) -> str:
    """Render a validated report for copy/download without adding facts."""
    lines = [f"# {report['title']}", "", f"**Fault family:** `{report['fault_family']}`",
             f"**Evidence cases:** {report['source_case_count']}", ""]
    for title, key in (("Khi nào áp dụng", "when_to_use"), ("Pre-check", "prechecks"),
                       ("Các bước xử lý", "steps"), ("Xác minh", "verification"),
                       ("Rollback", "rollback"), ("Phòng ngừa", "prevention")):
        lines += [f"## {title}"]
        value = report[key]
        lines += ([f"- {item}" for item in value] if isinstance(value, list) else [value]) + [""]
    lines += ["## Giới hạn", report["limitations"], "", "## Evidence citations"]
    lines += [f"- `{citation}`" for citation in report["citations"]]
    return "\n".join(lines) + "\n"
