import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import yaml
from sqlalchemy.exc import IntegrityError

from config.settings import settings
from shared import audit, db, incident_events
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    ChatMessage,
    Cluster,
    Incident,
    IncidentStatus,
)
from shared.ceph_releases import RELEASES, codename_for_version
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
# ceph_code do monitor tự đặt không có mặt trong `ceph health detail` nên
# không thể xác minh bằng cách đối chiếu ở đó — xem module ấy.
from watcher.ceph_code_families import is_monitor_owned
from shared.router_client import build_router_client
from shared.codex_app_server import CodexAppServerError, codex_app_server
from shared.claude_cli import ClaudeCLIError, run_claude_prompt
from shared.telegram_alerts import (
    send_ai_incident_alert,
    send_auto_remediation_alert,
    send_update_failure_alert,
)
from worker.backup import engine as backup_engine
from worker.executor import cinder_reconciliation, cluster_deploy, commands, rbd_reconciliation, vm_perf, volume_perf
from worker.executor.ssh_executor import ExecutorError, execute_command
from worker.policy import gate
from worker.preflight import run_preflight
from worker.redaction import default_redactor

logger = logging.getLogger(__name__)

# 2026-08-07 (incident follow-up): was 1024 -- verified against this
# deployment's real worker.log that a reasoning-style model
# (gemini-3-flash-preview via 9router) spends an unpredictable amount of
# its max_tokens budget on hidden reasoning tokens BEFORE it ever emits the
# report_diagnosis tool call, so 1024 was routinely exhausted before any
# tool-call content came out -- every diagnose_incident() call that day
# failed with openai.LengthFinishReasonError on all 3 retries, meaning NO
# Incident got a diagnosis/proposal at all. Raised well above the tool
# schema's own actual output size (diagnosis_text/rationale/command are at
# most a few hundred tokens of JSON) to leave real headroom for reasoning
# tokens; still finite so a genuinely stuck/looping model call fails fast
# rather than running indefinitely.
MAX_TOKENS = 8192
ROUTER_TIMEOUT_SECONDS = 60.0
TOOL_NAME = "report_diagnosis"

_POLICY_PATH = Path(__file__).resolve().parent.parent / "policy" / "action_policy.yaml"


def _load_valid_action_ids() -> frozenset[str]:
    with open(_POLICY_PATH) as f:
        policy = yaml.safe_load(f)
    return frozenset(policy["action_ids"])


VALID_ACTION_IDS = _load_valid_action_ids()
# Incident AI must never recommend a placeholder that the executor cannot
# actually run. Management actions normally stay out of this schema, except
# enable_pool_application: POOL_APP_NOT_ENABLED has a dedicated parameter
# selection flow before execution.
AI_EXECUTABLE_ACTION_IDS = frozenset(
    action_id for action_id in VALID_ACTION_IDS if commands.has_command(action_id)
) | frozenset({"enable_pool_application"})
_POOL_APP_CODE = "POOL_APP_NOT_ENABLED"
_POOL_TOO_FEW_PGS_CODE = "POOL_TOO_FEW_PGS"
_OSD_UPGRADE_FINISHED_CODE = "OSD_UPGRADE_FINISHED"
_KNOWN_CEPH_CODENAMES = frozenset(row["codename"] for row in RELEASES.values())
_POOL_NAME_PATTERNS = (
    re.compile(r"pool ['\"]([^'\"]+)['\"]", re.IGNORECASE),
    re.compile(r"pool\s+([A-Za-z0-9_.-]+)", re.IGNORECASE),
)
_POOL_TOO_FEW_PGS_PATTERN = re.compile(
    r"Pool\s+['\"]?([A-Za-z0-9_.-]+)['\"]?\s+has\s+(\d+)\s+placement groups?,\s*"
    r"should have\s+(\d+)",
    re.IGNORECASE,
)


def _pool_pg_adjustments_from_health_detail(envelope: dict) -> list[dict]:
    """Extract Ceph's exact per-pool PG targets; never ask the LLM to guess them."""
    check = (
        ((envelope.get("cluster_snapshot") or {}).get("checks") or {}).get(
            _POOL_TOO_FEW_PGS_CODE
        )
        or {}
    )
    messages = [
        str(row.get("message") or "")
        for row in check.get("detail", [])
        if isinstance(row, dict)
    ]
    # Some Ceph versions/transports leave detail out of the snapshot but
    # preserve it in the incident excerpt.
    messages.append(str(envelope.get("log_excerpt") or ""))

    by_pool: dict[str, dict] = {}
    for message in messages:
        for match in _POOL_TOO_FEW_PGS_PATTERN.finditer(message):
            pool_name, current_raw, target_raw = match.groups()
            current, target = int(current_raw), int(target_raw)
            if 1 <= current < target <= 32768:
                by_pool[pool_name] = {
                    "pool_name": pool_name,
                    "current_pg_num": current,
                    "pg_num": target,
                }
    return list(by_pool.values())


def _pool_name_from_snapshot(envelope: dict) -> str | None:
    text = json.dumps(envelope.get("cluster_snapshot") or {}, ensure_ascii=False)
    for pattern in _POOL_NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def _osd_upgrade_finished_release(envelope: dict) -> str | None:
    """Extract only a known Ceph codename from OSD_UPGRADE_FINISHED evidence."""
    evidence = " ".join(
        (
            str(envelope.get("log_excerpt") or ""),
            json.dumps(envelope.get("cluster_snapshot") or {}, ensure_ascii=False),
        )
    ).lower()
    patterns = (
        r"all osds are running\s+([a-z][a-z0-9-]+)\s+or later",
        r"require_osd_release\s*(?:<|is below)\s*([a-z][a-z0-9-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, evidence)
        if match and match.group(1) in _KNOWN_CEPH_CODENAMES:
            return match.group(1)
    return None


def _warn_if_missing_api_key(
    api_key: str, *, codex_enabled: bool = False, claude_enabled: bool = False
) -> None:
    if not api_key and not codex_enabled and not claude_enabled:
        logger.warning(
            "ROUTER_API_KEY is not configured — every diagnose_incident() call "
            "will fail authentication and exhaust its retry budget until this is set"
        )


def _warn_if_missing_worker_ssh_key(key_path: str) -> None:
    if not os.path.exists(key_path):
        logger.warning(
            "ssh_key_path=%s does not exist — every SAFE action execution will "
            "fail late with a generic ExecutorError until this is fixed",
            key_path,
        )


_warn_if_missing_api_key(
    settings.router_api_key,
    codex_enabled=settings.codex_chat_enabled,
    claude_enabled=settings.claude_chat_enabled,
)
_warn_if_missing_worker_ssh_key(settings.ssh_key_path)

SYSTEM_PROMPT = (
    "You are an expert Ceph storage cluster SRE. Given a detected Ceph health "
    "incident (error code, relevant daemon log excerpt, cluster snapshot), "
    "diagnose the likely root cause in plain language an operator without "
    "deep Ceph internals knowledge can understand, and recommend exactly one "
    "remediation action from the fixed set provided in the tool schema. "
    "Every recommendation must be a concrete action the system can execute; "
    "never recommend manual investigation or a generic diagnostic check. "
    "Write diagnosis_text and rationale in concise, natural Vietnamese (at most "
    "two short sentences each). Keep Ceph error codes, daemon names, pool names, "
    "commands, paths, and technical identifiers unchanged instead of translating them."
)

# Story 3.2: when a redelivered message finds an Action that's already
# resolved, Incident.status (already overwritten to DIAGNOSING by
# worker/main.py::_handle_message before this call) is restored to match,
# rather than left stuck. Any ActionStatus not covered here (shouldn't
# happen) falls back to FAILED — conservative by default.
_INCIDENT_STATUS_FOR_RESOLVED_ACTION = {
    ActionStatus.AUTO_EXECUTED.value: IncidentStatus.AUTO_FIXED.value,
    ActionStatus.FAILED.value: IncidentStatus.FAILED.value,
    ActionStatus.PENDING_APPROVAL.value: IncidentStatus.PENDING_APPROVAL.value,
    ActionStatus.APPROVED.value: IncidentStatus.APPROVED.value,
    ActionStatus.EXECUTED.value: IncidentStatus.RESOLVED.value,
    ActionStatus.REJECTED.value: IncidentStatus.REJECTED.value,
}


class RouterDiagnosisError(Exception):
    """Raised for any invalid router response — missing tool call,
    truncated (max_tokens) response, missing required field, or an
    `action_id` outside the fixed enum (AD-5: never accept an out-of-enum
    id). Deliberately NOT retried here — it propagates to
    worker/main.py::_handle_message, which already owns retry/dead-letter
    handling (Story 2.1, AC #3)."""


def _tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Report the root-cause diagnosis and recommended remediation "
                "action for this Ceph incident."
            ),
            # strict + additionalProperties:false: verified against the
            # real 9router — a plain (non-strict) function schema and a
            # forced tool_choice on this exact router/model combo silently
            # got ignored (the model answered in plain text instead of
            # calling the tool at all) until the schema was marked strict.
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "diagnosis_text": {
                        "type": "string",
                        "description": (
                            "Plain-language explanation of the likely root cause, "
                            "understandable without reading raw Ceph docs. Must be a "
                            "concise Vietnamese summary of at most two short sentences."
                        ),
                    },
                    "action_id": {
                        "type": "string",
                        "enum": sorted(AI_EXECUTABLE_ACTION_IDS),
                        "description": "The single recommended remediation action.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": (
                            "Concise Vietnamese summary (at most two short sentences) of why "
                            "this action_id was chosen over the alternatives."
                        ),
                    },
                },
                "required": ["diagnosis_text", "action_id", "rationale"],
                "additionalProperties": False,
            },
        },
    }


def _osd_placement_line(payload: dict) -> str:
    """Dòng nói cho model biết osd nào nằm ở máy nào -- hoặc nói thẳng là
    KHÔNG BIẾT.

    2026-08-20 -- sửa lỗi có thật: trước đây prompt chỉ có
    `Affected nodes: ip1, ip2, ip3`, một danh sách phẳng không kèm ánh xạ
    nào. Model không có cách nào biết osd.2 nằm ở máy nào nên nó đoán, và
    sinh ra những câu chẩn đoán gán cả cụm OSD vào một địa chỉ sai
    ("osd.2, osd.4 và osd.5 trên node <ip sai>"). watcher/osd_hosts.py giờ
    tra đúng host qua systemd của từng node đã cấu hình.

    Khi không tra được, KHÔNG im lặng bỏ qua dòng này: im lặng đưa model
    trở lại đúng tình huống cũ (một danh sách phẳng, tự do suy diễn). Nói
    thẳng "chưa xác định được" và cấm suy đoán.
    """
    osd_hosts = payload.get("osd_hosts") or {}
    if osd_hosts:
        placement = ", ".join(
            f"osd.{osd_id} -> {host}" for osd_id, host in sorted(osd_hosts.items(), key=lambda kv: int(kv[0]))
        )
        return (
            f"OSD placement (đã tra trực tiếp trên node, chính xác): {placement}\n"
            "Chỉ được dùng đúng ánh xạ này khi nói osd nào nằm ở node nào.\n"
        )
    return (
        "OSD placement: CHƯA XÁC ĐỊNH ĐƯỢC.\n"
        "Danh sách node ở trên là toàn bộ node OSD của cụm, KHÔNG phải kết luận "
        "về vị trí của osd nào. Tuyệt đối không gán một osd_id cụ thể cho một "
        "node cụ thể trong phần chẩn đoán.\n"
    )


