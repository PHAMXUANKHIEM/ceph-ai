"""Safe lab-only synthetic incident injection.

Synthetic incidents exercise the normal Incident -> AI diagnosis pipeline
without issuing a Ceph command.  They are deliberately marked in the
incident evidence so the Watcher does not reconcile them against live health
and the Worker can fail closed before any executor is reached.
"""

from __future__ import annotations

import functools
import json
import uuid
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from shared.time import utc_now

from shared.incident_actions import cancel_pending_actions
from shared.models import Cluster, Incident, IncidentStatus
from watcher import publisher

SYNTHETIC_EVIDENCE_KEY = "synthetic_injection"
SYNTHETIC_MODE = "shadow-only"


class SyntheticInjectionError(ValueError):
    """Raised when an injection request fails a safety invariant."""


@dataclass(frozen=True)
class Scenario:
    id: str
    ceph_code: str
    ceph_code_prefix: str | None
    severity: str
    message: str
    log_excerpt: str
    metrics: dict
    diagnosis_markers: tuple[tuple[str, ...], ...]
    acceptable_action_ids: tuple[str, ...]
    detection_timeout_seconds: int
    verified_real_incident: bool


CATALOG_PATH = Path(__file__).resolve().parents[1] / "worker/policy/failure_lab_scenarios.yaml"
_PROVENANCE_SOURCES = {"synthetic_fixture", "anonymized_incident"}


def _fail(scenario_id: object, message: str) -> SyntheticInjectionError:
    return SyntheticInjectionError(f"{scenario_id}: {message}")


def _scenario_code(entry: dict) -> tuple[str, str | None]:
    """(ceph_code, prefix): a fixed code, or a prefix plus a sample suffix."""
    scenario_id, prefix, suffix = entry.get("id"), entry.get("ceph_code_prefix"), entry.get("sample_suffix", "")
    if ("ceph_code" in entry) == (prefix is not None):
        raise _fail(scenario_id, "define exactly one of ceph_code or ceph_code_prefix")
    if prefix is not None and (not isinstance(prefix, str) or not prefix):
        raise _fail(scenario_id, "ceph_code_prefix must be a non-empty string")
    if not isinstance(suffix, str):
        raise _fail(scenario_id, "sample_suffix must be a string")
    code = entry.get("ceph_code") or f"{prefix}{suffix}"
    if not isinstance(code, str) or not code:
        raise _fail(scenario_id, "invalid ceph_code")
    return code, prefix


def _replay_fields(entry: dict) -> tuple[str, dict]:
    replay, scenario_id = entry.get("replay"), entry.get("id")
    if not isinstance(replay, dict) or not isinstance(replay.get("log_excerpt"), str) or not replay["log_excerpt"]:
        raise _fail(scenario_id, "replay.log_excerpt is required")
    if not isinstance(replay.get("metrics"), dict):
        raise _fail(scenario_id, "replay.metrics must be a mapping")
    return replay["log_excerpt"], dict(replay["metrics"])


def _marker_groups(raw: object, scenario_id: object) -> tuple[tuple[str, ...], ...]:
    """Each marker is one required concept; "a|b|c" accepts any spelling of it
    (diagnoses are written in Vietnamese or English)."""
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) and item.strip() for item in raw):
        raise _fail(scenario_id, "at least one diagnosis marker is required")
    return tuple(tuple(part.strip() for part in item.split("|") if part.strip()) for item in raw)


def _expect_fields(entry: dict) -> tuple[tuple[tuple[str, ...], ...], tuple[str, ...], int]:
    expect, scenario_id = entry.get("expect"), entry.get("id")
    if not isinstance(expect, dict):
        raise _fail(scenario_id, "expect is required")
    actions, timeout = expect.get("acceptable_action_ids"), expect.get("detection_timeout_seconds")
    if not isinstance(actions, list) or not all(isinstance(item, str) and item for item in actions):
        raise _fail(scenario_id, "acceptable_action_ids must be strings")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
        raise _fail(scenario_id, "detection timeout must be in [1, 3600]")
    return _marker_groups(expect.get("diagnosis_markers"), scenario_id), tuple(actions), timeout


