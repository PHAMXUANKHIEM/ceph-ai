import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from config.settings import settings
from shared import audit, db
from shared.kill_switch import is_kill_switch_enabled
from shared.models import Action, ActionClassification, ActionStatus, Incident, IncidentStatus
from shared.router_client import build_router_client
from worker.executor import commands
from worker.executor.ssh_executor import ExecutorError, execute_command
from worker.policy import gate
from worker.redaction import default_redactor

logger = logging.getLogger(__name__)

MAX_TOKENS = 1024
ROUTER_TIMEOUT_SECONDS = 60.0
TOOL_NAME = "report_diagnosis"

_POLICY_PATH = Path(__file__).resolve().parent.parent / "policy" / "action_policy.yaml"


def _load_valid_action_ids() -> frozenset[str]:
    with open(_POLICY_PATH) as f:
        policy = yaml.safe_load(f)
    return frozenset(policy["action_ids"])


VALID_ACTION_IDS = _load_valid_action_ids()


def _warn_if_missing_api_key(api_key: str) -> None:
    if not api_key:
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


_warn_if_missing_api_key(settings.router_api_key)
_warn_if_missing_worker_ssh_key(settings.ssh_key_path)

SYSTEM_PROMPT = (
    "You are an expert Ceph storage cluster SRE. Given a detected Ceph health "
    "incident (error code, relevant daemon log excerpt, cluster snapshot), "
    "diagnose the likely root cause in plain language an operator without "
    "deep Ceph internals knowledge can understand, and recommend exactly one "
    "remediation action from the fixed set provided in the tool schema."
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
                            "understandable without reading raw Ceph docs."
                        ),
                    },
                    "action_id": {
                        "type": "string",
                        "enum": sorted(VALID_ACTION_IDS),
                        "description": "The single recommended remediation action.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why this action_id was chosen over the alternatives.",
                    },
                },
                "required": ["diagnosis_text", "action_id", "rationale"],
                "additionalProperties": False,
            },
        },
    }


def _build_user_content(payload: dict) -> str:
    nodes = payload.get("nodes") or []
    return (
        f"Ceph error code: {payload.get('ceph_code')}\n"
        f"Detected at: {payload.get('detected_at')}\n"
        f"Affected nodes: {', '.join(nodes)}\n"
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
    if not diagnosis_text or not rationale or action_id not in VALID_ACTION_IDS:
        raise RouterDiagnosisError(
            f"invalid router response for incident {incident_id}: "
            f"action_id={action_id!r}, diagnosis_text={diagnosis_text!r}, rationale={rationale!r}"
        )
    logger.info("diagnose_incident: incident %s rationale: %s", incident_id, rationale)

    with db.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        if incident is None:
            logger.warning(
                "diagnose_incident: no Incident row for id=%s — skipping DB write", incident_id
            )
            return
        incident.diagnosis_text = diagnosis_text

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
            classification = gate.classify_action(action_id)
            nodes = envelope.get("nodes")
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
            )
            session.add(action)
            session.commit()
            action_pk = action.id  # read while session is still open
            resolved_action_id = action_id

    if classification == ActionClassification.SAFE:
        try:
            _maybe_execute_safe_action(incident_id, action_pk, resolved_action_id, envelope)
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


def _check_kill_switch_safe(incident_id: str) -> bool:
    """AD-4: fresh read, fresh session, every time this is called. Fails
    CLOSED (returns True / blocked) on any error reading it — "can't tell"
    must mean "don't execute", not "go ahead"."""
    try:
        with db.SessionLocal() as session:
            return is_kill_switch_enabled(session)
    except Exception:
        logger.exception(
            "diagnose_incident: failed to read kill-switch state for incident %s — "
            "failing closed (treated as ON)",
            incident_id,
        )
        return True


