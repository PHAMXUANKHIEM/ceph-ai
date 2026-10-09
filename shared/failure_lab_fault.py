"""Failure Lab fault runs: reversible real faults on the lab cluster (FL2).

Plan/in-progress/failure-lab-plan-2026-10-06.md, sections 3 and FL2. A run

1. refuses unless every gate holds: FAILURE_LAB_FAULT_ENABLED, no HALT file,
   the cluster is ``autonomy_environment=lab``, its live fsid equals
   FAILURE_LAB_CLUSTER_FSID, a scheduled run is inside FAILURE_LAB_WINDOW,
   no real incident of the same kind is open, the expected health codes are
   not already raised, and no other run holds the lock;
2. records its time window: the Worker holds SAFE actions of incidents
   detected inside it for approval and sends their alerts to the lab chat
   (``holding_run``);
3. injects one fault of a kind known to this module (catalog
   worker/policy/failure_lab_faults.yaml; no free-form commands);
4. watches the real Watcher/Worker detect and diagnose it;
5. ALWAYS undoes the fault, then waits until the cluster is back to its
   pre-run health. If it is not, or the run was interrupted, it writes the
   HALT file: no further run starts until an operator removes it.
"""

from __future__ import annotations

import fcntl
import functools
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import yaml  # type: ignore[import-untyped]
from sqlalchemy import or_

from config.settings import settings
from shared import audit, incident_events
from shared.failure_lab import _latest_action, diagnosis_ready, open_real_incident_codes
from shared.models import Cluster, Incident
from shared.synthetic_incidents import Scenario, is_synthetic_evidence, scenarios
from shared.time import utc_now

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).resolve().parents[1] / "worker" / "policy" / "failure_lab_faults.yaml"
STATE_DIR = Path("/var/lib/ceph-ai/failure-lab")
RUNS_FILE, LOCK_FILE, HALT_FILE = "fault-runs.json", "fault.lock", "HALT"
# Incidents detected up to this long after a run ended still belong to it
# (the Watcher polls, the Worker queues).
HOLD_GRACE = timedelta(minutes=15)
KEEP_RUNS = 50
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_OSD_MARKER = re.compile(r"osd\.\d+")


class FaultRefused(RuntimeError):
    """A gate did not hold; nothing was changed on the cluster."""


@dataclass(frozen=True)
class FaultScenario:
    id: str
    kind: str
    expect_from: str
    expected_health_codes: frozenset[str]
    max_seconds: int
    recovery_seconds: int
    detection_timeout_seconds: int


class CephLab(Protocol):
    def read(self, command: str) -> Any: ...

    def write(self, command: str) -> str: ...


@dataclass
class Injected:
    """One prepared fault: what it touches, how to apply, undo and verify it."""

    target: str
    apply: Callable[[], object]
    undo: Callable[[], object]
    restored: Callable[[], bool]


# --- catalog ------------------------------------------------------------------------

def _scenario(raw: object) -> FaultScenario:
    if not isinstance(raw, dict) or raw.get("kind") not in PREPARE:
        raise FaultRefused(f"fault catalog: unknown kind in {raw!r}")
    scenario = FaultScenario(
        id=str(raw["id"]), kind=str(raw["kind"]), expect_from=str(raw["expect_from"]),
        expected_health_codes=frozenset(str(code) for code in raw.get("expected_health_codes") or []),
        max_seconds=int(raw.get("max_seconds", 300)), recovery_seconds=int(raw.get("recovery_seconds", 600)),
        detection_timeout_seconds=int(raw.get("detection_timeout_seconds", 180)),
    )
    if not 60 <= scenario.max_seconds <= 540 or not 60 <= scenario.recovery_seconds <= 1800:
        raise FaultRefused(f"fault catalog: {scenario.id} time limits are out of range")
    return scenario


@functools.cache
def fault_scenarios() -> dict[str, FaultScenario]:
    document = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise FaultRefused("fault catalog: schema_version must be 1")
    return {item.id: item for item in (_scenario(raw) for raw in document.get("faults") or [])}


# --- cluster access -------------------------------------------------------------------

