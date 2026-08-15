"""Closed-loop Vitastor remediation — the Vitastor counterpart of the Ceph
Incident->Action->Audit pipeline (worker/policy/gate.py + worker/executor/
commands.py + shared/audit.py), kept entirely inside the ``vitastor`` package
so a Vitastor policy/execution decision can never touch Ceph state.

Design mirrors the Ceph pipeline's safety posture exactly:

* ``action_id`` is a CLOSED enum (``VALID_ACTION_IDS``) — never free-text
  shell. Every command is produced by a builder in ``_COMMAND_BUILDERS``.
* Classification is conservative (AD-5): SAFE only for an explicit allowlist
  hit; everything else (including an unknown ``action_id``) is RISKY and
  approval-gated.
* Execution only ever runs against a host on the cluster's own
  allowlist (``known_hosts``) — the same posture as
  dashboard/routes/vitastor.py's ``_cluster_log_hosts`` guard.

The deterministic proposer (``propose_from_status``) is a pure function over a
read-only status snapshot, so the watcher never has to ask an AI what to run —
it maps an observed fault (an OSD reported ``up: false``) to a single
approval-gated proposal, the same pattern watcher/device_health_monitor.py
uses on the Ceph side.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from shared import db
from shared.models import (
    VitastorActionClassification,
    VitastorActionStatus,
    VitastorAuditEntry,
    VitastorRemediationAction,
)
from vitastor.operations import VitastorOperationError, _run

# Vitastor OSD ids are small non-negative integers (the systemd template unit
# is ``vitastor-osd@<id>.service``). Validating to digits keeps a telemetry
# value from ever reaching a shell as anything but a number.
_OSD_ID_RE = re.compile(r"^[0-9]{1,10}$")

# Statuses that still "own" a dedup_key — a new proposal for the same
# underlying fault must not be created while one of these is outstanding.
_OPEN_STATUSES = (
    VitastorActionStatus.PENDING_APPROVAL.value,
    VitastorActionStatus.APPROVED.value,
    VitastorActionStatus.EXECUTING.value,
)


class VitastorRemediationError(RuntimeError):
    pass


# --- Policy (Safe/Risky) ---------------------------------------------------

# Conservative default (AD-5): SAFE is only ever this explicit allowlist.
SAFE_ACTION_IDS = frozenset({"resync_time"})
RISKY_ACTION_IDS = frozenset({
    "start_osd_service",
    "restart_osd_service",
    "restart_mon_service",
    "restart_etcd_service",
    "investigate_manually",
})
VALID_ACTION_IDS = SAFE_ACTION_IDS | RISKY_ACTION_IDS


def classify_action(action_id: str) -> VitastorActionClassification:
    """SAFE only for an explicit allowlist hit; RISKY for everything else,
    including any ``action_id`` not recognized at all — identical posture to
    worker/policy/gate.py::classify_action."""
    if action_id in SAFE_ACTION_IDS and action_id not in RISKY_ACTION_IDS:
        return VitastorActionClassification.SAFE
    return VitastorActionClassification.RISKY


# --- Command builders (closed) ---------------------------------------------

def _require_osd_id(params: dict) -> str:
    osd_id = str((params or {}).get("osd_id", "")).strip()
    if not _OSD_ID_RE.fullmatch(osd_id):
        raise VitastorRemediationError(f"OSD id không hợp lệ: {osd_id!r}")
    return osd_id


def _start_osd(params: dict) -> str:
    return f"systemctl start vitastor-osd@{_require_osd_id(params)}"


def _restart_osd(params: dict) -> str:
    return f"systemctl restart vitastor-osd@{_require_osd_id(params)}"


def _restart_mon(_params: dict) -> str:
    return "systemctl restart vitastor-mon"


def _restart_etcd(_params: dict) -> str:
    return "systemctl restart vitastor-etcd"


def _resync_time(_params: dict) -> str:
    # Best-effort clock resync across the common time daemons — a metadata-
    # level fix that never touches cluster data, the Vitastor analogue of the
    # Ceph pipeline's SAFE ``resync_ntp``.
    return (
        "chronyc makestep 2>/dev/null "
        "|| systemctl restart chrony 2>/dev/null "
        "|| systemctl restart chronyd 2>/dev/null "
        "|| systemctl restart systemd-timesyncd"
    )


# action_id -> builder returning the shell command, or None for a no-op
# (investigate_manually just records the fault as handled and runs nothing).
_COMMAND_BUILDERS = {
    "start_osd_service": _start_osd,
    "restart_osd_service": _restart_osd,
    "restart_mon_service": _restart_mon,
    "restart_etcd_service": _restart_etcd,
    "resync_time": _resync_time,
    "investigate_manually": lambda _params: None,
}


def build_command(action_id: str, params: dict | None) -> str | None:
    if action_id not in _COMMAND_BUILDERS:
        raise VitastorRemediationError(f"action_id không được hỗ trợ: {action_id!r}")
    return _COMMAND_BUILDERS[action_id](params or {})


# --- Host allowlist --------------------------------------------------------

def known_hosts(cluster, datasets: dict | None = None) -> set[str]:
    """Every host the remediation executor is allowed to SSH into for this
    cluster: the management host, any deploy-topology node, and any OSD parent
    seen in live (``datasets``) or cached (``last_status_json``) telemetry."""
    hosts = {str(cluster.management_host or "").strip()}
    try:
        cached = json.loads(cluster.last_status_json or "{}")
    except (TypeError, ValueError):
        cached = {}
    deployment = cached.get("deployment") if isinstance(cached, dict) else None
    if isinstance(deployment, dict):
        for node in deployment.get("nodes", []):
            if isinstance(node, dict) and str(node.get("host", "")).strip():
                hosts.add(str(node["host"]).strip())
    osd_rows = (datasets or {}).get("osds") if datasets else (cached.get("osds") if isinstance(cached, dict) else None)
    for row in osd_rows or []:
        if isinstance(row, dict) and row.get("type") == "osd" and str(row.get("parent", "")).strip():
            hosts.add(str(row["parent"]).strip())
    return {host for host in hosts if host}


# --- Deterministic proposer ------------------------------------------------

def propose_from_status(datasets: dict, summary: dict) -> list[dict]:
    """Pure function: derive approval-gated remediation proposals from a
    read-only status snapshot. Currently every OSD reported ``up: false``
    becomes one ``restart_osd_service`` proposal (RISKY — never auto-run).
    Restart is deliberate: a DOWN OSD may still have an active but wedged
    systemd unit, in which case ``systemctl start`` is a successful no-op."""
    proposals: list[dict] = []
    for row in datasets.get("osds") or []:
        if not isinstance(row, dict) or row.get("type") != "osd" or row.get("up"):
            continue
        osd_id = str(row.get("name") if row.get("name") is not None else row.get("id", "")).strip()
        host = str(row.get("parent") or "").strip()
        if not _OSD_ID_RE.fullmatch(osd_id) or not host:
            continue
        proposals.append({
            "action_id": "restart_osd_service",
            "target_host": host,
            "action_params": {"osd_id": osd_id},
            "rationale": (
                f"OSD {osd_id} trên {host} đang DOWN — đề xuất khởi động lại "
                f"daemon vitastor-osd@{osd_id} (chờ duyệt)."
            ),
            "dedup_key": f"restart_osd_service:{host}:{osd_id}",
        })
    return proposals


# --- Execution -------------------------------------------------------------

def run_remediation(
    action_id: str, action_params: dict | None, target_host: str,
    ssh_user: str, ssh_key_path: str, allowed_hosts: set[str],
) -> str:
    """Build the closed command and run it over SSH on an allowlisted host.
    Returns truncated output; a no-op action_id returns an empty string."""
    command = build_command(action_id, action_params)
    if command is None:
        return ""
    host = (target_host or "").strip()
    if not host:
        raise VitastorRemediationError("Thiếu host thực thi cho hành động")
    if host not in allowed_hosts:
        raise VitastorRemediationError(f"Host {host!r} không thuộc cụm Vitastor")
    return _run(host, ssh_user, ssh_key_path, command)[-4000:]


# --- Audit -----------------------------------------------------------------

def record_audit(session, cluster_id: str, action_pk: str | None, event_type: str, actor: str, detail: str | None = None) -> None:
    """Append one row to the Vitastor audit trail. Caller owns the commit —
    same append-only posture as shared/audit.py::record."""
    session.add(VitastorAuditEntry(
        cluster_id=cluster_id, action_pk=action_pk,
        event_type=event_type, actor=actor,
        detail=(detail or "")[:4000] or None,
    ))


# --- Watcher reconciliation ------------------------------------------------

def _auto_execute(session, row: VitastorRemediationAction, cluster, allowed: set[str]) -> None:
    """Run a SAFE proposal immediately (system actor), recording every
    transition. Failure is captured on the row, never raised — one bad
    auto-fix must not abort the watcher poll."""
    row.status = VitastorActionStatus.EXECUTING.value
    record_audit(session, cluster.id, row.id, "AUTO_EXECUTE", "vitastor-monitor", row.proposed_command)
    try:
        output = run_remediation(
            row.action_id, json.loads(row.action_params or "{}"), row.target_host,
            cluster.ssh_user, cluster.ssh_key_path, allowed,
        )
        row.status = VitastorActionStatus.AUTO_EXECUTED.value
        row.result_output = output
        row.executed_at = datetime.utcnow()
        record_audit(session, cluster.id, row.id, "AUTO_EXECUTED", "vitastor-monitor", output[-500:] or "(không có output)")
    except (VitastorRemediationError, VitastorOperationError) as exc:
        row.status = VitastorActionStatus.FAILED.value
        row.error_message = str(exc)
        record_audit(session, cluster.id, row.id, "FAILED", "vitastor-monitor", str(exc))


def reconcile_monitor_proposals(cluster, datasets: dict, summary: dict) -> list[dict]:
    """Insert PENDING_APPROVAL rows for newly-observed faults (deduped against
    still-open proposals), auto-execute any SAFE ones, and return the list of
    NEW RISKY pending proposals so the caller can raise a Telegram alert.

    ``cluster`` may be a detached VitastorCluster — only its already-loaded
    scalar attributes are read, never a lazy relationship."""
    proposals = propose_from_status(datasets, summary)
    allowed = known_hosts(cluster, datasets)
    new_pending: list[dict] = []
    with db.SessionLocal() as session:
        current_keys = {proposal["dedup_key"] for proposal in proposals}
        # A terminal action continues to own its key while the same fault is
        # observable.  Otherwise a fast execution followed by one stale DOWN
        # poll creates another approval every minute.  Once telemetry shows
        # recovery, release the key so a later, genuinely new outage can
        # create a fresh proposal.
        session.query(VitastorRemediationAction).filter(
            VitastorRemediationAction.cluster_id == cluster.id,
            VitastorRemediationAction.dedup_key != "",
            ~VitastorRemediationAction.dedup_key.in_(current_keys),
            ~VitastorRemediationAction.status.in_(_OPEN_STATUSES),
        ).update({VitastorRemediationAction.dedup_key: ""}, synchronize_session=False)
        owned_keys = {
            key for (key,) in session.query(VitastorRemediationAction.dedup_key).filter(
                VitastorRemediationAction.cluster_id == cluster.id,
                VitastorRemediationAction.dedup_key != "",
            ).all()
        }
        for proposal in proposals:
            if proposal["dedup_key"] in owned_keys:
                continue
            try:
                command = build_command(proposal["action_id"], proposal["action_params"])
            except VitastorRemediationError:
                continue
            classification = classify_action(proposal["action_id"])
            row = VitastorRemediationAction(
                cluster_id=cluster.id, source="MONITOR",
                action_id=proposal["action_id"], classification=classification.value,
                status=VitastorActionStatus.PENDING_APPROVAL.value,
                target_host=proposal["target_host"],
                action_params=json.dumps(proposal["action_params"]),
                proposed_command=command, rationale=proposal["rationale"],
                dedup_key=proposal["dedup_key"], requested_by="vitastor-monitor",
            )
            session.add(row)
            session.flush()
            owned_keys.add(proposal["dedup_key"])
            if classification is VitastorActionClassification.SAFE:
                _auto_execute(session, row, cluster, allowed)
            else:
                record_audit(session, cluster.id, row.id, "PROPOSED", "vitastor-monitor", proposal["rationale"])
                new_pending.append({
                    "action_id": row.action_id, "target_host": row.target_host,
                    "rationale": row.rationale,
                })
        session.commit()
    return new_pending