def _previous_attempts_block(payload: dict) -> str:
    """Liệt kê những lệnh đã chạy cho chính Incident này mà kiểm chứng cho
    thấy KHÔNG ăn thua (watcher/verify.py điền vào envelope ở vòng chẩn
    đoán thứ hai trở đi).

    2026-08-20 — thiếu khối này, vòng chẩn đoán lại gần như chắc chắn đề
    xuất đúng cái lệnh vừa thất bại: model không có cách nào biết nó đã
    được thử, vì log excerpt và ceph_code thì vẫn y hệt lần đầu.
    """
    attempts = payload.get("previous_attempts") or []
    if not attempts:
        return ""
    lines = ["Các lệnh ĐÃ THỬ cho sự cố này và ĐÃ KIỂM CHỨNG LÀ KHÔNG HẾT LỖI:"]
    for item in attempts:
        command = item.get("command") or "(không có lệnh tự động)"
        lines.append(f"  - {item.get('action_id')}: {command} (chạy lúc {item.get('executed_at')})")
    lines.append(
        "Đừng đề xuất lại đúng những lệnh trên. Hãy tìm nguyên nhân khác, "
        "hoặc nói rõ là cần vận hành viên can thiệp thủ công."
    )
    return "\n".join(lines) + "\n"


def _build_user_content(payload: dict) -> str:
    nodes = payload.get("nodes") or []
    return (
        f"{_previous_attempts_block(payload)}"
        f"Ceph error code: {payload.get('ceph_code')}\n"
        f"Detected at: {payload.get('detected_at')}\n"
        f"Affected nodes: {', '.join(nodes)}\n"
        f"{_osd_placement_line(payload)}"
        f"Cluster snapshot: {json.dumps(payload.get('cluster_snapshot', {}))}\n\n"
        f"Relevant daemon log excerpt:\n{payload.get('log_excerpt', '')}"
    )


def _get_client():
    return build_router_client(settings.router_api_key, settings.router_base_url)


async def _call_router(user_content: str) -> dict:
    """The single call site to 9router (AD-6's insertion point is the
    caller's redaction step, right before this function runs).

    Forces exactly one call to TOOL_NAME via tool_choice, so the model can
    never "answer" with plain text instead of the structured report —
    verified against a real running 9router instance (see _tool_schema's
    comment on why the schema must also be `strict`).

    `model` is read from settings at call time, not a module constant — the
    Dashboard's Settings page (worker/llm/router_client.py's caller,
    dashboard/routes/settings.py) can change it while the Worker process is
    already running, same as settings.ceph_osd_container_name elsewhere.

    This router (verified directly) always responds with an SSE stream
    regardless of whether streaming was requested — client.chat.completions
    .stream() + get_final_completion() reassembles the exact same
    ChatCompletion shape a plain non-streaming call would return, and works
    unchanged against a real non-streaming-only OpenAI-compatible endpoint
    too.
    """
    if settings.codex_chat_enabled:
        captured: dict = {}

        async def capture(tool_name: str, arguments: dict) -> tuple[str, bool]:
            if tool_name != TOOL_NAME:
                return f"Tool không được phép: {tool_name}", False
            captured.update(arguments)
            return "Đã ghi nhận chẩn đoán.", True

        prompt = (
            SYSTEM_PROMPT
            + "\n\nBạn BẮT BUỘC gọi tool report_diagnosis đúng một lần; không trả kết quả chỉ bằng văn bản.\n\n"
            + user_content
        )
        try:
            await codex_app_server.run_turn(prompt, [_tool_schema()], capture, timeout=ROUTER_TIMEOUT_SECONDS)
        except CodexAppServerError as exc:
            raise RouterDiagnosisError(f"Codex call failed: {exc}") from exc
        return captured

    if settings.claude_chat_enabled:
        prompt = (
            SYSTEM_PROMPT
            + "\n\nChỉ trả về một JSON object hợp lệ, không markdown, với đúng các trường "
            "diagnosis_text, action_id, rationale. action_id phải là một trong: "
            + ", ".join(sorted(AI_EXECUTABLE_ACTION_IDS))
            + "\n\n"
            + user_content
        )
        try:
            raw = await run_claude_prompt(prompt, timeout=ROUTER_TIMEOUT_SECONDS)
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
            result = json.loads(clean)
        except (ClaudeCLIError, json.JSONDecodeError) as exc:
            raise RouterDiagnosisError(f"Claude call failed: {exc}") from exc
        return result

    client = _get_client()
    try:
        async with client.chat.completions.stream(
            model=settings.router_model,
            max_tokens=MAX_TOKENS,
            tools=[_tool_schema()],
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            # httpx.Timeout(...), NOT a bare float — verified directly
            # against a real running 9router: passing a plain float here
            # silently truncated the streamed response on a .stream() call
            # specifically (no error raised, just a cut-off answer).
            # httpx.Timeout(...) does not have this problem.
            timeout=httpx.Timeout(ROUTER_TIMEOUT_SECONDS),
        ) as stream:
            completion = await stream.get_final_completion()
    except Exception as exc:
        raise RouterDiagnosisError(f"Router call failed: {exc}") from exc

    choice = completion.choices[0]
    if choice.finish_reason == "length":
        raise RouterDiagnosisError(
            f"Router response truncated at max_tokens={MAX_TOKENS} — tool call args may be incomplete"
        )
    for call in choice.message.tool_calls or []:
        if call.function.name == TOOL_NAME:
            try:
                return json.loads(call.function.arguments or "{}")
            except (TypeError, ValueError) as exc:
                raise RouterDiagnosisError(
                    f"report_diagnosis arguments were not valid JSON: {call.function.arguments!r}"
                ) from exc
    raise RouterDiagnosisError("Router response contained no report_diagnosis tool call")