class SshCephLab:
    """Ceph CLI on the lab cluster's first MON with the Worker's own identity
    (the mutation key for the default cluster), the same way remediation runs."""

    def __init__(self, cluster: Cluster):
        from shared.cluster_nodes import resolve_ssh_creds

        nodes = settings.ceph_mon_nodes if cluster.is_default else cluster.ceph_mon_nodes
        self.mon_nodes = [node.strip() for node in str(nodes or "").split(",") if node.strip()]
        self.user, self.key_path, self.exec_mode, self.container = resolve_ssh_creds(
            None if cluster.is_default else cluster)

    def read(self, command: str) -> Any:
        from watcher.ceph_client import run_ceph_json_command_with

        return run_ceph_json_command_with(
            self.mon_nodes, self.container, self.user, self.key_path, self.exec_mode, command)[1]

    def write(self, command: str) -> str:
        from watcher.ceph_client import build_exec_command
        from worker.executor.ssh_executor import execute_command

        if not self.mon_nodes:
            raise FaultRefused("the lab cluster has no MON node configured")
        return execute_command(self.mon_nodes[0], build_exec_command(self.exec_mode, self.container, command),
                               user=self.user, key_path=self.key_path)


def health_codes(lab: CephLab) -> set[str]:
    return set((lab.read("ceph health").get("checks") or {}).keys())


def _osds(lab: CephLab) -> list[dict]:
    return list(lab.read("ceph osd dump").get("osds") or [])


# --- faults ------------------------------------------------------------------------

def _prepare_stop_osd(lab: CephLab, scenario: FaultScenario) -> Injected:
    interval = int(str(lab.read("ceph config get mon mon_osd_down_out_interval")).strip('"'))
    if scenario.max_seconds + 120 > interval:
        # Past this interval Ceph marks the OSD out and starts moving data.
        raise FaultRefused(f"mon_osd_down_out_interval={interval}s is too short for a {scenario.max_seconds}s run")
    live = [osd for osd in _osds(lab) if osd.get("up") == 1 and osd.get("in") == 1]
    if len(live) < 3:
        raise FaultRefused("stopping an OSD needs at least 3 OSDs up and in")
    osd_id = max(int(osd["osd"]) for osd in live)
    try:
        lab.write(f"ceph osd ok-to-stop {osd_id}")  # fails when a PG would go inactive
    except Exception as exc:  # noqa: BLE001 - any refusal means: do not stop it
        raise FaultRefused(f"ceph osd ok-to-stop {osd_id} refused: {exc}") from exc
    return Injected(
        target=f"osd.{osd_id}",
        apply=lambda: lab.write(f"ceph orch daemon stop osd.{osd_id}"),
        undo=lambda: lab.write(f"ceph orch daemon start osd.{osd_id}"),
        restored=lambda: any(int(osd["osd"]) == osd_id and osd.get("up") == 1 for osd in _osds(lab)),
    )


def _prepare_nearfull_ratio(lab: CephLab, scenario: FaultScenario) -> Injected:
    original = float(lab.read("ceph osd dump")["nearfull_ratio"])
    nodes = lab.read("ceph osd df").get("nodes") or []
    if not nodes:
        raise FaultRefused("ceph osd df returned no OSD")
    fullest = max(nodes, key=lambda node: float(node.get("utilization") or 0))
    # Just under the fullest OSD's use: it (and only OSDs as full) turn nearfull.
    target = round(float(fullest.get("utilization") or 0) / 100 - 0.01, 3)
    if target < 0.02 or target >= original:
        raise FaultRefused(f"cannot pick a nearfull ratio below {original:g} for use {fullest.get('utilization')}%")
    return Injected(
        target=f"osd.{fullest['id']}",
        apply=lambda: lab.write(f"ceph osd set-nearfull-ratio {target:g}"),
        undo=lambda: lab.write(f"ceph osd set-nearfull-ratio {original:g}"),
        restored=lambda: abs(float(lab.read("ceph osd dump")["nearfull_ratio"]) - original) < 1e-6,
    )


PREPARE: dict[str, Callable[[CephLab, FaultScenario], Injected]] = {
    "stop_osd": _prepare_stop_osd,
    "nearfull_ratio": _prepare_nearfull_ratio,
}


# --- run windows, lock, halt ------------------------------------------------------------

