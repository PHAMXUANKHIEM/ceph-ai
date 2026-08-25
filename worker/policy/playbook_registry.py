"""Versioned, fail-closed contracts for incident-remediation playbooks.

The registry is server-owned policy.  An LLM may select an ``action_id`` but
cannot invent or modify any of the fields below.  Missing execution contracts
are deliberately capped at L2 (human approval), as required by the autonomous
operations roadmap.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from shared.models import ActionClassification
from worker.policy.gate import classify_action


_LEVELS = {f"L{level}": level for level in range(6)}
_TARGET_SCHEMAS = {"cluster", "host", "osd", "pg", "manual"}

# Closed server-owned hook catalogues.  A contract cannot make an arbitrary
# string executable by naming it here; adding/removing a hook is a reviewed
# code change, while each contract still records the exact resolved hook ID.
PREFLIGHT_HOOKS = frozenset({
    "capability_and_operational_preflight",
})
POSTCHECK_HOOKS = frozenset({
    "fresh_health_telemetry",
    "node_and_cluster_health_telemetry",
    "osd_and_fault_health_telemetry",
    "osd_release_health_telemetry",
    "pool_application_health_telemetry",
    "pool_pg_health_telemetry",
})


@dataclass(frozen=True)
class PlaybookContract:
    action_id: str
    version: str
    target_schema: str
    max_autonomy: str
    conflict_scope: str
    max_targets: int
    cooldown_seconds: int
    command_builder: str | None
    command_builder_version: str | None
    preflight: str | None
    postcheck: str | None
    rollback: str | None = None

    def snapshot(self) -> dict:
        payload = asdict(self)
        payload["contract_checksum"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return payload


@dataclass(frozen=True)
class ContractDecision:
    allowed: bool
    reason: str
    contract: PlaybookContract | None
    effective_max_autonomy: str
    hard_failure: bool = False

    def snapshot(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "effective_max_autonomy": self.effective_max_autonomy,
            "hard_failure": self.hard_failure,
            "contract": self.contract.snapshot() if self.contract else None,
        }


@dataclass(frozen=True)
class PostcheckResult:
    outcome: str
    reason: str
    hook_id: str | None = None


def _contract(action_id: str, *, version: str = "1", target_schema: str,
              max_autonomy: str, conflict_scope: str, max_targets: int = 1,
              cooldown_seconds: int = 1800, command_builder: str | None = "closed_command_builder",
              command_builder_version: str | None = "1",
              preflight: str | None = "capability_and_operational_preflight",
              postcheck: str | None = "fresh_health_telemetry", rollback: str | None = None,
              ) -> PlaybookContract:
    return PlaybookContract(
        action_id=action_id, version=version, target_schema=target_schema,
        max_autonomy=max_autonomy, conflict_scope=conflict_scope,
        max_targets=max_targets, cooldown_seconds=cooldown_seconds,
        command_builder=command_builder, command_builder_version=command_builder_version,
        preflight=preflight,
        postcheck=postcheck, rollback=rollback,
    )


# First Pha-2 slice: every action the Incident AI can currently produce is
# registered.  Only the two existing low-blast-radius SAFE remediations have
# a complete L3 contract; all other actions remain approval-gated.
PLAYBOOKS: dict[str, PlaybookContract] = {
    "resync_ntp": _contract(
        "resync_ntp", version="3", target_schema="host", max_autonomy="L3",
        conflict_scope="host", max_targets=2,
    ),
    "enable_mon_msgr2": _contract(
        "enable_mon_msgr2", version="2", target_schema="cluster", max_autonomy="L3",
        conflict_scope="cluster", cooldown_seconds=3600,
    ),
    "crash_archive_all": _contract(
        "crash_archive_all", target_schema="cluster", max_autonomy="L3",
        conflict_scope="cluster", cooldown_seconds=3600,
    ),
    "restart_osd_daemon": _contract(
        # The generic policy is RISKY, but router_client may derive SAFE
        # contextually only after deterministic BlueStore OSD/host checks.
        "restart_osd_daemon", target_schema="osd", max_autonomy="L3",
        # A single Ceph health check can legitimately identify any number of
        # slow BlueStore daemons.  Zero means no numeric ceiling for this
        # contract; router_client still requires every osd.ID to be present
        # in Ceph's detail and mapped deterministically to its real host.
        conflict_scope="osd", max_targets=0,
        postcheck="osd_and_fault_health_telemetry",
    ),
    "finalize_osd_release": _contract(
        "finalize_osd_release", target_schema="cluster", max_autonomy="L2",
        conflict_scope="cluster", postcheck="osd_release_health_telemetry",
    ),
    "set_pool_pg_num": _contract(
        "set_pool_pg_num", target_schema="cluster", max_autonomy="L2",
        conflict_scope="cluster", postcheck="pool_pg_health_telemetry",
    ),
    "enable_pool_application": _contract(
        "enable_pool_application", target_schema="cluster", max_autonomy="L2",
        conflict_scope="cluster", postcheck="pool_application_health_telemetry",
    ),
    "hard_reboot_node": _contract(
        "hard_reboot_node", target_schema="host", max_autonomy="L2",
        conflict_scope="host", postcheck="node_and_cluster_health_telemetry",
    ),
    "pg_repair_force": _contract(
        "pg_repair_force", target_schema="pg", max_autonomy="L2",
        conflict_scope="pg", command_builder=None, preflight=None, postcheck=None,
        command_builder_version=None,
    ),
    "investigate_manually": _contract(
        "investigate_manually", target_schema="manual", max_autonomy="L1",
        conflict_scope="none", max_targets=0, cooldown_seconds=0,
        command_builder=None, preflight=None, postcheck=None,
        command_builder_version=None,
    ),
    "remove_invalid_rgw_default_key": _contract(
        "remove_invalid_rgw_default_key", target_schema="host", max_autonomy="L2",
        conflict_scope="cluster", postcheck="fresh_health_telemetry",
    ),
}


def get_contract(action_id: str) -> PlaybookContract | None:
    return PLAYBOOKS.get(action_id)


def validate_contract(contract: PlaybookContract) -> tuple[str, ...]:
    errors: list[str] = []
    if not contract.action_id:
        errors.append("missing action_id")
    if not contract.version:
        errors.append("missing version")
    if contract.target_schema not in _TARGET_SCHEMAS:
        errors.append(f"unknown target_schema={contract.target_schema!r}")
    if contract.max_autonomy not in _LEVELS:
        errors.append(f"invalid max_autonomy={contract.max_autonomy!r}")
    if not contract.conflict_scope:
        errors.append("missing conflict_scope")
    if contract.max_targets < 0:
        errors.append("max_targets must be non-negative")
    if contract.cooldown_seconds < 0:
        errors.append("cooldown_seconds must be non-negative")
    if contract.command_builder and not contract.command_builder_version:
        errors.append("missing command_builder_version")
    if contract.preflight and contract.preflight not in PREFLIGHT_HOOKS:
        errors.append(f"unregistered preflight hook={contract.preflight!r}")
    if contract.postcheck and contract.postcheck not in POSTCHECK_HOOKS:
        errors.append(f"unregistered postcheck hook={contract.postcheck!r}")
    return tuple(errors)


def registry_coverage(required_action_ids) -> tuple[str, ...]:
    """Return missing IDs deterministically; callers/tests decide whether to abort."""
    return tuple(sorted(set(required_action_ids) - set(PLAYBOOKS)))


def resolve_case_postcheck(
    *, action_id: str, playbook_version: str, contract_snapshot: dict | None,
) -> tuple[str | None, str | None]:
    """Resolve the immutable Case contract, never silently use current defaults."""
    if not isinstance(contract_snapshot, dict):
        return None, "case contract snapshot is missing or malformed"
    registry = contract_snapshot.get("registry")
    if not isinstance(registry, dict):
        return None, "case has no registered playbook contract"
    if registry.get("action_id") != action_id:
        return None, "case contract action_id does not match executed action"
    if str(registry.get("version")) != str(playbook_version):
        return None, "case contract version does not match frozen playbook_version"
    hook_id = registry.get("postcheck")
    if hook_id not in POSTCHECK_HOOKS:
        return None, f"case postcheck hook {hook_id!r} is not registered"
    return hook_id, None


def _fault_absence_postcheck(*, fault_present: bool, health: dict | None) -> PostcheckResult:
    # ``health`` is retained in the strategy signature so specialized hooks
    # can add daemon/pool predicates without changing Watcher's dispatch API.
    if fault_present:
        return PostcheckResult("FAILED", "fault family is still present in fresh telemetry")
    return PostcheckResult("PASSED", "fault family is absent from fresh telemetry")


_POSTCHECK_STRATEGIES = {hook_id: _fault_absence_postcheck for hook_id in POSTCHECK_HOOKS}


def run_postcheck(hook_id: str, *, fault_present: bool, health: dict | None) -> PostcheckResult:
    strategy = _POSTCHECK_STRATEGIES.get(hook_id)
    if strategy is None:
        return PostcheckResult("INCONCLUSIVE", f"postcheck hook {hook_id!r} cannot be resolved")
    result = strategy(fault_present=fault_present, health=health)
    return PostcheckResult(result.outcome, result.reason, hook_id=hook_id)


def describe_contract(contract: PlaybookContract, *, command_builder_available: bool) -> dict:
    """Admin-facing static eligibility; runtime target/evidence are evaluated later."""
    errors = validate_contract(contract)
    policy_class = classify_action(contract.action_id)
    status = "L3_READY"
    reason = "Contract đầy đủ; vẫn phải qua kill switch, target, evidence và operational gate."
    if errors:
        status = "INVALID"
        reason = "; ".join(errors)
    elif not contract.command_builder or not contract.preflight or not contract.postcheck:
        status = "L2_ONLY"
        reason = "Thiếu command builder, preflight hoặc post-check."
    elif not command_builder_available:
        status = "L2_ONLY"
        reason = "Command builder đã khai báo nhưng executor hiện không có implementation."
    elif _LEVELS[contract.max_autonomy] < _LEVELS["L3"]:
        status = "L2_ONLY"
        reason = f"Autonomy ceiling của contract là {contract.max_autonomy}."
    elif policy_class in {ActionClassification.DESTRUCTIVE, ActionClassification.RISKY}:
        if contract.action_id == "restart_osd_daemon":
            status = "CONDITIONAL"
            reason = "Policy mặc định RISKY; chỉ đạt L3 khi server xác minh đầy đủ từng BlueStore OSD và host tương ứng."
        else:
            status = "L2_ONLY"
            reason = f"Safety policy hiện phân loại {policy_class.value}."
    payload = contract.snapshot()
    payload.update({
        "policy_classification": policy_class.value,
        "eligibility_status": status,
        "eligibility_reason": reason,
        "command_builder_available": command_builder_available,
        "preflight_registered": contract.preflight in PREFLIGHT_HOOKS,
        "postcheck_registered": contract.postcheck in POSTCHECK_HOOKS,
    })
    return payload


def registry_status_rows(*, command_available) -> list[dict]:
    return [
        describe_contract(contract, command_builder_available=bool(command_available(action_id)))
        for action_id, contract in sorted(PLAYBOOKS.items())
    ]


def _validate_runtime_target(
    contract: PlaybookContract, target_nodes: list[str] | None, action_params: dict | None,
) -> str | None:
    if not isinstance(target_nodes, list) or not target_nodes:
        return "target_nodes is missing or malformed"
    if not all(isinstance(node, str) and node.strip() for node in target_nodes):
        return "target_nodes contains an invalid host"
    # max_targets=0 is the explicit unlimited sentinel.  It is safe for the
    # OSD playbook because its schema below still rejects missing/ambiguous
    # IDs; this only removes the numeric cap from a verified target set.
    if contract.max_targets > 0 and len(target_nodes) > contract.max_targets:
        return f"target count {len(target_nodes)} exceeds blast-radius ceiling {contract.max_targets}"
    if contract.target_schema == "osd":
        params = action_params if isinstance(action_params, dict) else {}
        cephadm_ids = params.get("cephadm_osd_ids")
        by_host = params.get("osd_ids_by_host")
        if isinstance(cephadm_ids, list):
            osd_ids = cephadm_ids
        elif isinstance(by_host, dict):
            osd_ids = [osd_id for values in by_host.values() if isinstance(values, list) for osd_id in values]
        else:
            return "OSD target schema requires deterministic osd ids"
        if not osd_ids or not all(str(osd_id).isdigit() for osd_id in osd_ids):
            return "OSD target contains an invalid id"
        if contract.max_targets > 0 and len(osd_ids) > contract.max_targets:
            return f"OSD target count {len(osd_ids)} exceeds blast-radius ceiling {contract.max_targets}"
    return None


def evaluate_auto_execution(
    action_id: str, classification: str, *, target_nodes: list[str] | None = None,
    action_params: dict | None = None, command_builder_available: bool = False,
) -> ContractDecision:
    """Return an L3 execution decision; every missing/invalid field fails closed."""
    contract = get_contract(action_id)
    if contract is None:
        return ContractDecision(False, "playbook is not registered", None, "L2")
    errors = validate_contract(contract)
    if errors:
        return ContractDecision(False, "; ".join(errors), contract, "L2")
    if not contract.command_builder or not contract.preflight or not contract.postcheck:
        return ContractDecision(
            False, "playbook lacks command_builder, preflight or postcheck", contract, "L2",
        )
    if not command_builder_available:
        return ContractDecision(False, "registered command builder is unavailable", contract, "L2")
    if classification not in {ActionClassification.READ_ONLY.value, ActionClassification.SAFE.value}:
        return ContractDecision(False, f"classification {classification} is not auto-executable", contract, "L2")
    if _LEVELS[contract.max_autonomy] < _LEVELS["L3"]:
        return ContractDecision(
            False, f"playbook ceiling is {contract.max_autonomy}", contract, contract.max_autonomy,
        )
    target_error = _validate_runtime_target(contract, target_nodes, action_params)
    if target_error:
        malformed = target_error.startswith("target_nodes") or "invalid id" in target_error
        return ContractDecision(False, target_error, contract, "L2", hard_failure=malformed)
    return ContractDecision(True, "versioned playbook contract permits L3", contract, contract.max_autonomy)