def _maybe_execute_safe_action(
    incident_id: str, action_pk: str, action_id: str, envelope: dict
) -> None:
    """Runs the Command for a SAFE Action, checking the kill-switch fresh
    before EACH node (AD-4 — "trước MỌI lệnh remediation... không có ngoại
    lệ" — a multi-node Action must not let a mid-loop kill-switch flip go
    unnoticed for later nodes).

    Deliberately never raises — an execution failure ends in Action/Incident
    status=FAILED, not a retry (AC #3: retry/DLX is for transient Router
    failures, not for a command that's unlikely to succeed on a bare retry).
    """
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
        if _check_kill_switch_safe(incident_id):
            if executed_any:
                # Real execution already happened on an earlier node — this
                # is no longer a clean "nothing ran yet, awaiting approval"
                # state. PENDING_APPROVAL would misrepresent that. Surface
                # it as FAILED so a human looks at exactly what's now
                # inconsistent across nodes.
                logger.warning(
                    "diagnose_incident: kill-switch turned ON mid-execution for incident %s "
                    "(action_id=%s) after at least one node already ran — marking FAILED, "
                    "not PENDING_APPROVAL",
                    incident_id,
                    action_id,
                )
                all_succeeded = False
                break
            _route_to_manual_approval(incident_id, action_pk, action_id)
            return

        try:
            command = commands.get_command(action_id, host)
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
            execute_command(host, command)
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


def _route_to_manual_approval(incident_id: str, action_pk: str, action_id: str) -> None:
    logger.warning(
        "diagnose_incident: kill-switch is ON — routing SAFE action %s for incident %s "
        "to manual approval instead of auto-executing",
        action_id,
        incident_id,
    )
    with db.SessionLocal() as session:
        action = session.get(Action, action_pk)
        incident = session.get(Incident, incident_id)
        if action is None:
            logger.warning(
                "diagnose_incident: no Action row for pk=%s (incident %s) while routing "
                "to manual approval",
                action_pk,
                incident_id,
            )
        else:
            action.status = ActionStatus.PENDING_APPROVAL.value
        if incident is None:
            # AuditEntry.incident_id is a required FK — there is nothing
            # valid to attach an audit row to, so skip it too (an
            # unconditional audit.record() here would insert a row
            # referencing a nonexistent Incident, and under production's
            # PRAGMA foreign_keys=ON that raises IntegrityError on commit,
            # rolling back this Action's status update too).
            logger.warning(
                "diagnose_incident: no Incident row for id=%s while routing to manual "
                "approval — skipping audit.record() too (no valid Incident to attach it to)",
                incident_id,
            )
        else:
            incident.status = IncidentStatus.PENDING_APPROVAL.value
            audit.record(
                session,
                incident_id=incident_id,
                # action_id must reference a real row when non-None (FK) —
                # fall back to None (the model supports this) rather than
                # the stale action_pk if the Action row doesn't exist.
                action_id=action_pk if action is not None else None,
                event_type=audit.EVENT_SAFE_ACTION_BLOCKED_BY_KILL_SWITCH,
                actor=audit.ACTOR_SYSTEM,
            )
        session.commit()


def _record_execution_result(
    incident_id: str, action_pk: str, command: str | None, succeeded: bool
) -> None:
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
                IncidentStatus.AUTO_FIXED.value if succeeded else IncidentStatus.FAILED.value
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
        session.commit()


def _route_risky_to_approval(incident_id: str, action_pk: str, action_id: str) -> None:
    """Story 4.2: resolve the proposed command (best-effort — some
    action_ids have none, see worker/executor/commands.py's Epic-4 note)
    purely for display, then park the Action/Incident at PENDING_APPROVAL.
    Mirrors _route_to_manual_approval()'s structure (Story 3.2) — same kind
    of "never auto-execute, wait for a human" transition, just reached via
    Story 3.1's classification instead of a kill-switch interrupt."""
    try:
        command = commands.get_command(action_id)
    except ExecutorError:
        command = None

    with db.SessionLocal() as session:
        action = session.get(Action, action_pk)
        incident = session.get(Incident, incident_id)
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