def _read_runs(state_dir: Path) -> list[dict]:
    try:
        value = json.loads((state_dir / RUNS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _save_run(state_dir: Path, record: dict) -> None:
    runs = [run for run in _read_runs(state_dir) if run.get("run_id") != record["run_id"]] + [record]
    path = state_dir / RUNS_FILE
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(runs[-KEEP_RUNS:], ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)


def _covers(run: dict, cluster_id: str | None, detected_at: datetime) -> bool:
    if run.get("cluster_id") != cluster_id and not (cluster_id is None and run.get("is_default")):
        return False
    try:
        started = datetime.fromisoformat(run["started_at"])
        until = datetime.fromisoformat(run.get("ended_at") or run["deadline"])
    except (KeyError, TypeError, ValueError):
        return False
    return started <= detected_at <= until + HOLD_GRACE


def holding_run(cluster_id: str | None, detected_at: datetime | None, *, state_dir: Path = STATE_DIR) -> dict | None:
    """The fault run whose window covers an incident detected at ``detected_at``
    on ``cluster_id`` (None = a legacy row of the default cluster), if any."""
    if detected_at is None:
        return None
    return next((run for run in reversed(_read_runs(state_dir)) if _covers(run, cluster_id, detected_at)), None)


@contextmanager
def _exclusive(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / LOCK_FILE, "a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FaultRefused("another fault run is in progress") from exc
        yield


def _halt(state_dir: Path, reason: str) -> None:
    (state_dir / HALT_FILE).write_text(f"{utc_now().isoformat()}Z {reason}\n", encoding="utf-8")
    notify_lab(f"⛔ Failure Lab DỪNG: {reason}. Kiểm tra cụm lab, rồi xoá {state_dir / HALT_FILE} để chạy lại.")


def in_window(now: datetime, window: str) -> bool:
    start, end = window.split("-")
    local = now.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).strftime("%H:%M")
    return start <= local < end if start <= end else (local >= start or local < end)


def notify_lab(text: str) -> None:
    chat_id, token = settings.failure_lab_telegram_chat_id.strip(), settings.telegram_incident_bot_token
    if not chat_id or not token:
        logger.info("failure lab (no lab chat configured): %s", text)
        return
    from shared.telegram_client import send_telegram_message

    try:
        send_telegram_message(token, chat_id, "[FAILURE LAB] " + text)
    except Exception:  # noqa: BLE001 - a lost notice must not stop cleanup
        logger.exception("failure lab: Telegram notice failed")


# --- gates --------------------------------------------------------------------------

def _gated_cluster(session, cluster_id: str, *, scheduled: bool, state_dir: Path, now: datetime) -> Cluster:
    if not settings.failure_lab_fault_enabled:
        raise FaultRefused("FAILURE_LAB_FAULT_ENABLED=false")
    if (state_dir / HALT_FILE).exists():
        raise FaultRefused(f"halted: {(state_dir / HALT_FILE).read_text(encoding='utf-8').strip()}")
    if not settings.failure_lab_cluster_fsid.strip():
        raise FaultRefused("FAILURE_LAB_CLUSTER_FSID is not set")
    if scheduled and not in_window(now, settings.failure_lab_window):
        raise FaultRefused(f"outside FAILURE_LAB_WINDOW {settings.failure_lab_window}")
    cluster = session.get(Cluster, cluster_id)
    if cluster is None or cluster.autonomy_environment != "lab":
        raise FaultRefused("the cluster is not marked autonomy_environment=lab")
    return cluster


def _check_cluster_state(lab: CephLab, scenario: FaultScenario, replay: Scenario, busy: set[str]) -> set[str]:
    live = str(lab.read("ceph fsid").get("fsid", ""))
    if live != settings.failure_lab_cluster_fsid.strip():
        raise FaultRefused(f"live fsid {live or '?'} is not the pinned lab fsid")
    if replay.ceph_code in busy:
        raise FaultRefused(f"a real {replay.ceph_code} incident is already open")
    baseline = health_codes(lab)
    if baseline & scenario.expected_health_codes:
        raise FaultRefused(f"already raised: {', '.join(sorted(baseline & scenario.expected_health_codes))}")
    return baseline


# --- observe, undo, recover --------------------------------------------------------------

def _find_incident(session, run: dict, prefix: str) -> Incident | None:
    scope = Incident.cluster_id == run["cluster_id"]
    if run["is_default"]:
        scope = or_(scope, Incident.cluster_id.is_(None))
    rows = (session.query(Incident)
            .filter(scope, Incident.created_at >= datetime.fromisoformat(run["started_at"]),
                    Incident.ceph_code.like(prefix + "%"))
            .order_by(Incident.created_at).all())
    return next((row for row in rows if not is_synthetic_evidence(row.signal_evidence_json)), None)


@dataclass
class _Clock:
    sleep: Callable[[float], None]
    now: Callable[[], float]
    poll_seconds: float


def _observe(session_factory, lab: CephLab, run: dict, prefix: str, seconds: float, clock: _Clock) -> dict:
    """Wait for the real incident and its diagnosis; collect health codes seen."""
    started = clock.now()
    observed: dict[str, Any] = {"seen_codes": set(), "incident_id": None}
    while clock.now() - started < seconds:
        try:
            observed["seen_codes"] |= health_codes(lab)
        except Exception:  # noqa: BLE001 - a missed poll is not a reason to stop early
            logger.warning("failure lab: health poll failed", exc_info=True)
        with session_factory() as session:
            incident = _find_incident(session, run, prefix)
            if incident is not None and observed["incident_id"] is None:
                observed.update(incident_id=incident.id, detection_seconds=round(clock.now() - started))
            if incident is not None and diagnosis_ready(incident):
                action = _latest_action(session, incident.id)
                observed.update(detected_ceph_code=incident.ceph_code, diagnosis_text=incident.diagnosis_text or "",
                                action_id=action.action_id if action is not None else None)
                return observed
        clock.sleep(clock.poll_seconds)
    return observed


def _undo(injected: Injected, clock: _Clock, attempts: int = 3) -> bool:
    for attempt in range(1, attempts + 1):
        try:
            injected.undo()
            return True
        except Exception:  # noqa: BLE001 - retried, then reported by the halt
            logger.exception("failure lab: undo attempt %s for %s failed", attempt, injected.target)
            clock.sleep(clock.poll_seconds)
    return False


def _recover(lab: CephLab, injected: Injected, baseline: set[str], seconds: float, clock: _Clock) -> tuple[bool, set[str]]:
    deadline = clock.now() + seconds
    codes: set[str] = set()
    while True:
        try:
            restored, codes = injected.restored(), health_codes(lab)
        except Exception:  # noqa: BLE001 - keep polling until the deadline
            restored = False
        if restored and codes <= baseline:
            return True, codes
        if clock.now() >= deadline:
            return False, codes
        clock.sleep(clock.poll_seconds)


# --- scoring ------------------------------------------------------------------------

def score_fault(scenario: FaultScenario, replay: Scenario, target: str, observed: dict, *,
                baseline: set[str], undone: bool, recovered: bool) -> dict:
    """Same stages as a replay; the OSD named in the replay markers becomes the
    OSD this run really touched."""
    diagnosis = str(observed.get("diagnosis_text") or "").casefold()
    markers = [[target if _OSD_MARKER.fullmatch(item) else item for item in group] for group in replay.diagnosis_markers]
    code = str(observed.get("detected_ceph_code") or "")
    prefix = replay.ceph_code_prefix or replay.ceph_code
    unexpected = sorted(set(observed.get("seen_codes") or ()) - baseline - scenario.expected_health_codes)
    detection_seconds = observed.get("detection_seconds")
    stages = {
        "detection": code.startswith(prefix) and detection_seconds is not None
        and detection_seconds <= scenario.detection_timeout_seconds,
        "diagnosis": all(any(item.casefold() in diagnosis for item in group) for group in markers),
        "proposal": observed.get("action_id") in replay.acceptable_action_ids,
        "recovery": recovered,
        "side_effects": not unexpected,
        "cleanup": undone,
    }
    return {"kind": "fault", "scenario_id": scenario.id, "target": target, "stages": stages,
            "passed": all(stages.values()), "unexpected_health_codes": unexpected}


# FL3: the answer of a real-fault run, attached to the incident it raised.
# The cause is known because the lab caused it; case_references shows it to
# later diagnoses of the same fault family on any cluster (context only).
LABEL_EVENT = "failure_lab_label"
_CAUSE = {
    "stop_osd": "Failure Lab đã chủ động dừng daemon {target}: OSD down có chủ đích, không phải lỗi phần cứng.",
    "nearfull_ratio": "Failure Lab đã chủ động hạ ngưỡng nearfull của cụm xuống dưới mức dùng của {target}.",
}


def label_evidence(scenario: FaultScenario, replay: Scenario, result: dict) -> dict:
    return {
        "run_id": result["run_id"], "scenario_id": scenario.id, "kind": scenario.kind, "target": result["target"],
        "cause": _CAUSE.get(scenario.kind, "Failure Lab đã chủ động gây lỗi {kind} trên {target}.").format(
            target=result["target"], kind=scenario.kind),
        "expected_health_codes": sorted(scenario.expected_health_codes),
        "acceptable_action_ids": list(replay.acceptable_action_ids),
        "stages": dict(result["stages"]), "passed": bool(result["passed"]),
    }


def _record_label(session_factory, scenario: FaultScenario, replay: Scenario, result: dict) -> None:
    if not result.get("incident_id"):
        return
    with session_factory() as session:
        incident_events.record(session, incident_id=result["incident_id"], event_type=LABEL_EVENT,
                               actor="failure-lab", evidence=label_evidence(scenario, replay, result))
        session.commit()


def _audit(session_factory, incident_id: str | None, result: dict) -> None:
    if not incident_id:
        return
    with session_factory() as session:
        audit.record(session, incident_id=incident_id, action_id=None, event_type=audit.EVENT_FAILURE_LAB_FAULT_RUN,
                     actor="failure-lab", evidence={key: result[key] for key in ("run_id", "scenario_id", "target",
                                                                                 "passed", "stages")})
        session.commit()


# --- run ----------------------------------------------------------------------------

def run_fault(session_factory: Callable[[], Any], *, cluster_id: str, scenario_id: str, scheduled: bool = False,
              lab_factory: Callable[[Cluster], CephLab] = SshCephLab, state_dir: Path = STATE_DIR,
              poll_seconds: float = 10, sleep: Callable[[float], None] = time.sleep,
              monotonic: Callable[[], float] = time.monotonic) -> dict:
    scenario = fault_scenarios().get(scenario_id)
    if scenario is None:
        raise FaultRefused(f"unknown fault scenario {scenario_id}")
    replay = scenarios()[scenario.expect_from]
    clock = _Clock(sleep, monotonic, poll_seconds)
    with _exclusive(state_dir):
        now = utc_now()
        with session_factory() as session:
            cluster = _gated_cluster(session, cluster_id, scheduled=scheduled, state_dir=state_dir, now=now)
            busy, is_default = open_real_incident_codes(session, cluster_id), bool(cluster.is_default)
        lab = lab_factory(cluster)
        baseline = _check_cluster_state(lab, scenario, replay, busy)
        injected = PREPARE[scenario.kind](lab, scenario)
        run = {"run_id": f"fault-{uuid.uuid4().hex[:12]}", "cluster_id": cluster_id, "is_default": is_default,
               "scenario_id": scenario.id, "target": injected.target, "started_at": now.isoformat(),
               "deadline": (now + timedelta(seconds=scenario.max_seconds + scenario.recovery_seconds)).isoformat(),
               "ended_at": None}
        _save_run(state_dir, run)
        notify_lab(f"Bắt đầu {scenario.id} trên {injected.target} (tối đa {scenario.max_seconds}s).")
        observed = _inject_and_observe(session_factory, lab, run, injected, scenario, replay, clock, state_dir)
        recovered, final_codes = _recover(lab, injected, baseline, scenario.recovery_seconds, clock)
        result = {"run_id": run["run_id"], "incident_id": observed.get("incident_id"),
                  "final_health_codes": sorted(final_codes), "baseline_health_codes": sorted(baseline),
                  **score_fault(scenario, replay, injected.target, observed, baseline=baseline,
                                undone=observed["undone"], recovered=recovered)}
    if not (recovered and observed["undone"]):
        _halt(state_dir, f"{scenario.id} on {injected.target}: cụm chưa về trạng thái trước lượt chạy")
    _audit(session_factory, result["incident_id"], result)
    _record_label(session_factory, scenario, replay, result)
    failed = [stage for stage, ok in result["stages"].items() if not ok]
    notify_lab(f"{scenario.id} trên {injected.target}: " + ("ĐẠT" if result["passed"] else "TRƯỢT " + ", ".join(failed)))
    return result


def _inject_and_observe(session_factory, lab: CephLab, run: dict, injected: Injected, scenario: FaultScenario,
                        replay: Scenario, clock: _Clock, state_dir: Path) -> dict:
    """Apply, watch, and undo in a finally: an error or Ctrl-C/SIGTERM still
    undoes the fault (and halts further runs, since recovery was not checked)."""
    observed: dict[str, Any] = {"seen_codes": set()}
    completed = False
    try:
        injected.apply()
        observed = _observe(session_factory, lab, run, replay.ceph_code_prefix or replay.ceph_code,
                            scenario.max_seconds, clock)
        completed = True
    finally:
        observed["undone"] = _undo(injected, clock)
        run["ended_at"] = utc_now().isoformat()
        _save_run(state_dir, run)
        if not completed:
            _halt(state_dir, f"{scenario.id} on {injected.target} was interrupted; the fault was "
                             + ("undone" if observed["undone"] else "NOT undone"))
    return observed


def report_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=sorted)
