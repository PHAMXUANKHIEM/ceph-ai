"""Versioned, fail-closed contracts for incident-remediation playbooks.

The registry is server-owned policy.  An LLM may select an ``action_id`` but
cannot invent or modify any of the fields below.  Missing execution contracts
are deliberately capped at L2 (human approval), as required by the autonomous
operations roadmap.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from shared.models import ActionClassification


_LEVELS = {f"L{level}": level for level in range(6)}
_TARGET_SCHEMAS = {"cluster", "host", "osd", "pg", "manual"}


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
    preflight: str | None
    postcheck: str | None
    rollback: str | None = None

    def snapshot(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ContractDecision:
    allowed: bool
    reason: str
    contract: PlaybookContract | None
    effective_max_autonomy: str

    def snapshot(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "effective_max_autonomy": self.effective_max_autonomy,
            "contract": self.contract.snapshot() if self.contract else None,
        }


def _contract(action_id: str, *, version: str = "1", target_schema: str,
              max_autonomy: str, conflict_scope: str, max_targets: int = 1,
              cooldown_seconds: int = 1800, command_builder: str | None = "closed_command_builder",
              preflight: str | None = "capability_and_operational_preflight",
              postcheck: str | None = "fresh_health_telemetry", rollback: str | None = None,
              ) -> PlaybookContract:
    return PlaybookContract(
        action_id=action_id, version=version, target_schema=target_schema,
        max_autonomy=max_autonomy, conflict_scope=conflict_scope,
        max_targets=max_targets, cooldown_seconds=cooldown_seconds,
        command_builder=command_builder, preflight=preflight,
        postcheck=postcheck, rollback=rollback,
    )


# First Pha-2 slice: every action the Incident AI can currently produce is
# registered.  Only the two existing low-blast-radius SAFE remediations have
# a complete L3 contract; all other actions remain approval-gated.
PLAYBOOKS: dict[str, PlaybookContract] = {
    "resync_ntp": _contract(
        "resync_ntp", target_schema="host", max_autonomy="L3",
        conflict_scope="host", max_targets=1,
    ),
    "crash_archive_all": _contract(
        "crash_archive_all", target_schema="cluster", max_autonomy="L3",
        conflict_scope="cluster", cooldown_seconds=3600,
    ),
    "restart_osd_daemon": _contract(
        # The generic policy is RISKY, but router_client may derive SAFE
        # contextually only after deterministic BlueStore OSD/host checks.
        "restart_osd_daemon", target_schema="osd", max_autonomy="L3",
        conflict_scope="osd", postcheck="osd_and_fault_health_telemetry",
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
    ),
    "investigate_manually": _contract(
        "investigate_manually", target_schema="manual", max_autonomy="L1",
        conflict_scope="none", max_targets=0, cooldown_seconds=0,
        command_builder=None, preflight=None, postcheck=None,
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
    return tuple(errors)


def evaluate_auto_execution(action_id: str, classification: str) -> ContractDecision:
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
    if classification not in {ActionClassification.READ_ONLY.value, ActionClassification.SAFE.value}:
        return ContractDecision(False, f"classification {classification} is not auto-executable", contract, "L2")
    if _LEVELS[contract.max_autonomy] < _LEVELS["L3"]:
        return ContractDecision(
            False, f"playbook ceiling is {contract.max_autonomy}", contract, contract.max_autonomy,
        )
    return ContractDecision(True, "versioned playbook contract permits L3", contract, contract.max_autonomy)