def _reviewed_at_ok(value: object) -> bool:
    try:
        reviewed_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return reviewed_at.tzinfo is not None


def _verified_provenance(entry: dict) -> bool:
    """True for a reviewed anonymized incident; the catalog never grants golden approval."""
    provenance, scenario_id = entry.get("provenance"), entry.get("id")
    if not isinstance(provenance, dict) or provenance.get("source") not in _PROVENANCE_SOURCES:
        raise _fail(scenario_id, "provenance.source is required")
    verified = provenance.get("verified_real_incident")
    if not isinstance(verified, bool) or verified != (provenance["source"] == "anonymized_incident"):
        raise _fail(scenario_id, "verified_real_incident must be a boolean that agrees with provenance.source")
    if provenance.get("golden_set_approved") is not False:
        raise _fail(scenario_id, "golden-set approval cannot be granted by the scenario catalog")
    if verified:
        for field in ("source_incident_ref", "reviewed_by", "reviewed_at"):
            if not isinstance(provenance.get(field), str) or not provenance[field].strip():
                raise _fail(scenario_id, f"anonymized incidents require provenance.{field}")
        if not _reviewed_at_ok(provenance["reviewed_at"]):
            raise _fail(scenario_id, "provenance.reviewed_at must be an ISO-8601 timestamp with a timezone")
    return verified


def _scenario(entry: object, seen: dict) -> Scenario:
    if not isinstance(entry, dict):
        raise SyntheticInjectionError("Every Failure Lab scenario must be a mapping")
    scenario_id = entry.get("id")
    if not isinstance(scenario_id, str) or not scenario_id or scenario_id in seen:
        raise SyntheticInjectionError("Scenario ids must be non-empty and unique")
    if entry.get("mode") != "replay":
        raise _fail(scenario_id, "only replay scenarios are enabled in this catalog")
    if entry.get("severity") not in {"HEALTH_WARN", "HEALTH_ERR"}:
        raise _fail(scenario_id, "invalid health severity")
    if not isinstance(entry.get("message"), str) or not entry["message"].strip():
        raise _fail(scenario_id, "message is required")
    code, prefix = _scenario_code(entry)
    log_excerpt, metrics = _replay_fields(entry)
    markers, actions, timeout = _expect_fields(entry)
    return Scenario(
        id=scenario_id, ceph_code=code, ceph_code_prefix=prefix, severity=entry["severity"],
        message=entry["message"], log_excerpt=log_excerpt, metrics=metrics,
        diagnosis_markers=markers, acceptable_action_ids=actions, detection_timeout_seconds=timeout,
        verified_real_incident=_verified_provenance(entry),
    )


