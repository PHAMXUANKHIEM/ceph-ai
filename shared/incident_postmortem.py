"""Evidence-only Incident timeline and citation-validated AI postmortem."""

from __future__ import annotations

import json
import re
from datetime import datetime

import httpx

from config.settings import settings
from shared.claude_cli import ClaudeCLIError, run_claude_prompt
from shared.codex_app_server import CodexAppServerError, codex_app_server
from shared.ai_provider_runtime import refresh_chat_provider_flags
from shared import db
from shared.models import Action, AuditEntry, Incident, IncidentTimelineEvent
from shared.router_client import build_router_client
from shared.ai_redaction import default_redactor
from shared.ai_observability import observe_ai_call, record_ai_usage

PROMPT_VERSION = "v1"
TOOL_NAME = "report_incident_postmortem"
TIMEOUT_SECONDS = 90
MAX_TOKENS = 4096
TERMINAL_STATUSES = {"RESOLVED", "AUTO_FIXED", "REJECTED"}
_SENSITIVE_KEY = re.compile(r"(?:password|secret|token|api.?key|keyring|credential)", re.I)


class PostmortemError(Exception):
    pass


def _safe_json(raw: str | None):
    try:
        value = json.loads(raw or "null")
    except (TypeError, ValueError):
        return None

    def scrub(item):
        if isinstance(item, dict):
            return {key: ("[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else scrub(value))
                    for key, value in item.items()}
        if isinstance(item, list):
            return [scrub(value) for value in item]
        return item
    return scrub(value)


def build_timeline(session, incident_id: str) -> dict:
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise PostmortemError("Không tìm thấy Incident")
    actions = session.query(Action).filter_by(incident_id=incident_id).order_by(Action.created_at, Action.id).all()
    audits = session.query(AuditEntry).filter_by(incident_id=incident_id).order_by(
        AuditEntry.created_at, AuditEntry.id
    ).all()
    lifecycle_events = session.query(IncidentTimelineEvent).filter_by(incident_id=incident_id).order_by(
        IncidentTimelineEvent.created_at, IncidentTimelineEvent.id
    ).all()
    events = [{
        "id": f"incident:{incident.id}:detected", "at": incident.detected_at.isoformat(),
        "kind": "detected", "actor": "watcher", "summary": f"Detected {incident.ceph_code}",
        "evidence": _safe_json(incident.signal_evidence_json),
    }]
    for action in actions:
        events.append({
            "id": f"action:{action.id}:created", "at": action.created_at.isoformat(),
            "kind": "action_proposed", "actor": "system", "action_id": action.action_id,
            "summary": action.rationale or action.action_id,
            "current_action_status": action.status, "classification": action.classification,
        })
    mirrored_audits = {event.source_id for event in lifecycle_events if event.source_type == "audit"}
    for entry in audits:
        if entry.id in mirrored_audits:
            continue
        events.append({
            "id": f"audit:{entry.id}", "at": entry.created_at.isoformat(),
            "kind": entry.event_type, "actor": entry.actor,
            "action_ref": entry.action_id, "summary": entry.event_type,
        })
    for event in lifecycle_events:
        events.append({
            "id": f"event:{event.id}", "at": event.created_at.isoformat(),
            "kind": event.event_type, "actor": event.actor,
            "action_ref": event.action_id, "summary": event.event_type,
            "evidence": _safe_json(event.evidence_json),
        })
    events.sort(key=lambda event: (event["at"], event["id"]))
    return {
        "incident_id": incident.id, "ceph_code": incident.ceph_code,
        "status": incident.status, "severity": incident.severity,
        "diagnosis_context": incident.diagnosis_text,
        "events": events,
    }


def _schema() -> dict:
    properties = {
        key: {"type": "string", "maxLength": 2000}
        for key in ("root_cause", "impact", "actions_taken", "verification", "prevention", "limitations")
    }
    properties["citations"] = {"type": "array", "items": {"type": "string"}, "maxItems": 30}
    return {"type": "function", "function": {"name": TOOL_NAME, "description": "Return evidence-only postmortem",
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}}}


