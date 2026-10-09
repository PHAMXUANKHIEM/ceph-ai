"""FL4/FL6: the nightly Failure Lab campaign on the staging cluster.

``ceph-ai-failure-lab.timer`` starts this every 15 minutes; it does nothing
unless Settings > "Cụm Staging (Failure Lab)" has a cluster, a pinned fsid
and fault injection switched on, and the time is inside that window
(02:00-05:00 by default, Vietnam time). One campaign per night:

1. operator-approved AI reproductions (FL6.3/FL6.4) first, oldest first;
2. then the reviewed catalog faults in rotation, least recently run first;

at most ``MAX_RUNS_PER_NIGHT`` runs, each through every FL2 gate
(``scheduled=True``). The campaign stops at the first refusal, at a HALT
(the cluster did not come back to its pre-run health), or when the window
ends; a run whose AI diagnosis or proposal was wrong is learning data, not a
reason to stop. The lab chat gets one summary per night.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared import failure_lab_config
from shared import failure_lab_fault as fault
from shared.time import utc_now

logger = logging.getLogger(__name__)

CAMPAIGN_FILE = "campaign.json"
MAX_RUNS_PER_NIGHT = 3


def night_of(now: datetime, window: str) -> str:
    """The local date the window started on (a window may cross midnight)."""
    local = now.replace(tzinfo=timezone.utc).astimezone(fault.LOCAL_TZ)
    start = window.split("-")[0]
    return (local.date() - timedelta(days=1) if local.strftime("%H:%M") < start else local.date()).isoformat()


def _load(state_dir: Path) -> dict:
    try:
        value = json.loads((state_dir / CAMPAIGN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    temporary = state_dir / (CAMPAIGN_FILE + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(state_dir / CAMPAIGN_FILE)


def _next_job(state: dict, proposals_dir: Path | None) -> tuple[str, str]:
    """("proposal", id) for the oldest approved AI reproduction, else ("fault", least recently run)."""
    from shared import reproduction_approval as approvals

    tried = {run["job"] for run in state["runs"]}
    proposal_id = approvals.next_approved(proposals_dir)
    if proposal_id and f"proposal:{proposal_id}" not in tried:
        return "proposal", proposal_id
    last = state.get("last_by_scenario") or {}
    candidates = sorted((str(last.get(scenario_id, "")), scenario_id) for scenario_id in fault.fault_scenarios()
                        if f"fault:{scenario_id}" not in tried)
    if not candidates:
        raise fault.FaultRefused("every catalog fault already ran tonight")
    return "fault", candidates[0][1]


def _gate(now: datetime, state_dir: Path) -> tuple[failure_lab_config.LabConfig | None, str]:
    config = failure_lab_config.load()
    if not (config.fault_enabled and config.cluster_id and config.fsid):
        return None, "off"
    if not fault.in_window(now, config.window):
        return None, "outside_window"
    if (state_dir / fault.HALT_FILE).exists():
        return None, "halted"
    return config, "ok"


def run_campaign(session_factory: Callable[[], Any], *, state_dir: Path = fault.STATE_DIR,
                 proposals_dir: Path | None = None, clock: Callable[[], datetime] = utc_now,
                 run_fault: Callable[..., dict] = fault.run_fault,
                 run_proposal: Callable[..., dict] = fault.run_proposal,
                 notify: Callable[[str], None] = fault.notify_lab,
                 max_runs: int = MAX_RUNS_PER_NIGHT) -> dict:
    """Run tonight's campaign if it is due; returns {"status": ..., "runs": [...]}."""
    now = clock()
    config, status = _gate(now, state_dir)
    if config is None:
        return {"status": status, "runs": []}
    night = night_of(now, config.window)
    state = _load(state_dir)
    if state.get("night") == night and state.get("finished"):
        return {"status": "done_tonight", "runs": state.get("runs", [])}
    if state.get("night") != night:
        state = {"night": night, "runs": [], "finished": False,
                 "last_by_scenario": state.get("last_by_scenario") or {}}
    stop = _run_jobs(session_factory, config, state, state_dir=state_dir, proposals_dir=proposals_dir, clock=clock,
                     run_fault=run_fault, run_proposal=run_proposal, max_runs=max_runs)
    state.update(finished=True, stopped_because=stop)
    _save(state_dir, state)
    notify(summary_text(state))
    return {"status": "ran", "runs": state["runs"], "stopped_because": stop}


def _run_jobs(session_factory, config: failure_lab_config.LabConfig, state: dict, *, state_dir: Path,
              proposals_dir: Path | None, clock: Callable[[], datetime], run_fault: Callable[..., dict],
              run_proposal: Callable[..., dict], max_runs: int) -> str:
    """Run jobs until the nightly limit, the window's end, a refusal or a HALT; returns why it stopped."""
    while len(state["runs"]) < max_runs:
        if not fault.in_window(clock(), config.window):
            return "hết khung giờ"
        try:
            kind, job_id = _next_job(state, proposals_dir)
        except fault.FaultRefused as exc:
            return str(exc)
        entry: dict[str, Any] = {"job": f"{kind}:{job_id}", "started_at": clock().isoformat()}
        try:
            if kind == "proposal":
                result = run_proposal(session_factory, cluster_id=config.cluster_id, proposal_id=job_id,
                                      proposals_dir=proposals_dir, scheduled=True, state_dir=state_dir)
            else:
                result = run_fault(session_factory, cluster_id=config.cluster_id, scenario_id=job_id,
                                   scheduled=True, state_dir=state_dir)
                state["last_by_scenario"][job_id] = entry["started_at"]
        except fault.FaultRefused as exc:
            state["runs"].append({**entry, "refused": str(exc)})
            _save(state_dir, state)
            return f"bị từ chối: {exc}"
        except Exception as exc:  # noqa: BLE001 - recorded and reported; the runner already undid the fault
            logger.exception("failure lab campaign: %s failed", entry["job"])
            state["runs"].append({**entry, "error": f"{type(exc).__name__}: {exc}"[:300]})
            _save(state_dir, state)
            return "lỗi khi chạy"
        state["runs"].append({**entry, "passed": bool(result.get("passed")), "target": result.get("target"),
                              "failed_stages": [name for name, ok in (result.get("stages") or {}).items() if not ok]})
        _save(state_dir, state)
        if (state_dir / fault.HALT_FILE).exists():
            return "HALT: cụm chưa về trạng thái trước lượt chạy"
    return f"đủ {max_runs} lượt"


def _run_line(run: dict) -> str:
    if "refused" in run:
        return f"• {run['job']}: bị từ chối — {run['refused']}"
    if "error" in run:
        return f"• {run['job']}: lỗi — {run['error']}"
    verdict = "ĐẠT" if run["passed"] else "TRƯỢT " + ", ".join(run["failed_stages"])
    return f"• {run['job']} trên {run.get('target') or '?'}: {verdict}"


def summary_text(state: dict) -> str:
    runs = state.get("runs") or []
    lines = [f"🌙 Chiến dịch Failure Lab đêm {state.get('night')}: {len(runs)} lượt"
             f" ({sum(1 for run in runs if run.get('passed'))} đạt)."]
    lines += [_run_line(run) for run in runs] or ["• không có lượt nào"]
    lines.append(f"Dừng vì: {state.get('stopped_because')}.")
    return "\n".join(lines)
