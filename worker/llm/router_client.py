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
from shared.models import Action, ActionClassification, ActionStatus, Cluster, Incident, IncidentStatus
from shared.ceph_releases import codename_for_version
from shared.cluster_nodes import configured_nodes
from shared.router_client import build_router_client
from shared.telegram_alerts import send_auto_remediation_alert
from worker.backup import engine as backup_engine
from worker.executor import cluster_deploy, commands, volume_perf
from worker.executor.ssh_executor import ExecutorError, execute_command
from worker.policy import gate
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
    action_pk: str, action_params: dict, progress: list[dict]
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

    mon_nodes = [h.strip() for h in settings.ceph_mon_nodes.split(",") if h.strip()]
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
        execute_command(mon_host, command)
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


def _set_upgrade_osd_flags(action_pk: str, mon_host: str, progress: list[dict]) -> None:
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
        execute_command(mon_host, command)
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


def _unset_upgrade_osd_flags(action_pk: str, mon_host: str, progress: list[dict]) -> None:
    """Always attempted after a package-based upgrade's per-host loop ends
    — success, partial failure, or blocked by the kill-switch before any
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
        execute_command(mon_host, command)
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

_KILL_SWITCH_SKIPPED_MESSAGE = "Bị chặn bởi kill-switch giữa chừng — chưa chạy trên node này"
_NOTHING_TO_RESTART_MESSAGE = "Không tìm thấy systemd unit nào cần khởi động lại trên node này"
# Code review fix (2026-08-04, Story 7.2): a host whose install step failed
# must not have its later MON/MGR/OSD/MDS-RGW unit restarted — that would
# restart a daemon against a possibly broken/partial package install.
_INSTALL_FAILED_SKIP_MESSAGE = "Bỏ qua vì cài đặt gói thất bại trên node này"


def _execute_package_upgrade_action(
    action_pk: str,
    action_id_str: str,
    nodes: list[str],
    action_params: dict | None,
    incident_id: str,
) -> None:
    """The package-based Cluster Upgrade's 5-phase executor — install-only
    on every configured host, then restart MON-role hosts' MON units, then
    MGR-role hosts' MGR units, then OSD-role hosts' OSD units, then any
    remaining host with a leftover discovered MDS/RGW unit (matches
    `_discover_ceph_units`'s daemon-type coverage). A host with more than
    one role (e.g. MON+OSD) installs exactly once (phase 0) and restarts
    once per role-phase it belongs to — its MON unit in the MON phase, its
    OSD unit in the OSD phase, never both together and never twice.

    Kill-switch is re-checked fresh before EVERY step across the WHOLE
    5-phase sequence (AD-4) — install and restart steps alike. A
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
    roles_by_host: dict[str, set[str]] = {n["host"]: set(n["roles"]) for n in configured_nodes()}
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
        "blocked_before_start": False,
        "stopped_mid_sequence": False,
        "last_command": None,
        # Code review fix (2026-08-04): hosts whose install step failed —
        # every later restart phase (MON/MGR/OSD, and the MDS/RGW dynamic
        # phase) must skip these rather than restart a daemon against a
        # possibly broken/partial install.
        "failed_install_hosts": set(),
    }

    # Same bracket as the pre-7.2 code — set BEFORE phase 0, unset once
    # after the whole sequence (or an early stop), never per-phase (see
    # this function's own docstring / epic-7-context.md's Technical
    # Decisions on why the bracket's scope is the full upgrade window).
    mon_nodes_cfg = [h.strip() for h in settings.ceph_mon_nodes.split(",") if h.strip()]
    upgrade_flags_mon_host: str | None = None
    if _check_kill_switch_safe(incident_id):
        state["blocked_before_start"] = True
    elif mon_nodes_cfg:
        upgrade_flags_mon_host = mon_nodes_cfg[0]
        _set_upgrade_osd_flags(action_pk, upgrade_flags_mon_host, progress)
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
        if not state["blocked_before_start"]:
            install_entries = [p for p in progress if p.get("phase") == _UPGRADE_PHASE_INSTALL]
            _run_install_phase(
                action_pk, action_id_str, nodes, install_entries, action_params, progress, incident_id, state
            )

        if not state["blocked_before_start"] and not state["stopped_mid_sequence"]:
            for phase_name, role, daemon_types in _ROLE_RESTART_PHASES:
                if state["blocked_before_start"] or state["stopped_mid_sequence"]:
                    break
                phase_entries = [p for p in progress if p.get("phase") == phase_name]
                _run_restart_phase(
                    action_pk, action_id_str, phase_name, role_hosts[role], phase_entries,
                    daemon_types, action_params, progress, incident_id, state,
                )

        if not state["blocked_before_start"] and not state["stopped_mid_sequence"]:
            _run_restart_phase(
                action_pk, action_id_str, _UPGRADE_PHASE_MDS_RGW, nodes, None,
                ("mds", "rgw"), action_params, progress, incident_id, state,
            )
    finally:
        if state["stopped_mid_sequence"]:
            # Every step that never got a chance to run (this phase's own
            # not-yet-visited candidates, AND every host in a phase that
            # never even started) is still "pending" at this point — flip
            # all of them to "skipped" in one pass, same message the
            # pre-7.2 per-host loop already used.
            for entry in progress:
                if entry["status"] == "pending":
                    entry["status"] = "skipped"
                    entry["error"] = _KILL_SWITCH_SKIPPED_MESSAGE
            _write_action_progress(action_pk, progress)

        if upgrade_flags_mon_host is not None:
            _unset_upgrade_osd_flags(action_pk, upgrade_flags_mon_host, progress)

    if state["blocked_before_start"]:
        _revert_approved_action_to_pending(incident_id, action_pk)
        return

    # Code review fix (2026-08-04): a kill-switch trip mid-sequence must
    # also block this finalize step — without `not state["stopped_mid_
    # sequence"]`, `ceph osd require-osd-release <codename>` could still
    # run on the MON after the operator hit the emergency kill-switch.
    if state["executed_any"] and not state["stopped_mid_sequence"]:
        _finalize_package_upgrade_osd_release(action_pk, action_params, progress)

    _record_approved_execution_result(
        action_pk,
        command=state["last_command"],
        succeeded=state["all_succeeded"] and state["executed_any"],
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
) -> None:
    """Phase 0 — install-only on every host, positional addressing into
    `phase_entries` (one pre-populated "pending" dict per host, same
    order), mirroring the pre-7.2 generic per-host loop's own kill-switch/
    failure semantics exactly (just against the install-only command
    variant instead of "install && restart everything")."""
    total = len(hosts)
    for index, host in enumerate(hosts):
        entry = phase_entries[index]
        if _check_kill_switch_safe(incident_id):
            if state["executed_any"]:
                state["all_succeeded"] = False
                state["stopped_mid_sequence"] = True
            else:
                state["blocked_before_start"] = True
            return

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
            entry["status"] = "failed"
            entry["error"] = str(exc)
            _write_action_progress(action_pk, progress)
            continue
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
            execute_command(host, command)
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
            entry["status"] = "failed"
            entry["error"] = str(exc)
            entry["finished_at"] = datetime.utcnow().isoformat()
            _write_action_progress(action_pk, progress)
            continue

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
        # Code review fix (2026-08-04): a host whose install step already
        # failed (Fix 1) must not have its unit(s) restarted here — that
        # would restart a daemon against a possibly broken/partial
        # install. Checked before the kill-switch so it never consumes a
        # kill-switch check and never blocks other hosts in this phase.
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

        if _check_kill_switch_safe(incident_id):
            if state["executed_any"]:
                state["all_succeeded"] = False
                state["stopped_mid_sequence"] = True
            else:
                state["blocked_before_start"] = True
            if phase_entries is None:
                # Code review fix (2026-08-04): the MDS/RGW phase's entries
                # are appended dynamically, only for a host confirmed to
                # have something to restart — without this, a host never
                # reached because of a mid-phase kill-switch trip would
                # vanish from the audit trail entirely instead of being
                # recorded as skipped (the MON/MGR/OSD phases avoid this
                # because their pre-populated "pending" placeholders already
                # get flipped to "skipped" by the outer cleanup loop, which
                # this dynamic phase has nothing for it to flip).
                for later_host in candidate_hosts[index:]:
                    progress.append(
                        {
                            "host": later_host,
                            "phase": phase_name,
                            "status": "skipped",
                            "error": _KILL_SWITCH_SKIPPED_MESSAGE,
                        }
                    )
                _write_action_progress(action_pk, progress)
            return

        try:
            discovered = commands._discover_ceph_units(host)
        except ExecutorError as exc:
            state["all_succeeded"] = False
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
            continue

        if not any(discovered.get(daemon_type) for daemon_type in daemon_types):
            if phase_entries is not None:
                entry = phase_entries[index]
                entry["status"] = "skipped"
                entry["error"] = _NOTHING_TO_RESTART_MESSAGE
                _write_action_progress(action_pk, progress)
            continue  # dynamic (MDS/RGW) phase: nothing to restart here -> no entry at all

        try:
            command = commands.get_command(
                action_id_str,
                host,
                dict(action_params, _phase="restart_only", _phase_daemon_types=list(daemon_types)),
            )
        except ExecutorError as exc:
            state["all_succeeded"] = False
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
            continue
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
            execute_command(host, command)
            state["executed_any"] = True
        except ExecutorError as exc:
            state["all_succeeded"] = False
            state["executed_any"] = True
            entry["status"] = "failed"
            entry["error"] = str(exc)
            entry["finished_at"] = datetime.utcnow().isoformat()
            _write_action_progress(action_pk, progress)
            continue

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
        # 2026-08-10 (multi-tenant remediation Phase 1): resolve THIS
        # Incident's own cluster's SSH creds here, inside the same session —
        # by the time this function runs (a DB poll, long after the RabbitMQ
        # envelope that carried them at diagnosis time is gone), the
        # envelope itself is unavailable, so Incident.cluster_id -> Cluster
        # is the only place left to look them up. None means the default
        # cluster (execute_command()'s own settings.* fallback applies).
        if incident is not None and incident.cluster_id is not None:
            cluster = session.get(Cluster, incident.cluster_id)
            ssh_user = cluster.ssh_user if cluster is not None else None
            ssh_key_path = cluster.ssh_key_path if cluster is not None else None
        else:
            ssh_user = None
            ssh_key_path = None

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
    # _write_action_progress/_check_kill_switch_safe unchanged (both are
    # already generic enough to accept its own step-shaped progress lists).
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
            _check_kill_switch_safe,
        )
        _record_approved_execution_result(action_pk, command=None, succeeded=succeeded)
        return

    # 2026-07-29: Volumes page "Đo hiệu năng tối đa" (load sweep) — same
    # reasoning as the cluster-deploy branch just above: this is its own
    # multi-step orchestrator (sweeps fio iodepth 1->256, checking the
    # kill-switch before each step), not a single command fanned out to
    # target_nodes.
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
        succeeded = volume_perf.run(
            action_pk,
            action_params,
            incident_id,
            _write_action_progress,
            _check_kill_switch_safe,
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
            _write_action_progress,
            _check_kill_switch_safe,
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
        _execute_package_upgrade_action(action_pk, action_id_str, nodes, action_params, incident_id)
        return

    executed_any = False
    all_succeeded = True
    blocked_before_start = False
    # Resolved per-host below (not once up front) — restart_osd_daemon's
    # cephadm-mode command depends on which host's OSD daemon name(s) get
    # discovered (see worker/executor/commands.py::get_command). Every
    # other action_id's command is identical regardless of host.
    last_command: str | None = None
    total_nodes = len(nodes)
    progress = [{"host": host, "status": "pending"} for host in nodes]
    _write_action_progress(action_pk, progress)

    for node_index, host in enumerate(nodes, start=1):
        if blocked_before_start:
            break
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
                # Remaining nodes never ran — record why, so the step log
                # doesn't just show them stuck at "pending" forever with no
                # explanation.
                for later in progress[node_index - 1 :]:
                    later["status"] = "skipped"
                    later["error"] = "Bị chặn bởi kill-switch giữa chừng — chưa chạy trên node này"
                _write_action_progress(action_pk, progress)
            else:
                blocked_before_start = True
            break

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
            execute_command(host, command, user=ssh_user, key_path=ssh_key_path)
            executed_any = True
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
            # Keep trying remaining nodes, same rationale as
            # _maybe_execute_safe_action: the log should show exactly which
            # nodes failed even though the overall Action is already FAILED.
            continue

        progress[node_index - 1]["status"] = "done"
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