def _execute_approved_action(action_pk: str) -> None:
    """Runs the Command for an operator-APPROVED Action, checking the
    kill-switch fresh before EACH node — same AD-4 guarantee
    (_maybe_execute_safe_action's docstring, Story 3.2) applies here
    verbatim: "Safe lẫn Risky-đã-duyệt", no exceptions.

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
        incident = session.get(Incident, incident_id)
        if incident is not None:
            incident.status = IncidentStatus.EXECUTING.value
            session.commit()

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

    executed_any = False
    all_succeeded = True
    blocked_before_start = False
    # Resolved per-host below (not once up front) — restart_osd_daemon's
    # cephadm-mode command depends on which host's OSD daemon name(s) get
    # discovered (see worker/executor/commands.py::get_command). Every
    # other action_id's command is identical regardless of host.
    last_command: str | None = None

    for host in nodes:
        if _check_kill_switch_safe(incident_id):
            if executed_any:
                logger.warning(
                    "_execute_approved_action: kill-switch turned ON mid-execution for "
                    "action %s (incident %s) after at least one node already ran — "
                    "marking FAILED, not reverting to PENDING_APPROVAL",
                    action_pk,
                    incident_id,
                )
                all_succeeded = False
            else:
                blocked_before_start = True
            break

        try:
            command = commands.get_command(action_id_str, host, action_params)
        except ExecutorError:
            logger.warning(
                "_execute_approved_action: no Command for action_id=%s on host=%s "
                "(action %s, incident %s) — marking this node failed",
                action_id_str,
                host,
                action_pk,
                incident_id,
            )
            all_succeeded = False
            continue
        last_command = command

        try:
            execute_command(host, command)
            executed_any = True
        except ExecutorError:
            logger.exception(
                "_execute_approved_action: execution of action_id=%s failed on node %s "
                "(action %s)",
                action_id_str,
                host,
                action_pk,
            )
            all_succeeded = False
            executed_any = True
            # Keep trying remaining nodes, same rationale as
            # _maybe_execute_safe_action: the log should show exactly which
            # nodes failed even though the overall Action is already FAILED.

    if blocked_before_start:
        _revert_approved_action_to_pending(incident_id, action_pk)
        return

    _record_approved_execution_result(
        action_pk, command=last_command, succeeded=all_succeeded and executed_any
    )


def _revert_approved_action_to_pending(incident_id: str, action_pk: str) -> None:
    """AD-4, no exceptions: even an action a human already approved must not
    run while the kill-switch is on. Reverting to PENDING_APPROVAL (rather
    than leaving it APPROVED) means the poller won't just immediately retry
    it next tick — an operator has to look at it again."""
    logger.warning(
        "_execute_approved_action: kill-switch is ON — reverting approved action %s "
        "(incident %s) to PENDING_APPROVAL instead of executing",
        action_pk,
        incident_id,
    )
    with db.SessionLocal() as session:
        action = session.get(Action, action_pk)
        incident = session.get(Incident, incident_id)
        if action is not None:
            action.status = ActionStatus.PENDING_APPROVAL.value
        if incident is None:
            logger.warning(
                "_revert_approved_action_to_pending: no Incident row for id=%s — "
                "skipping audit.record() too (no valid Incident to attach it to)",
                incident_id,
            )
        else:
            incident.status = IncidentStatus.PENDING_APPROVAL.value
            audit.record(
                session,
                incident_id=incident_id,
                action_id=action_pk if action is not None else None,
                event_type=audit.EVENT_RISKY_ACTION_BLOCKED_BY_KILL_SWITCH,
                actor=audit.ACTOR_SYSTEM,
            )
        session.commit()


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
            incident.status = (
                IncidentStatus.RESOLVED.value if succeeded else IncidentStatus.FAILED.value
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