@observe_ai_call("incident_postmortem")
async def _call_model(payload: dict) -> dict:
    refresh_chat_provider_flags()
    payload = default_redactor.redact(payload)
    schema = _schema()
    system = (
        "You write concise Vietnamese Ceph incident postmortems. Use only the supplied JSON. "
        "Never invent a time, cause, impact, action, or verification. Every factual conclusion must "
        "be supported by citations containing exact event IDs. State uncertainty in limitations."
    )
    user = "SOURCE_TIMELINE_JSON:\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    provider_errors: list[str] = []
    if settings.codex_chat_enabled:
        captured = {}
        async def capture(name, arguments):
            if name != TOOL_NAME:
                return "Tool không được phép", False
            captured.update(arguments)
            return "Đã ghi postmortem", True
        try:
            await codex_app_server.run_turn(system + "\nCall the tool exactly once.\n" + user, [schema], capture,
                                            timeout=TIMEOUT_SECONDS)
        except CodexAppServerError as exc:
            provider_errors.append(f"Codex call failed: {exc}")
        else:
            return captured
    if settings.claude_chat_enabled:
        try:
            raw = await run_claude_prompt(
                system + "\nReturn only JSON matching this schema:\n" +
                json.dumps(schema["function"]["parameters"], ensure_ascii=False) + "\n" + user,
                timeout=TIMEOUT_SECONDS,
            )
        except (ClaudeCLIError, json.JSONDecodeError) as exc:
            provider_errors.append(f"Claude call failed: {exc}")
        else:
            try:
                return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I))
            except (TypeError, ValueError) as exc:
                provider_errors.append(f"Claude response was not valid JSON: {exc}")
    router_ready = bool(
        settings.router_enabled
        and settings.router_api_key
        and settings.router_base_url
        and settings.router_model
    )
    if not router_ready:
        if provider_errors:
            raise PostmortemError("; ".join(provider_errors))
        raise PostmortemError("Router đang tắt hoặc chưa cấu hình đầy đủ")
    client = build_router_client(settings.router_api_key, settings.router_base_url)
    try:
        async with client.chat.completions.stream(
            model=settings.router_model, max_tokens=MAX_TOKENS, tools=[schema],
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            stream_options={"include_usage": True}, timeout=httpx.Timeout(TIMEOUT_SECONDS),
        ) as stream:
            completion = await stream.get_final_completion()
        record_ai_usage(completion)
    except Exception as exc:
        raise PostmortemError(f"Router call failed: {str(exc) or type(exc).__name__}") from exc
    for call in completion.choices[0].message.tool_calls or []:
        if call.function.name == TOOL_NAME:
            return json.loads(call.function.arguments or "{}")
    raise PostmortemError("AI response contained no postmortem tool call")


def validate_postmortem(result: dict, timeline: dict) -> dict:
    fields = ("root_cause", "impact", "actions_taken", "verification", "prevention", "limitations")
    if not isinstance(result, dict) or any(
        not isinstance(result.get(field), str) or not result[field].strip() for field in fields
    ):
        raise PostmortemError("AI postmortem không đúng schema")
    citations = result.get("citations")
    allowed = {event["id"] for event in timeline["events"]}
    if not isinstance(citations, list) or not citations or any(
        not isinstance(value, str) or value not in allowed for value in citations
    ):
        raise PostmortemError("AI postmortem chứa citation không tồn tại trong timeline")
    return {field: result[field].strip() for field in fields} | {"citations": list(dict.fromkeys(citations))}


async def generate(incident_id: str) -> dict:
    with db.SessionLocal() as session:
        timeline = build_timeline(session, incident_id)
    if timeline["status"] not in TERMINAL_STATUSES:
        raise PostmortemError("Chỉ tạo postmortem sau khi Incident đã kết thúc")
    result = validate_postmortem(await _call_model(timeline), timeline)
    with db.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        if incident is None:
            raise PostmortemError("Incident đã bị xoá trong lúc tạo postmortem")
        incident.postmortem_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
        incident.postmortem_generated_at = datetime.utcnow()
        incident.postmortem_prompt_version = PROMPT_VERSION
        session.commit()
    return result