def _compute_idempotency_key(
    action_id: str, nodes: list | None, action_params: dict | None
) -> str:
    """AI roadmap Pha 0.4: deterministic hash of the real-world command
    this Action represents — `action_id` + target nodes + params —
    deliberately EXCLUDING incident_id, so it's the same key regardless of
    WHICH Incident proposed it (see Action.idempotency_key's own docstring
    in shared/models.py for why that's the point: catching a duplicate
    proposal across two different incident_ids, not just a re-run of the
    same one). `nodes` sorted before hashing since envelope["nodes"]'s
    order carries no meaning; `action_params`/keys sorted via
    `sort_keys=True` for the same reason."""
    payload = json.dumps(
        {
            "action_id": action_id,
            "nodes": sorted(nodes) if isinstance(nodes, list) else None,
            "action_params": action_params or None,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def diagnose_incident(incident_id: str, envelope: dict) -> None:
    """Real production `process_incident` callback (Story 2.3) — replaces
    `worker/main.py::default_process_incident`. Redacts (AD-6, single call
    site), calls the router via forced tool-use, validates the structured
    result, persists `diagnosis_text`, classifies the recommended action
    (Story 3.1), and — for a SAFE classification — executes it (Story 3.2).

    Raises on any failure UP TO AND INCLUDING classification (API error/
    timeout, malformed or out-of-enum response) — worker/main.py's existing
    retry/DLX logic (Story 2.1) is what handles this, unchanged. Anything
    from execution onward never raises (see _maybe_execute_safe_action).
    """
    payload = default_redactor.redact(envelope)
    user_content = _build_user_content(payload)

    result = await _call_router(user_content)

    diagnosis_text = (result.get("diagnosis_text") or "").strip()
    action_id = (result.get("action_id") or "").strip()
    rationale = (result.get("rationale") or "").strip()
    deterministic_code = envelope.get("ceph_code") in {
        _OSD_UPGRADE_FINISHED_CODE,
        _POOL_TOO_FEW_PGS_CODE,
    }
    if (
        not diagnosis_text
        or not rationale
        or (not deterministic_code and action_id not in AI_EXECUTABLE_ACTION_IDS)
    ):
        raise RouterDiagnosisError(
            f"invalid router response for incident {incident_id}: "
            f"action_id={action_id!r}, diagnosis_text={diagnosis_text!r}, rationale={rationale!r}"
        )

    osd_release = None
    pool_pg_adjustments = None
    if envelope.get("ceph_code") == _OSD_UPGRADE_FINISHED_CODE:
        osd_release = _osd_upgrade_finished_release(envelope)
        if osd_release is None:
            raise RouterDiagnosisError(
                "OSD_UPGRADE_FINISHED không chứa release Ceph đã biết; từ chối đoán lệnh require-osd-release"
            )
        action_id = "finalize_osd_release"
        rationale = (
            f"Tất cả OSD đã chạy {osd_release} hoặc mới hơn nhưng require_osd_release vẫn thấp hơn; "
            f"đề xuất chạy `ceph osd require-osd-release {osd_release}` để hoàn tất nâng cấp và xoá cảnh báo."
        )
    elif envelope.get("ceph_code") == _POOL_TOO_FEW_PGS_CODE:
        pool_pg_adjustments = _pool_pg_adjustments_from_health_detail(envelope)
        if not pool_pg_adjustments:
            raise RouterDiagnosisError(
                "POOL_TOO_FEW_PGS không chứa pool/PG mục tiêu hợp lệ; từ chối đoán tham số lệnh"
            )
        action_id = "set_pool_pg_num"
        changes = ", ".join(
            f"{item['pool_name']}: {item['current_pg_num']} -> {item['pg_num']}"
            for item in pool_pg_adjustments
        )
        rationale = (
            "Ceph health detail đã chỉ rõ PG mục tiêu cho từng pool; "
            f"đề xuất điều chỉnh đúng các giá trị này ({changes})."
        )
    logger.info("diagnose_incident: incident %s rationale: %s", incident_id, rationale)

    action_params: dict | None = None
    with db.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        if incident is None:
            logger.warning(
                "diagnose_incident: no Incident row for id=%s — skipping DB write", incident_id
            )
            return
        incident.diagnosis_text = diagnosis_text
        incident_events.record(
            session, incident_id=incident_id, event_type="diagnosis_completed",
            actor="ai", evidence={"prompt_version": "incident-diagnosis-v1"},
        )
        alert_ceph_code = incident.ceph_code
        alert_severity = incident.severity
        alert_cluster_name = None
        alert_bot_token = None
        alert_chat_id = None
        alert_enabled = None
        if incident.cluster_id is not None:
            alert_cluster = session.get(Cluster, incident.cluster_id)
            if alert_cluster is not None:
                alert_cluster_name = alert_cluster.name
                if alert_cluster.telegram_bot_token and alert_cluster.telegram_chat_id:
                    alert_bot_token = alert_cluster.telegram_bot_token
                    alert_chat_id = alert_cluster.telegram_chat_id
                    alert_enabled = alert_cluster.telegram_enabled

        # Guard against duplicate/conflicting Action rows if this incident
        # gets diagnosed more than once (e.g. a message redelivered after
        # Story 2.1's retry logic, or an ack() failure triggering
        # reprocessing) — but distinguish WHY an Action already exists:
        existing_action = session.query(Action).filter_by(incident_id=incident_id).one_or_none()
        if existing_action is not None:
            if existing_action.status == ActionStatus.PENDING.value:
                # Crash-recovery: an Action was classified on a prior attempt
                # but the process died before execution ran/completed.
                # Reuse it and retry execution — otherwise it would be
                # stranded behind this guard forever (created, never
                # executed, never marked FAILED).
                logger.warning(
                    "diagnose_incident: found existing PENDING Action for incident %s "
                    "(likely redelivered before execution completed) — retrying execution",
                    incident_id,
                )
                action_pk = existing_action.id
                classification = ActionClassification(existing_action.classification)
                resolved_action_id = existing_action.action_id
                try:
                    action_params = json.loads(existing_action.action_params or "null")
                except (TypeError, ValueError):
                    action_params = None
                session.commit()
            else:
                # Already resolved by a prior attempt — restore
                # Incident.status to match (worker/main.py::_handle_message
                # just overwrote it to DIAGNOSING before this call ran), and
                # do NOT re-execute or reclassify.
                logger.warning(
                    "diagnose_incident: Action for incident %s already resolved "
                    "(status=%s) — restoring Incident.status, not re-executing",
                    incident_id,
                    existing_action.status,
                )
                incident.status = _INCIDENT_STATUS_FOR_RESOLVED_ACTION.get(
                    existing_action.status, IncidentStatus.FAILED.value
                )
                session.commit()
                return
        else:
            # AI roadmap Pha 0.3: fail-closed capability/version preflight,
            # run BEFORE classification/Action creation for every fresh
            # proposal — see worker/preflight.py's own module docstring for
            # what the 3 checks cover. Enforcement defaults on: unknown or
            # stale compatibility evidence must fail closed.
            preflight = run_preflight(session, cluster_id=incident.cluster_id, action_id=action_id)
            if not preflight.allowed:
                if settings.ai_preflight_enforcement_enabled:
                    logger.warning(
                        "diagnose_incident: preflight BLOCKED action_id=%s for incident %s: %s",
                        action_id, incident_id, preflight.reason,
                    )
                    incident.diagnosis_text = (
                        f"{diagnosis_text}\n\n[Preflight — Pha 0.3] Bị chặn: {preflight.reason}"
                    )
                    incident.status = IncidentStatus.FAILED.value
                    audit.record(
                        session,
                        incident_id=incident_id,
                        action_id=None,
                        event_type=audit.EVENT_PROPOSAL_BLOCKED_BY_PREFLIGHT,
                        actor=audit.ACTOR_SYSTEM,
                    )
                    session.commit()
                    return
                logger.warning(
                    "diagnose_incident: preflight WOULD block action_id=%s for incident %s "
                    "(ai_preflight_enforcement_enabled=false, allowing) — %s",
                    action_id, incident_id, preflight.reason,
                )

            classification = gate.classify_action(action_id)
            # BLUESTORE_SLOW_OP_ALERT is safe to self-heal only after the
            # watcher has extracted concrete osd.N values from health detail
            # and independently mapped every one to an SSH-able host.  This
            # contextual exception does not make generic OSD restarts SAFE.
            bluestore_osd_hosts = envelope.get("osd_hosts") or {}
            bluestore_details = (
                envelope.get("cluster_snapshot", {}).get("checks", {})
                .get("BLUESTORE_SLOW_OP_ALERT", {}).get("detail", [])
            )
            bluestore_osd_ids = sorted({
                int(value)
                for item in bluestore_details if isinstance(item, dict)
                for value in re.findall(r"osd\.(\d+)", str(item.get("message", "")))
            })
            cephadm_verified = (
                envelope.get("ceph_exec_mode") == "cephadm"
                and bool(bluestore_osd_ids)
            )
            verified_bluestore_restart = (
                incident.ceph_code == "BLUESTORE_SLOW_OP_ALERT"
                and action_id == "restart_osd_daemon"
                and (
                    cephadm_verified
                    or (
                        isinstance(bluestore_osd_hosts, dict)
                        and bool(bluestore_osd_hosts)
                        and all(str(osd_id).isdigit() and isinstance(host, str) and host
                                for osd_id, host in bluestore_osd_hosts.items())
                    )
                )
            )
            if verified_bluestore_restart:
                classification = ActionClassification.SAFE
            pool_choice_required = (
                incident.ceph_code == _POOL_APP_CODE and action_id == "enable_pool_application"
            )
            pool_name = _pool_name_from_snapshot(envelope) if pool_choice_required else None
            if pool_choice_required:
                # RBD/CephFS/RGW cannot be inferred safely from the warning.
                # Park this as a Telegram choice even though the generic
                # policy classifies the metadata update as SAFE.
                classification = ActionClassification.RISKY
            if pool_pg_adjustments is not None:
                # Increasing PGs redistributes data, so an incident-driven
                # proposal must be explicitly approved even though the same
                # management action from operator-confirmed Chat is SAFE.
                classification = ActionClassification.RISKY
            nodes = envelope.get("nodes")
            if action_id == "finalize_osd_release" and isinstance(nodes, list):
                nodes = nodes[:1]
            action_params = None
            if osd_release is not None:
                action_params = {"release": osd_release}
            elif pool_pg_adjustments is not None:
                action_params = {"adjustments": pool_pg_adjustments}
            elif pool_name:
                action_params = {"pool_name": pool_name}
            elif verified_bluestore_restart:
                if cephadm_verified:
                    # ceph orch runs through one reachable MON and asks the
                    # orchestrator to restart the exact remote daemon.  It
                    # does not require direct SSH access to the OSD host.
                    nodes = nodes[:1]
                    action_params = {"cephadm_osd_ids": bluestore_osd_ids}
                else:
                    by_host: dict[str, list[int]] = {}
                    for osd_id, host in bluestore_osd_hosts.items():
                        by_host.setdefault(host, []).append(int(osd_id))
                    nodes = list(by_host)
                    action_params = {"osd_ids_by_host": by_host}
            action = Action(
                incident_id=incident_id,
                action_id=action_id,
                classification=classification.value,
                status=ActionStatus.PENDING.value,
                rationale=rationale,
                # Story 4.2: persisted so the approved-RISKY execution path
                # (Story 4.3), which runs from a DB poll long after this
                # envelope is gone, still knows which host(s) to target.
                target_nodes=json.dumps(nodes) if isinstance(nodes, list) else None,
                action_params=json.dumps(action_params) if action_params else None,
                # AI roadmap Pha 0.4 (section 3.3): expiry for the stale-
                # evidence check on approval, idempotency_key for the
                # in-flight-duplicate DB guard — see Action's own column
                # docstrings in shared/models.py for both.
                expires_at=datetime.utcnow() + timedelta(hours=settings.action_approval_expiry_hours),
                idempotency_key=_compute_idempotency_key(action_id, nodes, action_params),
            )
            session.add(action)
            try:
                session.commit()
            except IntegrityError:
                # Pha 0.4's uq_actions_idempotency_key_inflight partial
                # unique index fired — a DIFFERENT, still in-flight Action
                # already proposes the exact same command against the same
                # target (e.g. two near-simultaneous Incidents on the same
                # node both diagnosing to resync_ntp). The existing_action
                # guard above only catches a re-run for THIS SAME
                # incident_id; this is the cross-incident case. Roll back
                # and treat like the "already resolved" branch above — no
                # second Action, no second alert/approval card for a
                # command that's already proposed and pending.
                session.rollback()
                logger.warning(
                    "diagnose_incident: idempotency_key collision for incident %s, "
                    "action_id=%s — an equivalent Action is already in flight, skipping",
                    incident_id, action_id,
                )
                return
            action_pk = action.id  # read while session is still open
            resolved_action_id = action_id

    # The old Watcher alert was deliberately sent before AI ran, leaving
    # operators with a raw log-only warning.  This is now the primary alert;
    # SAFE execution outcomes and RISKY approval cards remain follow-ups.
    send_ai_incident_alert(
        alert_ceph_code,
        alert_severity,
        diagnosis_text,
        rationale,
        cluster_name=alert_cluster_name,
        bot_token=alert_bot_token,
        chat_id=alert_chat_id,
        enabled=alert_enabled,
    )

    if classification == ActionClassification.SAFE:
        if not settings.autopilot_enabled:
            _route_safe_to_approval(incident_id, action_pk, resolved_action_id)
            return
        try:
            _maybe_execute_safe_action(
                incident_id, action_pk, resolved_action_id, envelope, action_params
            )
        except Exception:
            # Belt-and-suspenders: _maybe_execute_safe_action is designed to
            # never raise, but if it somehow does (a bug, an unrelated DB
            # outage), this must still end in a visible FAILED state rather
            # than propagate into Story 2.1's Router-failure retry/DLX path.
            logger.exception(
                "diagnose_incident: unexpected error executing SAFE action for incident %s "
                "— marking FAILED",
                incident_id,
            )
            try:
                _record_execution_result(incident_id, action_pk, command=None, succeeded=False)
            except Exception:
                # This recovery call must never raise either — if it somehow
                # does, swallow it here rather than let it escape into Story
                # 2.1's Router-failure retry/DLX path (Action/Incident status
                # may be left stuck at PENDING; the log line is the record).
                logger.exception(
                    "diagnose_incident: failed to record the FAILED outcome for incident %s "
                    "after an unexpected execution error",
                    incident_id,
                )
        return

    # 2026-07-24: a cluster upgrade/patch install restarting every daemon
    # one host at a time routinely trips transient OSD_DOWN/MGR_DOWN
    # incidents for the whole run — surfacing each as a new RISKY proposal
    # means an operator has to manually reject a stream of them just to let
    # the upgrade they already approved keep going. Skip proposing while
    # one of those is in flight; normal proposal resumes on its own once it
    # leaves PENDING_APPROVAL/APPROVED (no separate "turn back on" step).
    if _is_disruptive_cluster_operation_in_flight():
        _auto_reject_risky_during_cluster_operation(incident_id, action_pk, resolved_action_id)
        return

    # Story 4.2: RISKY -> PENDING_APPROVAL — never auto-executed (FR8), just
    # surfaced on the Dashboard for an operator to Approve/Reject (Story 4.3).
    try:
        _route_risky_to_approval(incident_id, action_pk, resolved_action_id)
    except Exception:
        logger.exception(
            "diagnose_incident: unexpected error routing RISKY action to approval for "
            "incident %s — marking FAILED",
            incident_id,
        )
        try:
            _record_execution_result(incident_id, action_pk, command=None, succeeded=False)
        except Exception:
            logger.exception(
                "diagnose_incident: failed to record the FAILED outcome for incident %s "
                "after an unexpected routing error",
                incident_id,
            )


def _maybe_execute_safe_action(
    incident_id: str, action_pk: str, action_id: str, envelope: dict,
    action_params: dict | None = None,
) -> None:
    """Run a SAFE action on every target node.

    Deliberately never raises — an execution failure ends in Action/Incident
    status=FAILED, not a retry (AC #3: retry/DLX is for transient Router
    failures, not for a command that's unlikely to succeed on a bare retry).
    """
    # Check again at the final execution boundary. This closes the race where
    # the kill switch changes after diagnosis but before SSH dispatch, and
    # protects future callers that forget the higher-level autonomy gate.
    if not settings.autopilot_enabled:
        logger.warning(
            "_maybe_execute_safe_action: global Autopilot kill switch blocked action_id=%s "
            "for incident %s; routing to approval", action_id, incident_id,
        )
        _route_safe_to_approval(incident_id, action_pk, action_id)
        return
    if settings.ai_preflight_enforcement_enabled:
        with db.SessionLocal() as session:
            incident = session.get(Incident, incident_id)
            result = run_preflight(
                session,
                cluster_id=incident.cluster_id if incident is not None else None,
                action_id=action_id,
            )
            if not result.allowed:
                action = session.get(Action, action_pk)
                if action is not None:
                    action.status = ActionStatus.PENDING_APPROVAL.value
                if incident is not None:
                    incident.status = IncidentStatus.PENDING_APPROVAL.value
                    incident.diagnosis_text = (
                        f"{incident.diagnosis_text or ''}\n\n"
                        f"[Execution preflight] Bị chặn: {result.reason}"
                    ).strip()
                    audit.record(
                        session, incident_id=incident_id,
                        action_id=action_pk if action is not None else None,
                        event_type=audit.EVENT_PROPOSAL_BLOCKED_BY_PREFLIGHT,
                        actor=audit.ACTOR_SYSTEM,
                    )
                session.commit()
                logger.warning(
                    "_maybe_execute_safe_action: execution-time preflight blocked action_id=%s "
                    "for incident %s: %s", action_id, incident_id, result.reason,
                )
                return
    # AI roadmap Pha 0.4 hard invariant (section 3.3: "RISKY/DESTRUCTIVE
    # phải phê duyệt riêng; không được tự chạy từ Chat hoặc nội dung AI"):
    # the ONLY caller of this function already gates on
    # `classification == ActionClassification.SAFE` (classify_action can
    # never return SAFE for a destructive: entry, see that function's own
    # precedence docstring) — this is redundant today, but a second,
    # independent check right here, on the actual execution path, means a
    # future bug in the CALLER's gating (a copy-paste, a refactor that
    # drops the check) still can't auto-run something DESTRUCTIVE. Belt-
    # and-suspenders, same posture as this function's own docstring above.
    if gate.classify_action(action_id) == ActionClassification.DESTRUCTIVE:
        logger.error(
            "_maybe_execute_safe_action: REFUSING to execute action_id=%s for incident %s — "
            "classify_action() says DESTRUCTIVE; this function must never auto-run a "
            "DESTRUCTIVE action (Pha 0.4 hard invariant)",
            action_id, incident_id,
        )
        _record_execution_result(incident_id, action_pk, command=None, succeeded=False)
        return
    nodes = envelope.get("nodes")
    if not isinstance(nodes, list) or not nodes or not all(
        isinstance(host, str) and host for host in nodes
    ):
        logger.warning(
            "diagnose_incident: envelope nodes is missing or malformed for incident %s "
            "(action_id=%s) — marking FAILED instead of guessing",
            incident_id,
            action_id,
        )
        _record_execution_result(incident_id, action_pk, command=None, succeeded=False)
        return

    # 2026-08-10 (multi-tenant remediation Phase 1): the envelope carries the
    # ORIGINATING cluster's own SSH creds (watcher/publisher.py::
    # build_envelope) — every execute_command() call below uses these
    # explicitly instead of implicitly falling back to settings.ssh_user/
    # settings.ssh_key_path (the default cluster), so a non-default
    # cluster's Incident never silently runs against the wrong credentials.
    ssh_user = envelope.get("ssh_user") or None
    ssh_key_path = envelope.get("ssh_key_path") or None

    executed_any = False
    all_succeeded = True
    # The command actually run — resolved per-host below rather than once
    # up front, because restart_osd_daemon's cephadm-mode command depends on
    # which host's OSD daemon name(s) get discovered (see
    # worker/executor/commands.py::get_command). Every other action_id's
    # command is identical regardless of host, so this changes nothing for
    # them beyond calling a pure function once per node instead of once
    # total.
    last_command: str | None = None

    for host in nodes:
        try:
            command = commands.get_command(action_id, host, action_params)
        except ExecutorError:
            logger.exception(
                "diagnose_incident: no Command for action_id=%s on host=%s (incident %s) "
                "— marking this node failed",
                action_id,
                host,
                incident_id,
            )
            all_succeeded = False
            continue
        last_command = command

        try:
            execute_command(host, command, user=ssh_user, key_path=ssh_key_path)
            executed_any = True
        except ExecutorError:
            logger.exception(
                "diagnose_incident: execution of action_id=%s failed on node %s "
                "(incident %s)",
                action_id,
                host,
                incident_id,
            )
            all_succeeded = False
            executed_any = True
            # Keep trying remaining nodes so the log shows exactly which
            # ones failed, but the overall Action is already FAILED — any
            # node failing means no partial success (AC #3, conservative).

    _record_execution_result(
        incident_id, action_pk, command=last_command, succeeded=all_succeeded and executed_any
    )


def _record_execution_result(
    incident_id: str, action_pk: str, command: str | None, succeeded: bool
) -> None:
    # Captured while the session is open so the Telegram notification below
    # (best-effort network I/O) can run AFTER commit/close — same "don't hold
    # a DB connection open across unrelated network I/O" posture
    # watcher/main.py::build_and_publish_incident's own docstring already
    # establishes for this codebase.
    notify_ceph_code: str | None = None
    notify_diagnosis: str | None = None
    notify_rationale: str | None = None
    # 2026-08-10 (multi-tenant remediation Phase 2): None/None/None means
    # "use the 3 global settings.telegram_incident_* fields" (unchanged
    # default-cluster behavior) — overridden below only when this Incident
    # belongs to a non-default cluster that has configured its own channel.
    notify_bot_token: str | None = None
    notify_chat_id: str | None = None
    notify_enabled: bool | None = None

    with db.SessionLocal() as session:
        action = session.get(Action, action_pk)
        incident = session.get(Incident, incident_id)
        if action is None:
            logger.warning(
                "diagnose_incident: no Action row for pk=%s (incident %s) while recording "
                "execution result — SSH side effects (if any) are untracked",
                action_pk,
                incident_id,
            )
        else:
            action.proposed_command = command
            action.status = (
                ActionStatus.AUTO_EXECUTED.value if succeeded else ActionStatus.FAILED.value
            )
            if succeeded:
                action.executed_at = datetime.utcnow()
            notify_rationale = action.rationale
        if incident is None:
            # AuditEntry.incident_id is a required FK — there is nothing
            # valid to attach an audit row to, so skip it too (see the same
            # reasoning in _route_to_manual_approval above).
            logger.warning(
                "diagnose_incident: no Incident row for id=%s while recording execution "
                "result — skipping audit.record() too (no valid Incident to attach it to)",
                incident_id,
            )
        else:
            incident.status = (
                IncidentStatus.VERIFYING.value if succeeded else IncidentStatus.FAILED.value
            )
            if succeeded:
                incident.verify_after = datetime.utcnow() + timedelta(
                    seconds=settings.incident_verify_delay_seconds
                )
            audit.record(
                session,
                incident_id=incident_id,
                # action_id must reference a real row when non-None (FK) —
                # fall back to None rather than the stale action_pk if the
                # Action row doesn't exist.
                action_id=action_pk if action is not None else None,
                event_type=(
                    audit.EVENT_SAFE_ACTION_EXECUTED if succeeded else audit.EVENT_SAFE_ACTION_FAILED
                ),
                actor=audit.ACTOR_SYSTEM,
            )
            notify_ceph_code = incident.ceph_code
            notify_diagnosis = incident.diagnosis_text
            if incident.cluster_id is not None:
                cluster = session.get(Cluster, incident.cluster_id)
                if cluster is not None and cluster.telegram_bot_token and cluster.telegram_chat_id:
                    notify_bot_token = cluster.telegram_bot_token
                    notify_chat_id = cluster.telegram_chat_id
                    notify_enabled = cluster.telegram_enabled
        session.commit()

    # Best-effort, never raises (see shared/telegram_alerts.py's own
    # docstring) — a SAFE action's Telegram follow-up must never affect the
    # already-decided Action/Incident status above. No-op if there was no
    # Incident row to report against.
    if notify_ceph_code is not None:
        send_auto_remediation_alert(
            notify_ceph_code,
            notify_diagnosis,
            notify_rationale,
            command,
            succeeded,
            bot_token=notify_bot_token,
            chat_id=notify_chat_id,
            enabled=notify_enabled,
        )


_DISRUPTIVE_CLUSTER_OPERATION_ACTION_IDS = (
    gate.VALID_CLUSTER_UPGRADE_ACTION_IDS | gate.VALID_PATCH_ACTION_IDS
)

# 2026-07-28: the two ceph-deploy/package-based upgrade action_ids (see
# dashboard/routes/upgrade.py's PACKAGE_DOWNLOAD_ACTION_ID/PACKAGE_LOCAL_ACTION_ID)
# have no orchestrator behind them — unlike `ceph orch upgrade` (cephadm),
# which automatically runs `ceph osd require-osd-release <codename>` as its
# own last step once every OSD reports the new version, installing/
# restarting packages node-by-node never bumps that flag on its own. Left
# alone, the cluster is left permanently sitting in HEALTH_WARN
# (OSD_UPGRADE_FINISHED: "all OSDs are running <release> or later but
# require_osd_release < <release>") even though the upgrade itself fully
# succeeded — verified live against a real ceph-deploy Nautilus->Octopus
# upgrade this session. Idempotent, metadata-only, no daemon restart — same
# "safe to always run, not gated behind operator choice" posture as
# cluster_deploy.py's own _phase_ceph_deploy_mon_security.
_PACKAGE_UPGRADE_ACTION_IDS = frozenset(
    {"upgrade_ceph_cluster_package_download", "upgrade_ceph_cluster_package_local"}
)


def _finalize_package_upgrade_osd_release(
    action_pk: str, action_params: dict, progress: list[dict], cluster: Cluster | None = None
) -> None:
    """Runs `ceph osd require-osd-release <codename>` once, via the first
    configured MON node, after every target node's install+restart step
    above has already been attempted — see _PACKAGE_UPGRADE_ACTION_IDS'
    comment for why this is needed at all. Best-effort: a failure here
    (e.g. this one MON temporarily unreachable) must not retroactively mark
    an otherwise-successful multi-node package upgrade as FAILED — the real
    daemons are already upgraded either way; only appended to `progress` (so
    it's visible on the Upgrade page / Markdown log) and logged.
    """
    target_version = (action_params or {}).get("target_version")
    codename = codename_for_version(target_version) if target_version else None
    if not codename:
        logger.warning(
            "_finalize_package_upgrade_osd_release: no codename for target_version=%r "
            "(action %s) — skipping require-osd-release finalization",
            target_version,
            action_pk,
        )
        return

    mon_raw = settings.ceph_mon_nodes if cluster is None else cluster.ceph_mon_nodes
    mon_nodes = [h.strip() for h in mon_raw.split(",") if h.strip()]
    if not mon_nodes:
        logger.warning(
            "_finalize_package_upgrade_osd_release: no MON node configured — skipping "
            "require-osd-release finalization for action %s",
            action_pk,
        )
        return
    mon_host = mon_nodes[0]

    command = f"ceph osd require-osd-release {codename}"
    step = {
        "host": mon_host,
        "status": "running",
        "command": command,
        "started_at": datetime.utcnow().isoformat(),
    }
    progress.append(step)
    _write_action_progress(action_pk, progress)

    try:
        _execute_for_cluster(mon_host, command, cluster)
    except ExecutorError as exc:
        logger.warning(
            "_finalize_package_upgrade_osd_release: %s failed on %s (action %s) — cluster "
            "will keep reporting OSD_UPGRADE_FINISHED until run manually: %s",
            command,
            mon_host,
            action_pk,
            exc,
        )
        step["status"] = "failed"
        step["error"] = str(exc)
    else:
        step["status"] = "done"
    step["finished_at"] = datetime.utcnow().isoformat()
    _write_action_progress(action_pk, progress)


# 2026-08-04: package-based Cluster Upgrade has no orchestrator behind it
# (see _PACKAGE_UPGRADE_ACTION_IDS' comment above) — nothing suppresses
# scrub/backfill/PG-autoscale churn while OSDs bounce one host at a time,
# unlike a real production upgrade runbook. Verified against Ceph's own
# cephadm orchestrator source (src/pybind/mgr/cephadm/upgrade.py) that even
# `ceph orch upgrade start` does NOT set/unset these on its own (it only
# manages `noautoscale`) — this is a known, still-open gap in cephadm
# itself, not something this app can rely on the orchestrator for either.
_UPGRADE_OSD_FLAGS = ("noout", "noscrub", "nodeep-scrub", "nosnaptrim")


def _set_upgrade_osd_flags(
    action_pk: str, mon_host: str, progress: list[dict], cluster: Cluster | None = None
) -> None:
    """Sets _UPGRADE_OSD_FLAGS before a package-based upgrade's per-host
    loop starts. `&&`-chained (stop at the first failure) — if the MON
    can't even take a `ceph osd set` right now, something more fundamental
    is wrong and attempting the rest wouldn't help. Best-effort like
    _finalize_package_upgrade_osd_release: a failure here must not abort
    an otherwise-approved, expected package upgrade — logged and recorded
    in `progress`, never raised."""
    command = " && ".join(f"ceph osd set {flag}" for flag in _UPGRADE_OSD_FLAGS)
    step = {
        "host": mon_host,
        "status": "running",
        "command": command,
        "started_at": datetime.utcnow().isoformat(),
    }
    # Appended, NOT inserted at the front — `progress` already has one
    # "pending" placeholder PER HOST at fixed indices the per-host loop
    # below addresses positionally (`progress[node_index - 1]`, node_index
    # from `enumerate(nodes, ...)`); inserting this step at index 0 would
    # shift every one of those indices by one and corrupt the per-host
    # loop's own writes into the wrong (this) entry. Same "append at the
    # end regardless of real execution order" posture
    # _finalize_package_upgrade_osd_release already established for its
    # own (also last-executed, also appended) step.
    progress.append(step)
    _write_action_progress(action_pk, progress)

    try:
        _execute_for_cluster(mon_host, command, cluster)
    except ExecutorError as exc:
        logger.warning(
            "_set_upgrade_osd_flags: %s failed on %s (action %s) — proceeding with the "
            "upgrade anyway, but scrub/backfill/autoscale are NOT suppressed during it: %s",
            command,
            mon_host,
            action_pk,
            exc,
        )
        step["status"] = "failed"
        step["error"] = str(exc)
    else:
        step["status"] = "done"
    step["finished_at"] = datetime.utcnow().isoformat()
    _write_action_progress(action_pk, progress)


def _unset_upgrade_osd_flags(
    action_pk: str, mon_host: str, progress: list[dict], cluster: Cluster | None = None
) -> None:
    """Always attempted after a package-based upgrade's per-host loop ends
    host ran — unsetting an already-unset flag is a harmless no-op, so
    this doesn't try to track whether _set_upgrade_osd_flags actually
    succeeded first. `;`-joined (NOT `&&`) so every flag gets its own
    unset attempt regardless of an earlier one failing — maximizes cleanup
    at the cost of the step's own recorded status only ever reflecting the
    LAST flag's outcome (acceptable for a best-effort cleanup step: see
    this function's own error log for the full command either way).
    Best-effort, same posture as _set_upgrade_osd_flags: logged, not
    raised — leaving flags set is undesirable but must not retroactively
    fail an otherwise-finished upgrade."""
    command = "; ".join(f"ceph osd unset {flag}" for flag in _UPGRADE_OSD_FLAGS)
    step = {
        "host": mon_host,
        "status": "running",
        "command": command,
        "started_at": datetime.utcnow().isoformat(),
    }
    progress.append(step)
    _write_action_progress(action_pk, progress)

    try:
        _execute_for_cluster(mon_host, command, cluster)
    except ExecutorError as exc:
        logger.warning(
            "_unset_upgrade_osd_flags: %s failed on %s (action %s) — noout/noscrub/"
            "nodeep-scrub/nosnaptrim may still be set on the cluster, unset manually: %s",
            command,
            mon_host,
            action_pk,
            exc,
        )
        step["status"] = "failed"
        step["error"] = str(exc)
    else:
        step["status"] = "done"
    step["finished_at"] = datetime.utcnow().isoformat()
    _write_action_progress(action_pk, progress)


# --- Story 7.2 (2026-08-04): phased MON->MGR->OSD->MDS/RGW package upgrade -
#
# Package-based Cluster Upgrade (`upgrade_ceph_cluster_package_download`/
# `_local` — see _PACKAGE_UPGRADE_ACTION_IDS above) used to run ONE combined
# "install && restart everything this host runs" command per host, in
# `configured_nodes()`'s host order (MON-then-MGR-then-OSD-then-RGW, but
# with install/restart interleaved per host — e.g. a MON host's daemons
# already restarted before an OSD host had even finished installing). The
# operator wants explicit control instead: install the new package on
# EVERY host first, then restart strictly MON-then-MGR-then-OSD across the
# WHOLE cluster (not host-by-host), then any leftover MDS/RGW units
# `_discover_ceph_units` finds. Still targets the SAME single Action row —
# per this story's frozen intent, phasing execution must not split into
# multiple Action rows.
_UPGRADE_PHASE_INSTALL = "install"
_UPGRADE_PHASE_MON = "mon"
_UPGRADE_PHASE_MGR = "mgr"
_UPGRADE_PHASE_OSD = "osd"
_UPGRADE_PHASE_MDS_RGW = "mds_rgw"

# (phase name, shared/cluster_nodes.py::configured_nodes() role string,
# worker/executor/commands.py daemon-type list) — MON -> MGR -> OSD order.
_ROLE_RESTART_PHASES = (
    (_UPGRADE_PHASE_MON, "MON", ("mon",)),
    (_UPGRADE_PHASE_MGR, "MGR", ("mgr",)),
    (_UPGRADE_PHASE_OSD, "OSD", ("osd",)),
)

_NOTHING_TO_RESTART_MESSAGE = "Không tìm thấy systemd unit nào cần khởi động lại trên node này"
# Code review fix (2026-08-04, Story 7.2): a host whose install step failed
# must not have its later MON/MGR/OSD/MDS-RGW unit restarted — that would
# restart a daemon against a possibly broken/partial package install.
_INSTALL_FAILED_SKIP_MESSAGE = "Bỏ qua vì cài đặt gói thất bại trên node này"
_UPGRADE_ABORTED_SKIP_MESSAGE = "Bỏ qua để bảo vệ cụm sau khi một bước cập nhật thất bại"


def _execute_for_cluster(host: str, command: str, cluster: Cluster | None = None) -> str:
    if cluster is None:
        return execute_command(host, command)
    ssh_user, ssh_key_path, _exec_mode, _container = resolve_ssh_creds(cluster)
    return execute_command(host, command, ssh_user, ssh_key_path)


def _discover_units_for_cluster(host: str, cluster: Cluster | None = None) -> dict[str, list[str]]:
    if cluster is None:
        return commands._discover_ceph_units(host)
    ssh_user, ssh_key_path, _exec_mode, _container = resolve_ssh_creds(cluster)
    return commands._discover_ceph_units(host, ssh_user, ssh_key_path)


def _execute_package_upgrade_action(
    action_pk: str,
    action_id_str: str,
    nodes: list[str],
    action_params: dict | None,
    incident_id: str,
    cluster: Cluster | None = None,
) -> None:
    """The package-based Cluster Upgrade's 5-phase executor — install-only
    on every configured host, then restart MON-role hosts' MON units, then
    MGR-role hosts' MGR units, then OSD-role hosts' OSD units, then any
    remaining host with a leftover discovered MDS/RGW unit (matches
    `_discover_ceph_units`'s daemon-type coverage). A host with more than
    one role (e.g. MON+OSD) installs exactly once (phase 0) and restarts
    once per role-phase it belongs to — its MON unit in the MON phase, its
    OSD unit in the OSD phase, never both together and never twice.

    5-phase sequence — install and restart steps alike. A
    mid-sequence trip stops immediately: every not-yet-run step (any
    phase) is recorded `skipped`; the Action reverts to PENDING_APPROVAL
    only if NOTHING executed anywhere yet, else ends FAILED with the
    partial progress kept — identical semantics to the pre-7.2 per-host
    loop (worker/llm/router_client.py's git history), just evaluated at
    per-phase-step granularity now.

    noout/noscrub/nodeep-scrub/nosnaptrim (_set_/_unset_upgrade_osd_flags)
    bracket the ENTIRE 5-phase sequence exactly as they bracketed the old
    single-phase per-host loop — set once before phase 0, unset once after
    the last phase actually run (or after an early stop), never per-phase.
    """
    action_params = action_params or {}

    # MON/MGR/OSD role membership is known purely from configured Settings
    # (shared/cluster_nodes.py::configured_nodes(), fresh read — same
    # "re-derive from Settings rather than trust anything cached" posture
    # _set_upgrade_osd_flags' own mon_nodes lookup already has) — no SSH
    # needed to PLAN these 3 phases, unlike the MDS/RGW leftover phase
    # below, whose membership can only be learned by actually discovering
    # each host's systemd units.
    roles_by_host: dict[str, set[str]] = {
        n["host"]: set(n["roles"]) for n in configured_nodes(cluster)
    }
    role_hosts = {
        role: [h for h in nodes if role in roles_by_host.get(h, ())]
        for _phase_name, role, _daemon_types in _ROLE_RESTART_PHASES
    }

    progress: list[dict] = [
        {"host": host, "status": "pending", "phase": _UPGRADE_PHASE_INSTALL} for host in nodes
    ]
    for phase_name, role, _daemon_types in _ROLE_RESTART_PHASES:
        progress.extend(
            {"host": host, "status": "pending", "phase": phase_name} for host in role_hosts[role]
        )
    _write_action_progress(action_pk, progress)

    state = {
        "executed_any": False,
        "all_succeeded": True,
        "last_command": None,
        # Code review fix (2026-08-04): hosts whose install step failed —
        # every later restart phase (MON/MGR/OSD, and the MDS/RGW dynamic
        # phase) must skip these rather than restart a daemon against a
        # possibly broken/partial install.
        "failed_install_hosts": set(),
        "aborted": False,
        "failures": [],
    }

    # Same bracket as the pre-7.2 code — set BEFORE phase 0, unset once
    # after the whole sequence (or an early stop), never per-phase (see
    # this function's own docstring / epic-7-context.md's Technical
    # Decisions on why the bracket's scope is the full upgrade window).
    mon_raw = settings.ceph_mon_nodes if cluster is None else cluster.ceph_mon_nodes
    mon_nodes_cfg = [h.strip() for h in mon_raw.split(",") if h.strip()]
    upgrade_flags_mon_host: str | None = None
    if mon_nodes_cfg:
        upgrade_flags_mon_host = mon_nodes_cfg[0]
        _set_upgrade_osd_flags(action_pk, upgrade_flags_mon_host, progress, cluster)
    else:
        logger.warning(
            "_execute_package_upgrade_action: no MON node configured — skipping "
            "noout/noscrub/nodeep-scrub/nosnaptrim for action %s",
            action_pk,
        )

    # Code review fix (2026-08-04): _run_install_phase/_run_restart_phase
    # only catch ExecutorError — any OTHER exception (an unwrapped network/
    # OS error from _discover_ceph_units or execute_command) must still
    # guarantee _unset_upgrade_osd_flags runs before it propagates, or
    # noout/noscrub/nodeep-scrub/nosnaptrim are left set on the live
    # cluster indefinitely with no automatic recovery. A `finally` here is
    # the deliberate fix — NOT widening the except clauses inside the phase
    # functions, which would silently swallow real bugs instead.
    try:
        install_entries = [p for p in progress if p.get("phase") == _UPGRADE_PHASE_INSTALL]
        _run_install_phase(
            action_pk, action_id_str, nodes, install_entries, action_params, progress,
            incident_id, state, cluster,
        )
        if not state["aborted"]:
            for phase_name, role, daemon_types in _ROLE_RESTART_PHASES:
                phase_entries = [p for p in progress if p.get("phase") == phase_name]
                _run_restart_phase(
                    action_pk, action_id_str, phase_name, role_hosts[role], phase_entries,
                    daemon_types, action_params, progress, incident_id, state, cluster,
                )
                if state["aborted"]:
                    break

        if not state["aborted"]:
            _run_restart_phase(
                action_pk, action_id_str, _UPGRADE_PHASE_MDS_RGW, nodes, None,
                ("mds", "rgw"), action_params, progress, incident_id, state, cluster,
            )
    finally:
        if upgrade_flags_mon_host is not None:
            _unset_upgrade_osd_flags(action_pk, upgrade_flags_mon_host, progress, cluster)

    # also block this finalize step — without `not state["stopped_mid_
    # sequence"]`, `ceph osd require-osd-release <codename>` could still
    if state["aborted"]:
        now = datetime.utcnow().isoformat()
        for item in progress:
            if item.get("status") == "pending":
                item.update(status="skipped", error=_UPGRADE_ABORTED_SKIP_MESSAGE,
                            started_at=now, finished_at=now)
        progress.append({
            "host": upgrade_flags_mon_host or "cluster",
            "phase": "rollback",
            "status": "done",
            "message": "Đã dừng rollout, không restart thêm daemon và đã gỡ các cờ bảo trì Ceph.",
            "started_at": now,
            "finished_at": now,
        })
        _write_action_progress(action_pk, progress)
    elif state["executed_any"]:
        _finalize_package_upgrade_osd_release(action_pk, action_params, progress, cluster)

    _record_approved_execution_result(
        action_pk,
        command=state["last_command"],
        succeeded=state["all_succeeded"] and state["executed_any"],
    )
    if state["aborted"]:
        _notify_package_upgrade_failure(incident_id, state["failures"], cluster)


def _notify_package_upgrade_failure(
    incident_id: str, failures: list[str], cluster: Cluster | None
) -> None:
    with db.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        diagnosis = incident.diagnosis_text if incident else None
        ceph_code = incident.ceph_code if incident else "CLUSTER_UPGRADE"
    has_cluster_channel = bool(cluster and cluster.telegram_bot_token and cluster.telegram_chat_id)
    send_update_failure_alert(
        ceph_code,
        diagnosis,
        "; ".join(failures) or "Không xác định được chi tiết lỗi",
        "Đã dừng các bước còn lại, giữ nguyên daemon đang chạy và gỡ các cờ bảo trì Ceph.",
        cluster_name=cluster.name if cluster else None,
        bot_token=cluster.telegram_bot_token if has_cluster_channel else None,
        chat_id=cluster.telegram_chat_id if has_cluster_channel else None,
        enabled=cluster.telegram_enabled if has_cluster_channel else None,
    )


def _run_install_phase(
    action_pk: str,
    action_id_str: str,
    hosts: list[str],
    phase_entries: list[dict],
    action_params: dict,
    progress: list[dict],
    incident_id: str,
    state: dict,
    cluster: Cluster | None = None,
) -> None:
    """Phase 0 — install-only on every host, positional addressing into
    `phase_entries` (one pre-populated "pending" dict per host, same
    failure semantics exactly (just against the install-only command
    variant instead of "install && restart everything")."""
    total = len(hosts)
    for index, host in enumerate(hosts):
        entry = phase_entries[index]
        try:
            command = commands.get_command(
                action_id_str, host, dict(action_params, _phase="install_only")
            )
        except ExecutorError as exc:
            logger.warning(
                "_execute_package_upgrade_action: no install Command for action_id=%s on "
                "host=%s (action %s, incident %s) — marking this step failed",
                action_id_str,
                host,
                action_pk,
                incident_id,
            )
            state["all_succeeded"] = False
            state["failed_install_hosts"].add(host)
            state["aborted"] = True
            state["failures"].append(f"Node {host}, bước install: {exc}")
            entry["status"] = "failed"
            entry["error"] = str(exc)
            _write_action_progress(action_pk, progress)
            break
        state["last_command"] = command

        logger.info(
            "_execute_package_upgrade_action: bắt đầu cài đặt trên host %s (%d/%d) (action %s)",
            host,
            index + 1,
            total,
            action_pk,
        )
        entry["status"] = "running"
        entry["command"] = command
        entry["started_at"] = datetime.utcnow().isoformat()
        _write_action_progress(action_pk, progress)

        try:
            _execute_for_cluster(host, command, cluster)
            state["executed_any"] = True
        except ExecutorError as exc:
            logger.exception(
                "_execute_package_upgrade_action: install failed on host %s (action %s)",
                host,
                action_pk,
            )
            state["all_succeeded"] = False
            state["executed_any"] = True
            state["failed_install_hosts"].add(host)
            state["aborted"] = True
            state["failures"].append(f"Node {host}, bước install: {exc}")
            entry["status"] = "failed"
            entry["error"] = str(exc)
            entry["finished_at"] = datetime.utcnow().isoformat()
            _write_action_progress(action_pk, progress)
            break

        entry["status"] = "done"
        entry["finished_at"] = datetime.utcnow().isoformat()
        _write_action_progress(action_pk, progress)
        logger.info(
            "_execute_package_upgrade_action: hoàn tất cài đặt trên host %s (%d/%d) (action %s)",
            host,
            index + 1,
            total,
            action_pk,
        )


def _run_restart_phase(
    action_pk: str,
    action_id_str: str,
    phase_name: str,
    candidate_hosts: list[str],
    phase_entries: list[dict] | None,
    daemon_types: tuple[str, ...],
    action_params: dict,
    progress: list[dict],
    incident_id: str,
    state: dict,
    cluster: Cluster | None = None,
) -> None:
    """Restarts `daemon_types`' systemd units on each host in
    `candidate_hosts` that ACTUALLY has one discovered — a host listed for
    this phase (by config role, for MON/MGR/OSD) with no matching unit is
    silently left alone (no failure recorded), same "nothing found ->
    nothing to do" contract
    worker/executor/commands.py::_restart_units_by_type_snippet already
    has for the un-phased command.

    `phase_entries`, when given, are PRE-POPULATED "pending" placeholders
    (one per `candidate_hosts`, same order) mutated in place — used for
    the MON/MGR/OSD phases, whose host list is knowable from
    configured_nodes() alone (no SSH needed to plan it). `None` means the
    MDS/RGW leftover phase instead, whose membership can only be learned
    by actually discovering each host — a step is appended dynamically
    only for a host that turns out to have something to restart, so a
    host with no leftover MDS/RGW unit never gets a progress entry at all
    for this phase (matches the pre-7.2 command's own silent no-op when
    nothing is discovered to restart).
    """
    for index, host in enumerate(candidate_hosts):
        if state["aborted"]:
            break
        # Code review fix (2026-08-04): a host whose install step already
        # failed (Fix 1) must not have its unit(s) restarted here — that
        # would restart a daemon against a possibly broken/partial
        if host in state["failed_install_hosts"]:
            now = datetime.utcnow().isoformat()
            if phase_entries is not None:
                entry = phase_entries[index]
                entry["status"] = "skipped"
                entry["error"] = _INSTALL_FAILED_SKIP_MESSAGE
            else:
                progress.append(
                    {
                        "host": host,
                        "phase": phase_name,
                        "status": "skipped",
                        "error": _INSTALL_FAILED_SKIP_MESSAGE,
                        "started_at": now,
                        "finished_at": now,
                    }
                )
            _write_action_progress(action_pk, progress)
            continue

        try:
            discovered = _discover_units_for_cluster(host, cluster)
        except ExecutorError as exc:
            state["all_succeeded"] = False
            state["aborted"] = True
            state["failures"].append(f"Node {host}, bước {phase_name}/discover: {exc}")
            now = datetime.utcnow().isoformat()
            if phase_entries is not None:
                entry = phase_entries[index]
                entry["status"] = "failed"
                entry["error"] = str(exc)
                entry["started_at"] = now
                entry["finished_at"] = now
            else:
                progress.append(
                    {
                        "host": host,
                        "phase": phase_name,
                        "status": "failed",
                        "error": str(exc),
                        "started_at": now,
                        "finished_at": now,
                    }
                )
            _write_action_progress(action_pk, progress)
            break

        if not any(discovered.get(daemon_type) for daemon_type in daemon_types):
            if phase_entries is not None:
                entry = phase_entries[index]
                entry["status"] = "skipped"
                entry["error"] = _NOTHING_TO_RESTART_MESSAGE
                _write_action_progress(action_pk, progress)
            continue  # dynamic (MDS/RGW) phase: nothing to restart here -> no entry at all

        try:
            command_params = dict(
                action_params, _phase="restart_only", _phase_daemon_types=list(daemon_types)
            )
            if cluster is not None:
                ssh_user, ssh_key_path, _exec_mode, _container = resolve_ssh_creds(cluster)
                command_params.update(_ssh_user=ssh_user, _ssh_key_path=ssh_key_path)
            command = commands.get_command(
                action_id_str,
                host,
                command_params,
            )
        except ExecutorError as exc:
            state["all_succeeded"] = False
            state["aborted"] = True
            state["failures"].append(f"Node {host}, bước {phase_name}/prepare: {exc}")
            now = datetime.utcnow().isoformat()
            if phase_entries is not None:
                entry = phase_entries[index]
                entry["status"] = "failed"
                entry["error"] = str(exc)
                entry["started_at"] = now
                entry["finished_at"] = now
            else:
                progress.append(
                    {
                        "host": host,
                        "phase": phase_name,
                        "status": "failed",
                        "error": str(exc),
                        "started_at": now,
                        "finished_at": now,
                    }
                )
            _write_action_progress(action_pk, progress)
            break
        state["last_command"] = command

        if phase_entries is not None:
            entry = phase_entries[index]
            entry["status"] = "running"
            entry["command"] = command
            entry["started_at"] = datetime.utcnow().isoformat()
        else:
            entry = {
                "host": host,
                "phase": phase_name,
                "status": "running",
                "command": command,
                "started_at": datetime.utcnow().isoformat(),
            }
            progress.append(entry)
        _write_action_progress(action_pk, progress)

        try:
            _execute_for_cluster(host, command, cluster)
            state["executed_any"] = True
        except ExecutorError as exc:
            state["all_succeeded"] = False
            state["executed_any"] = True
            state["aborted"] = True
            state["failures"].append(f"Node {host}, bước {phase_name}/restart: {exc}")
            entry["status"] = "failed"
            entry["error"] = str(exc)
            entry["finished_at"] = datetime.utcnow().isoformat()
            _write_action_progress(action_pk, progress)
            break

        entry["status"] = "done"
        entry["finished_at"] = datetime.utcnow().isoformat()
        _write_action_progress(action_pk, progress)


def _is_disruptive_cluster_operation_in_flight() -> bool:
    """True while a cluster-upgrade or patch-install Action is proposed but
    not yet resolved (PENDING_APPROVAL/APPROVED) — same in-flight window
    dashboard/routes/actions.py::approve_action already gates other RISKY
    approvals on, checked here too so a fresh RISKY proposal doesn't even
    get created in the first place. Fails closed (treated as "in flight",
    i.e. suppress) on a DB error — "can't tell" must not mean "go ahead and
    spam a new proposal".
    """
    try:
        with db.SessionLocal() as session:
            return (
                session.query(Action)
                .filter(Action.action_id.in_(_DISRUPTIVE_CLUSTER_OPERATION_ACTION_IDS))
                .filter(
                    Action.status.in_(
                        (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value)
                    )
                )
                .first()
                is not None
            )
    except Exception:
        logger.exception(
            "_is_disruptive_cluster_operation_in_flight: failed to query DB — "
            "failing closed (treated as in-flight, suppressing new RISKY proposals)"
        )
        return True


def _auto_reject_risky_during_cluster_operation(
    incident_id: str, action_pk: str, action_id: str
) -> None:
    logger.warning(
        "diagnose_incident: auto-rejecting RISKY action %s for incident %s — a cluster "
        "upgrade/patch install is currently in flight",
        action_id,
        incident_id,
    )
    with db.SessionLocal() as session:
        action = session.get(Action, action_pk)
        incident = session.get(Incident, incident_id)
        if action is None:
            logger.warning(
                "_auto_reject_risky_during_cluster_operation: no Action row for pk=%s "
                "(incident %s)",
                action_pk,
                incident_id,
            )
        else:
            action.status = ActionStatus.REJECTED.value
        if incident is None:
            logger.warning(
                "_auto_reject_risky_during_cluster_operation: no Incident row for id=%s — "
                "skipping audit.record() too (no valid Incident to attach it to)",
                incident_id,
            )
        else:
            incident.status = IncidentStatus.REJECTED.value
            audit.record(
                session,
                incident_id=incident_id,
                action_id=action_pk if action is not None else None,
                event_type=audit.EVENT_RISKY_ACTION_AUTO_REJECTED_CLUSTER_OPERATION_IN_PROGRESS,
                actor=audit.ACTOR_SYSTEM,
            )
        session.commit()


def _route_safe_to_approval(incident_id: str, action_pk: str, action_id: str) -> None:
    """Park a SAFE action when the global Autopilot kill switch is off."""
    with db.SessionLocal() as session:
        action = session.get(Action, action_pk)
        incident = session.get(Incident, incident_id)
        try:
            params = json.loads(action.action_params) if action and action.action_params else None
        except (TypeError, ValueError):
            params = None
        try:
            command = commands.get_command(action_id, params=params)
        except ExecutorError:
            command = None
        if action is not None:
            action.status = ActionStatus.PENDING_APPROVAL.value
            action.proposed_command = command
        if incident is not None:
            incident.status = IncidentStatus.PENDING_APPROVAL.value
            audit.record(
                session, incident_id=incident_id,
                action_id=action_pk if action is not None else None,
                event_type=audit.EVENT_AUTOPILOT_KILL_SWITCH_BLOCKED,
                actor=audit.ACTOR_SYSTEM,
            )
        session.commit()


def _route_risky_to_approval(incident_id: str, action_pk: str, action_id: str) -> None:
    """Story 4.2: resolve the proposed command (best-effort — some
    action_ids have none, see worker/executor/commands.py's Epic-4 note)
    purely for display, then park the Action/Incident at PENDING_APPROVAL."""
    with db.SessionLocal() as session:
        action = session.get(Action, action_pk)
        incident = session.get(Incident, incident_id)
        try:
            params = json.loads(action.action_params) if action and action.action_params else None
        except (TypeError, ValueError):
            params = None
        try:
            command = commands.get_command(action_id, params=params)
        except ExecutorError:
            command = None
        if action is None:
            logger.warning(
                "_route_risky_to_approval: no Action row for pk=%s (incident %s)",
                action_pk,
                incident_id,
            )
        else:
            action.status = ActionStatus.PENDING_APPROVAL.value
            action.proposed_command = command
        if incident is None:
            # Same reasoning as _route_to_manual_approval/_record_execution_result:
            # AuditEntry.incident_id is a required FK — nothing valid to attach to.
            logger.warning(
                "_route_risky_to_approval: no Incident row for id=%s — skipping "
                "audit.record() too (no valid Incident to attach it to)",
                incident_id,
            )
        else:
            incident.status = IncidentStatus.PENDING_APPROVAL.value
            audit.record(
                session,
                incident_id=incident_id,
                action_id=action_pk if action is not None else None,
                event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
                actor=audit.ACTOR_SYSTEM,
            )
        session.commit()


# --- Story 4.3: execute an operator-approved RISKY action -----------------
#
# Separate from the RabbitMQ consumer loop entirely (worker/main.py::run()):
# an Approve click on the Dashboard isn't a queue message, so nothing
# redelivers it — the Worker has to notice it itself by polling the DB.
# `worker/main.py`'s `__main__` block runs poll_approved_actions() alongside
# run() via asyncio.gather() in the same process/event loop, because only
# the Worker holds SSH executor credentials (AD-3).


def _process_approved_actions_once() -> None:
    _reconcile_stuck_rbd_actions_once()
    with db.SessionLocal() as session:
        approved_pks = [
            row.id
            for row in session.query(Action).filter_by(status=ActionStatus.APPROVED.value).all()
        ]
    for action_pk in approved_pks:
        try:
            _execute_approved_action(action_pk)
        except Exception:
            logger.exception(
                "_process_approved_actions_once: unexpected error executing action %s "
                "— marking FAILED",
                action_pk,
            )
            try:
                _record_approved_execution_result(action_pk, command=None, succeeded=False)
            except Exception:
                logger.exception(
                    "_process_approved_actions_once: failed to record the FAILED outcome "
                    "for action %s after an unexpected execution error",
                    action_pk,
                )


def _reconcile_stuck_rbd_actions_once(
    *, stale_after_seconds: int = 600, now: datetime | None = None,
) -> list[str]:
    """Resolve stale RBD executions from live state without rerunning mutation."""
    cutoff = (now or datetime.utcnow()) - timedelta(seconds=max(1, stale_after_seconds))
    candidates: list[dict] = []
    with db.SessionLocal() as session:
        rows = (
            session.query(Action, Incident)
            .join(Incident, Incident.id == Action.incident_id)
            .filter(
                Action.action_id.in_(rbd_reconciliation.RBD_RECONCILED_ACTION_IDS),
                Action.status == ActionStatus.APPROVED.value,
                Action.updated_at < cutoff,
                Incident.status == IncidentStatus.EXECUTING.value,
            )
            .all()
        )
        for action, incident in rows:
            cluster = session.get(Cluster, incident.cluster_id) if incident.cluster_id else None
            try:
                params = json.loads(action.action_params or "{}")
                nodes = json.loads(action.target_nodes or "[]")
            except (TypeError, ValueError):
                params, nodes = {}, []
            candidates.append({
                "action_pk": action.id,
                "action_id": action.action_id,
                "params": params,
                "host": nodes[0] if isinstance(nodes, list) and nodes else None,
                "ssh_user": cluster.ssh_user if cluster is not None else None,
                "ssh_key_path": cluster.ssh_key_path if cluster is not None else None,
            })

    resolved: list[str] = []
    for item in candidates:
        if not item["host"]:
            continue
        try:
            command = rbd_reconciliation.reconciliation_command(item["action_id"], item["params"])
            output = execute_command(
                item["host"], command, user=item["ssh_user"], key_path=item["ssh_key_path"]
            )
        except ExecutorError:
            logger.warning(
                "RBD stuck-action reconciliation could not query action %s; retrying later",
                item["action_pk"], exc_info=True,
            )
            continue
        try:
            rbd_reconciliation.reconcile(item["action_id"], item["params"], output)
        except ExecutorError as exc:
            _write_action_progress(item["action_pk"], [{
                "host": item["host"], "status": "failed", "phase": "reconciliation",
                "command": command, "error": str(exc), "finished_at": datetime.utcnow().isoformat(),
            }])
            _record_approved_execution_result(item["action_pk"], command=command, succeeded=False)
        else:
            _write_action_progress(item["action_pk"], [{
                "host": item["host"], "status": "done", "phase": "reconciliation",
                "command": command, "finished_at": datetime.utcnow().isoformat(),
            }])
            _record_approved_execution_result(item["action_pk"], command=command, succeeded=True)
        resolved.append(item["action_pk"])
    return resolved


def _execute_approved_action(action_pk: str) -> None:
    """Run the command for an operator-approved action.

    A separate function from _maybe_execute_safe_action rather than a
    shared helper: the two run from different triggers (RabbitMQ message
    envelope vs. a DB-polled Action row long after that envelope is gone)
    and end in different terminal states (AUTO_FIXED/AUTO_EXECUTED vs.
    RESOLVED/EXECUTED) — reusing Story 3.2's already-shipped, already
    code-reviewed function risked regressing it for a resemblance that
    isn't actually load-bearing.
    """
    with db.SessionLocal() as session:
        action = session.get(Action, action_pk)
        if action is None or action.status != ActionStatus.APPROVED.value:
            # Already handled (executed/failed/reverted by a previous poll
            # tick, or the row is gone) — nothing to do this tick.
            return
        incident_id = action.incident_id
        action_id_str = action.action_id
        target_nodes_raw = action.target_nodes
        action_params_raw = action.action_params
        # Atomic execution claim: multiple Worker processes may poll the same
        # APPROVED row at once. The first one transitions its Incident to
        # EXECUTING; every contender then observes rowcount=0 and must leave
        # before performing any SSH side effect.
        claimed = (
            session.query(Incident)
            .filter(Incident.id == incident_id)
            .filter(Incident.status != IncidentStatus.EXECUTING.value)
            .update({Incident.status: IncidentStatus.EXECUTING.value}, synchronize_session=False)
        )
        session.commit()
        if claimed != 1:
            logger.info(
                "_execute_approved_action: action %s was already claimed by another Worker",
                action_pk,
            )
            return
        incident = session.get(Incident, incident_id)
        # 2026-08-10 (multi-tenant remediation Phase 1): resolve THIS
        # Incident's own cluster's SSH creds here, inside the same session —
        # by the time this function runs (a DB poll, long after the RabbitMQ
        # envelope that carried them at diagnosis time is gone), the
        # envelope itself is unavailable, so Incident.cluster_id -> Cluster
        # is the only place left to look them up. None means the default
        # cluster (execute_command()'s own settings.* fallback applies).
        cluster = None
        if incident is not None and incident.cluster_id is not None:
            cluster = session.get(Cluster, incident.cluster_id)
            ssh_user = cluster.ssh_user if cluster is not None else None
            ssh_key_path = cluster.ssh_key_path if cluster is not None else None
        else:
            ssh_user = None
            ssh_key_path = None
        # 2026-08-11 (multi-tenant remediation Phase 3): captured here too
        # (not re-derived below) for worker/backup/engine.py's dispatch —
        # same "None means the default cluster" semantics as ssh_user/
        # ssh_key_path just above.
        cluster_id = incident.cluster_id if incident is not None else None

    try:
        nodes = json.loads(target_nodes_raw) if target_nodes_raw else None
    except (TypeError, ValueError):
        nodes = None
    if not isinstance(nodes, list) or not nodes or not all(
        isinstance(host, str) and host for host in nodes
    ):
        logger.warning(
            "_execute_approved_action: missing/malformed target_nodes for action %s "
            "(incident %s) — marking FAILED instead of guessing",
            action_pk,
            incident_id,
        )
        _record_approved_execution_result(action_pk, command=None, succeeded=False)
        return
    try:
        action_params = json.loads(action_params_raw) if action_params_raw else None
    except (TypeError, ValueError):
        action_params = None

    # 2026-07-25 (Story 8.1): Dựng cụm Ceph tự động's 3 action_ids delegate
    # entirely to worker/executor/cluster_deploy.py's own multi-phase
    # orchestrator instead of the generic per-host loop below — that loop
    # fires ONE command family identically at every host with no
    # cross-host ordering and no wait step, which cannot express "MON
    # before MGR/OSD, wait for quorum first". cluster_deploy.run() reuses
    # its own step-shaped progress lists.
    if action_id_str in cluster_deploy.CLUSTER_DEPLOY_ACTION_IDS:
        if not isinstance(action_params, dict):
            logger.warning(
                "_execute_approved_action: missing/malformed action_params for cluster-deploy "
                "action %s (incident %s) — marking FAILED instead of guessing",
                action_pk,
                incident_id,
            )
            _record_approved_execution_result(action_pk, command=None, succeeded=False)
            return
        succeeded = cluster_deploy.run(
            action_pk,
            action_id_str,
            action_params,
            incident_id,
            _write_action_progress,
        )
        _record_approved_execution_result(action_pk, command=None, succeeded=succeeded)
        return

    # 2026-07-29: Volumes page "Đo hiệu năng tối đa" (load sweep) — same
    # reasoning as the cluster-deploy branch just above: this is its own
    # multi-step orchestrator (sweeps fio iodepth 1->256) over target_nodes.
    if action_id_str in volume_perf.VOLUME_PERF_ACTION_IDS:
        if not isinstance(action_params, dict):
            logger.warning(
                "_execute_approved_action: missing/malformed action_params for volume-perf "
                "action %s (incident %s) — marking FAILED instead of guessing",
                action_pk,
                incident_id,
            )
            _record_approved_execution_result(action_pk, command=None, succeeded=False)
            return
        executor = vm_perf if action_id_str == vm_perf.VM_PERF_ACTION_ID else volume_perf
        if action_id_str == vm_perf.VM_PERF_ACTION_ID:
            succeeded = executor.run(
                action_pk, action_params, incident_id, _write_action_progress, cluster
            )
        else:
            succeeded = executor.run(
                action_pk, action_params, incident_id, _write_action_progress
            )
        _record_approved_execution_result(action_pk, command=None, succeeded=succeeded)
        return

    # 2026-07-30 (Epic 9, Story 9.1): Ceph Backup & Disaster Recovery —
    # same reasoning as the two branches just above: worker/backup/
    # engine.py is its own bespoke orchestrator (rbd snapshot/export/
    # retention), not a single command fanned out to target_nodes. Unlike
    # the other two, this family has more than one action_id needing
    # different logic, so backup_engine.run() also takes action_id_str
    # (same as cluster_deploy.run() above).
    if action_id_str in backup_engine.BACKUP_ACTION_IDS:
        if not isinstance(action_params, dict):
            logger.warning(
                "_execute_approved_action: missing/malformed action_params for backup "
                "action %s (incident %s) — marking FAILED instead of guessing",
                action_pk,
                incident_id,
            )
            _record_approved_execution_result(action_pk, command=None, succeeded=False)
            return
        succeeded = backup_engine.run(
            action_pk,
            action_id_str,
            action_params,
            incident_id,
            cluster_id,
            _write_action_progress,
        )
        _record_approved_execution_result(action_pk, command=None, succeeded=succeeded)
        return

    # 2026-08-04 (Story 7.2): the two package-based Cluster Upgrade
    # action_ids get a dedicated 5-phase executor (install-only on every
    # host, then restart MON-role -> MGR-role -> OSD-role hosts' units one
    # daemon type at a time, then any host with leftover discovered
    # MDS/RGW units) instead of the generic "one command per host" loop
    # below — see _execute_package_upgrade_action's own docstring for why
    # the generic loop can't express "install everywhere first, restart in
    # role order across the WHOLE cluster" (it fires one command per host,
    # done). `upgrade_ceph_cluster` (cephadm) is NOT in
    # _PACKAGE_UPGRADE_ACTION_IDS and falls through to the generic loop
    # completely unchanged, per this story's explicit boundary.
    if action_id_str in _PACKAGE_UPGRADE_ACTION_IDS:
        _execute_package_upgrade_action(
            action_pk, action_id_str, nodes, action_params, incident_id, cluster
        )
        return

    executed_any = False
    all_succeeded = True
    # Resolved per-host below (not once up front) — restart_osd_daemon's
    # cephadm-mode command depends on which host's OSD daemon name(s) get
    # discovered (see worker/executor/commands.py::get_command). Every
    # other action_id's command is identical regardless of host.
    last_command: str | None = None
    update_failures: list[str] = []
    update_rollback_summary: str | None = None
    total_nodes = len(nodes)
    progress = [{"host": host, "status": "pending"} for host in nodes]
    _write_action_progress(action_pk, progress)

    for node_index, host in enumerate(nodes, start=1):

        try:
            command = commands.get_command(action_id_str, host, action_params)
        except ExecutorError as exc:
            logger.warning(
                "_execute_approved_action: no Command for action_id=%s on host=%s "
                "(action %s, incident %s) — marking this node failed",
                action_id_str,
                host,
                action_pk,
                incident_id,
            )
            all_succeeded = False
            progress[node_index - 1]["status"] = "failed"
            progress[node_index - 1]["error"] = str(exc)
            _write_action_progress(action_pk, progress)
            if action_id_str == "upgrade_ceph_cluster":
                update_failures.append(f"Node {host}, bước chuẩn bị: {exc}")
                break
            continue
        last_command = command

        # 2026-07-24: added so an operator tailing worker.log (or the
        # Upgrade page, via `progress` above) can tell WHICH node the
        # upgrade is on right now and which ones already finished —
        # execute_command() blocks for the whole duration of a real package
        # install (minutes), so without this the log goes silent mid-run
        # with no way to tell progress from a hang.
        logger.info(
            "_execute_approved_action: bắt đầu action_id=%s trên host %s (%d/%d) "
            "(action %s)",
            action_id_str,
            host,
            node_index,
            total_nodes,
            action_pk,
        )
        # 2026-07-27: started_at/command/finished_at/error added so
        # dashboard/routes/upgrade.py can render a per-step markdown log
        # (Cluster Upgrade's "ghi lại từng bước, từng lỗi" requirement) —
        # execution_progress is the only record of what actually ran on
        # each host once this Action resolves; before this, a failed node's
        # real error text only ever reached worker.log, never the Dashboard.
        progress[node_index - 1]["status"] = "running"
        progress[node_index - 1]["command"] = command
        progress[node_index - 1]["started_at"] = datetime.utcnow().isoformat()
        _write_action_progress(action_pk, progress)

        try:
            command_output = execute_command(host, command, user=ssh_user, key_path=ssh_key_path)
            executed_any = True
            rbd_reconciliation.reconcile(action_id_str, action_params or {}, command_output)
            cinder_reconciliation.reconcile(action_id_str, action_params or {}, command_output)
        except ExecutorError as exc:
            logger.exception(
                "_execute_approved_action: execution of action_id=%s failed on node %s "
                "(action %s)",
                action_id_str,
                host,
                action_pk,
            )
            all_succeeded = False
            executed_any = True
            progress[node_index - 1]["status"] = "failed"
            progress[node_index - 1]["error"] = str(exc)
            progress[node_index - 1]["finished_at"] = datetime.utcnow().isoformat()
            _write_action_progress(action_pk, progress)
            if action_id_str == "upgrade_ceph_cluster":
                update_failures.append(f"Node {host}, bước cephadm upgrade: {exc}")
                rollback_command = (
                    "cephadm shell -- bash -c 'ceph orch upgrade stop; "
                    + "; ".join(f"ceph osd unset {flag}" for flag in _UPGRADE_OSD_FLAGS)
                    + "'"
                )
                rollback_step = {
                    "host": host,
                    "phase": "rollback",
                    "status": "running",
                    "command": rollback_command,
                    "started_at": datetime.utcnow().isoformat(),
                }
                progress.append(rollback_step)
                _write_action_progress(action_pk, progress)
                try:
                    execute_command(host, rollback_command, user=ssh_user, key_path=ssh_key_path)
                except ExecutorError as rollback_exc:
                    rollback_step["status"] = "failed"
                    rollback_step["error"] = str(rollback_exc)
                    update_rollback_summary = f"Rollback cephadm thất bại: {rollback_exc}"
                else:
                    rollback_step["status"] = "done"
                    update_rollback_summary = "Đã dừng cephadm upgrade và gỡ các cờ bảo trì Ceph."
                rollback_step["finished_at"] = datetime.utcnow().isoformat()
                now = datetime.utcnow().isoformat()
                for pending in progress:
                    if pending.get("status") == "pending":
                        pending.update(status="skipped", error=_UPGRADE_ABORTED_SKIP_MESSAGE,
                                       started_at=now, finished_at=now)
                _write_action_progress(action_pk, progress)
                break
            # Keep trying remaining nodes, same rationale as
            # _maybe_execute_safe_action: the log should show exactly which
            # nodes failed even though the overall Action is already FAILED.
            continue

        progress[node_index - 1]["status"] = "done"
        if action_id_str == "execute_node_command":
            progress[node_index - 1]["output"] = command_output[-50000:]
        progress[node_index - 1]["finished_at"] = datetime.utcnow().isoformat()
        _write_action_progress(action_pk, progress)

        logger.info(
            "_execute_approved_action: hoàn tất action_id=%s trên host %s (%d/%d) "
            "(action %s)",
            action_id_str,
            host,
            node_index,
            total_nodes,
            action_pk,
        )

    _record_approved_execution_result(
        action_pk, command=last_command, succeeded=all_succeeded and executed_any
    )
    if update_failures:
        _notify_update_failure(
            incident_id,
            update_failures,
            update_rollback_summary or "Đã dừng rollout trước các node còn lại.",
            cluster,
        )


def _notify_update_failure(
    incident_id: str, failures: list[str], rollback_summary: str, cluster: Cluster | None
) -> None:
    with db.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        diagnosis = incident.diagnosis_text if incident else None
        ceph_code = incident.ceph_code if incident else "CLUSTER_UPGRADE"
    has_cluster_channel = bool(cluster and cluster.telegram_bot_token and cluster.telegram_chat_id)
    send_update_failure_alert(
        ceph_code,
        diagnosis,
        "; ".join(failures),
        rollback_summary,
        cluster_name=cluster.name if cluster else None,
        bot_token=cluster.telegram_bot_token if has_cluster_channel else None,
        chat_id=cluster.telegram_chat_id if has_cluster_channel else None,
        enabled=cluster.telegram_enabled if has_cluster_channel else None,
    )


def _write_action_progress(action_pk: str, progress: list[dict]) -> None:
    """Persists per-host progress for the Upgrade page to display — a
    package-based upgrade has no orchestrator to poll for status (unlike
    cephadm's `ceph orch upgrade status`), and a single host's install can
    block execute_command() for minutes, so without this the page has
    nothing to show between "Đã duyệt" and the final result. Best-effort:
    swallows its own failure rather than letting a progress-write bug take
    down the actual upgrade.
    """
    try:
        with db.SessionLocal() as session:
            action = session.get(Action, action_pk)
            if action is None:
                return
            action.execution_progress = json.dumps(progress)
            session.commit()
    except Exception:
        logger.exception(
            "_write_action_progress: failed to persist progress for action %s — "
            "continuing execution regardless",
            action_pk,
        )


def _record_approved_execution_result(
    action_pk: str, command: str | None, succeeded: bool
) -> None:
    with db.SessionLocal() as session:
        action = session.get(Action, action_pk)
        if action is None:
            logger.warning(
                "_record_approved_execution_result: no Action row for pk=%s — SSH side "
                "effects (if any) are untracked",
                action_pk,
            )
            return
        incident_id = action.incident_id
        if command is not None:
            action.proposed_command = command
        action.status = ActionStatus.EXECUTED.value if succeeded else ActionStatus.FAILED.value
        if succeeded:
            action.executed_at = datetime.utcnow()

        incident = session.get(Incident, incident_id)
        if incident is None:
            logger.warning(
                "_record_approved_execution_result: no Incident row for id=%s — "
                "skipping audit.record() too (no valid Incident to attach it to)",
                incident_id,
            )
        else:
            if not succeeded:
                incident.status = IncidentStatus.FAILED.value
            elif is_monitor_owned(incident.ceph_code):
                # ceph_code do monitor tự đặt không bao giờ xuất hiện trong
                # `ceph health detail`, nên không có gì để đối chiếu — chính
                # module monitor sở hữu nó mới biết vấn đề còn hay hết. Giữ
                # nguyên hành vi cũ cho nhóm này.
                incident.status = IncidentStatus.RESOLVED.value
            else:
                # 2026-08-20: lệnh chạy xong exit 0 KHÔNG phải bằng chứng
                # lỗi đã hết — nó chỉ chứng minh lệnh chạy được. Chuyển sang
                # VERIFYING và để watcher/verify.py hỏi lại cụm sau
                # `settings.incident_verify_delay_seconds` rồi mới kết luận.
                incident.status = IncidentStatus.VERIFYING.value
                incident.verify_after = datetime.utcnow() + timedelta(
                    seconds=max(0, settings.incident_verify_delay_seconds)
                )
            audit.record(
                session,
                incident_id=incident_id,
                action_id=action_pk,
                event_type=(
                    audit.EVENT_RISKY_ACTION_EXECUTED
                    if succeeded
                    else audit.EVENT_RISKY_ACTION_FAILED
                ),
                actor=audit.ACTOR_SYSTEM,
            )
        if action.action_id == "execute_node_command":
            source_message = (
                session.query(ChatMessage)
                .filter(
                    ChatMessage.proposed_incident_id == incident_id,
                    ChatMessage.proposed_action_id == "execute_node_command",
                )
                .first()
            )
            existing_result = (
                session.query(ChatMessage)
                .filter(
                    ChatMessage.proposed_incident_id == incident_id,
                    ChatMessage.proposed_status == "RESULT",
                )
                .first()
            )
            if source_message is not None and existing_result is None:
                try:
                    progress = json.loads(action.execution_progress or "[]")
                except (TypeError, ValueError):
                    progress = []
                blocks = []
                for item in progress if isinstance(progress, list) else []:
                    host = item.get("host", "node")
                    if item.get("status") == "done":
                        output = str(item.get("output") or "(lệnh hoàn tất, không có output)")
                        blocks.append(f"Kết quả trên {host}:\n{output}")
                    elif item.get("status") == "failed":
                        error = item.get("error") or "Không rõ lỗi"
                        blocks.append(f"Lệnh thất bại trên {host}:\n{error}")
                session.add(
                    ChatMessage(
                        session_id=source_message.session_id,
                        role="assistant",
                        content=(
                            "\n\n".join(blocks)
                            or "Worker đã kết thúc nhưng không ghi nhận được kết quả lệnh."
                        ),
                        actor=source_message.actor,
                        proposed_status="RESULT",
                        proposed_incident_id=incident_id,
                    )
                )
        session.commit()


async def poll_approved_actions(
    poll_interval: float | None = None, max_iterations: int | None = None
) -> None:
    """Runs forever (`max_iterations=None`, real usage) or a bounded number
    of ticks (tests). Each tick's DB work is blocking (paramiko, SQLAlchemy)
    — run off the event loop via asyncio.to_thread so it doesn't stall
    worker/main.py::run()'s RabbitMQ consumer sharing this event loop.
    """
    interval = (
        poll_interval if poll_interval is not None else settings.worker_approval_poll_interval_seconds
    )
    iterations = 0
    while True:
        try:
            await asyncio.to_thread(_process_approved_actions_once)
        except Exception:
            logger.exception("poll_approved_actions: unexpected error during poll tick")
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return
        await asyncio.sleep(interval)