def load_scenario_catalog(path: Path | None = None) -> dict[str, Scenario]:
    try:
        document = yaml.safe_load((path or CATALOG_PATH).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SyntheticInjectionError(f"Failure Lab scenario catalog cannot be loaded: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise SyntheticInjectionError("Failure Lab scenario catalog schema_version must be 1")
    entries = document.get("scenarios")
    if not isinstance(entries, list) or not entries:
        raise SyntheticInjectionError("Failure Lab scenario catalog must contain scenarios")
    loaded: dict[str, Scenario] = {}
    for entry in entries:
        scenario = _scenario(entry, loaded)
        loaded[scenario.id] = scenario
    return loaded


@functools.cache
def scenarios() -> dict[str, Scenario]:
    """The catalog, read on first use.

    Watcher and Worker import this module for is_synthetic_evidence(); reading
    the YAML at import time would let one bad catalog edit stop them both.
    """
    return load_scenario_catalog()


def score_replay(scenario_id: object, observed: object) -> dict:
    if not isinstance(scenario_id, str):
        raise SyntheticInjectionError("Scenario id must be a string")
    scenario = scenarios().get(scenario_id)
    if scenario is None:
        raise SyntheticInjectionError("Unknown synthetic scenario")
    if not isinstance(observed, dict):
        raise SyntheticInjectionError("Replay outcome must be a mapping")
    observed_code = observed.get("detected_ceph_code")
    if not isinstance(observed_code, str):
        observed_code = ""
    code_matches = (
        observed_code.startswith(scenario.ceph_code_prefix)
        if scenario.ceph_code_prefix else observed_code == scenario.ceph_code
    )
    detection_seconds = observed.get("detection_seconds")
    detected = (
        code_matches and isinstance(detection_seconds, (int, float))
        and not isinstance(detection_seconds, bool)
        and 0 <= detection_seconds <= scenario.detection_timeout_seconds
    )
    diagnosis_text = observed.get("diagnosis_text")
    diagnosis = diagnosis_text.casefold() if isinstance(diagnosis_text, str) else ""
    diagnosis_correct = all(
        any(spelling.casefold() in diagnosis for spelling in group) for group in scenario.diagnosis_markers
    )
    action_id = observed.get("action_id")
    proposal_correct = action_id in scenario.acceptable_action_ids
    stages = {
        "detection": bool(detected),
        "diagnosis": bool(diagnosis_correct),
        "proposal": bool(proposal_correct),
        # Replay changes nothing on the cluster: there is nothing to recover
        # or clean up, so these stages only apply to fault-mode runs (FL2).
        "recovery": None,
        "side_effects": (
            isinstance(observed.get("unexpected_health_codes", []), list)
            and all(isinstance(code, str) for code in observed.get("unexpected_health_codes", []))
            and observed.get("unexpected_health_codes", []) == []
        ),
        "cleanup": None,
    }
    return {
        "kind": "replay",
        "scenario_id": scenario.id,
        "stages": stages,
        "passed": all(result for result in stages.values() if result is not None),
        # Scenario YAML is not an independent approval authority. Keep this
        # false until a separately controlled approval registry is implemented.
        "golden_set_eligible": False,
        "golden_set_block_reason": "independent_golden_set_approval_not_configured",
        "fixture_source": "anonymized_incident" if scenario.verified_real_incident else "synthetic_fixture",
    }


def score_replay_report(document: object) -> dict:
    """Score recorded replay outcomes into an offline campaign report."""
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise SyntheticInjectionError("Replay report input schema_version must be 1")
    campaign_id = document.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        raise SyntheticInjectionError("Replay report input requires campaign_id")
    runs = document.get("runs")
    if not isinstance(runs, list) or not runs:
        raise SyntheticInjectionError("Replay report input requires a non-empty runs list")

    results = []
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            raise SyntheticInjectionError(f"Replay run {index} must be a mapping")
        kind = run.get("kind", "replay")
        if kind == "control":
            score = score_replay_control(run.get("observed"))
        elif kind == "replay":
            score = score_replay(run.get("scenario_id"), run.get("observed"))
        else:
            raise SyntheticInjectionError(f"Replay run {index} has unsupported kind")
        results.append({"run_id": run.get("run_id"), **score})
    passed = sum(result["passed"] for result in results)
    controls = [result for result in results if result["kind"] == "control"]
    return {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "run_count": len(results),
        "passed_count": passed,
        "failed_count": len(results) - passed,
        "control_run_count": len(controls),
        "control_false_positive_count": sum(result["false_positive"] for result in controls),
        "golden_set_eligible_count": sum(result["golden_set_eligible"] for result in results),
        "results": results,
    }


def score_replay_control(observed: object) -> dict:
    """Score a no-fault control; any fabricated incident/action is a false positive."""
    if not isinstance(observed, dict):
        raise SyntheticInjectionError("Control outcome must be a mapping")
    detected_code = observed.get("detected_ceph_code")
    action_id = observed.get("action_id")
    unexpected_codes = observed.get("unexpected_health_codes", [])
    stages = {
        "no_incident": observed.get("incident_created") is False,
        "no_detected_fault": detected_code is None or (isinstance(detected_code, str) and not detected_code.strip()),
        "no_action": action_id is None or (isinstance(action_id, str) and not action_id.strip()),
        "no_unexpected_health_codes": (
            isinstance(unexpected_codes, list)
            and all(isinstance(code, str) for code in unexpected_codes)
            and not unexpected_codes
        ),
    }
    false_positive = not all(stages.values())
    return {
        "kind": "control",
        "scenario_id": None,
        "stages": stages,
        "passed": not false_positive,
        "false_positive": false_positive,
        "golden_set_eligible": False,
    }


def is_synthetic_evidence(raw: str | None) -> bool:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and value.get(SYNTHETIC_EVIDENCE_KEY) is True


def _cluster_nodes(cluster: Cluster) -> list[str]:
    return [node.strip() for node in (cluster.ceph_mon_nodes or "").split(",") if node.strip()]


def create(session, *, cluster: Cluster, scenario_id: str, actor: str) -> tuple[Incident, dict]:
    """Create one un-published synthetic Incident and its safe envelope.

    Only a cluster explicitly placed in the ``lab`` commissioning environment
    is accepted.  The returned envelope is marked shadow-only; callers may
    publish it to RabbitMQ, but no command is ever allowed to run for it.
    """
    scenario = scenarios().get(scenario_id)
    if scenario is None:
        raise SyntheticInjectionError("Unknown synthetic scenario")
    if not cluster.is_active:
        raise SyntheticInjectionError("Cluster is not active")
    if cluster.autonomy_environment != "lab":
        raise SyntheticInjectionError("Synthetic injection chỉ được phép trên cluster có environment=lab")
    nodes = _cluster_nodes(cluster)
    if not nodes:
        raise SyntheticInjectionError("Cluster chưa có MON node để dựng evidence")

    run_id = str(uuid.uuid4())
    detected_at = utc_now()
    snapshot = {
        "status": "HEALTH_WARN",
        "checks": {
            scenario.ceph_code: {
                "severity": scenario.severity,
                "detail": [{"message": scenario.message}],
            },
        },
        "failure_lab_metrics": scenario.metrics,
        SYNTHETIC_EVIDENCE_KEY: True,
        "scenario": scenario.id,
        "run_id": run_id,
        "mode": SYNTHETIC_MODE,
    }
    evidence = {
        SYNTHETIC_EVIDENCE_KEY: True,
        "scenario": scenario.id,
        "run_id": run_id,
        "mode": SYNTHETIC_MODE,
        "created_by": actor,
    }
    incident = Incident(
        cluster_id=cluster.id,
        ceph_code=scenario.ceph_code,
        status=IncidentStatus.NEW.value,
        severity=scenario.severity,
        log_excerpt=scenario.log_excerpt,
        signal_evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        detected_at=detected_at,
    )
    session.add(incident)
    session.flush()
    envelope = publisher.build_envelope(
        incident_id=incident.id,
        ceph_code=scenario.ceph_code,
        detected_at=detected_at.isoformat(),
        nodes=nodes,
        log_excerpt=scenario.log_excerpt,
        cluster_snapshot=snapshot,
        cluster_id=cluster.id,
        ssh_user=cluster.ssh_user or "",
        ssh_key_path=cluster.ssh_key_path or "",
        ceph_exec_mode=cluster.ceph_exec_mode or "",
        ceph_container_name=cluster.ceph_container_name or "",
    )
    envelope[SYNTHETIC_EVIDENCE_KEY] = True
    envelope["synthetic_scenario"] = scenario.id
    envelope["synthetic_run_id"] = run_id
    envelope["synthetic_mode"] = SYNTHETIC_MODE
    envelope["failure_lab_provenance"] = (
        "anonymized_incident" if scenario.verified_real_incident else "synthetic_fixture"
    )
    return incident, envelope


def cleanup(session, *, cluster_id: str, run_id: str | None = None) -> int:
    """Close only synthetic rows for a cluster, never real incidents."""
    rows = session.query(Incident).filter(Incident.cluster_id == cluster_id).all()
    changed = 0
    for incident in rows:
        try:
            evidence = json.loads(incident.signal_evidence_json or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(evidence, dict) or evidence.get(SYNTHETIC_EVIDENCE_KEY) is not True:
            continue
        if run_id is not None and evidence.get("run_id") != run_id:
            continue
        if incident.status in {
            IncidentStatus.NEW.value, IncidentStatus.DIAGNOSING.value,
            IncidentStatus.PENDING_APPROVAL.value, IncidentStatus.APPROVED.value,
            IncidentStatus.EXECUTING.value, IncidentStatus.VERIFYING.value,
            IncidentStatus.FAILED.value,
        }:
            incident.status = IncidentStatus.REJECTED.value
            cancel_pending_actions(session, incident.id)
            changed += 1
    return changed
