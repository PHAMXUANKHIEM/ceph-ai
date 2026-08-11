"""Volumes page "Đo hiệu năng tối đa" (load sweep) — dashboard/routes/
volumes.py's propose route creates the Action, worker/llm/router_client.py
dispatches its execution here (same "own multi-step orchestrator, not the
generic per-host command loop" pattern worker/executor/cluster_deploy.py
uses for the cluster-lifecycle action family).

Why this exists: VolumeMetric's own "peak" (dashboard/routes/volumes.py's
history API) is only the highest sample a real workload happened to
produce — a close-but-wrong proxy for "this volume's maximum performance"
that says more about the workload than about the volume. The real answer
requires actively sweeping load (fio, increasing iodepth) and finding the
saturation KNEE — the point where pushing further trades a small IOPS gain
for a disproportionate latency spike (an operator's own description,
verified against standard storage-benchmarking practice: this is the
textbook "IOPS vs. latency knee" method, not something invented for this
codebase).

Scoped to a dedicated SCRATCH image only, never the operator's real
volume — confirmed via AskUserQuestion when this feature was requested,
specifically so a sweep never contends with real traffic on real data. A
consequence worth being explicit about: the measured knee reflects the
POOL's/cluster's available capacity at sweep time, not literally "this
one volume's own ceiling" — those are the same number only when nothing
else is contending for I/O, which is the common case this exists for
(deciding "do we have headroom left / did we hit a wall") but not a
per-volume guarantee.
"""

import json
import logging
import shlex
import uuid
from datetime import datetime

from shared import db
from shared.models import VolumePerfSweep
from worker.executor.ssh_executor import ExecutorError, execute_command
from worker.policy.gate import VALID_VOLUME_PERF_ACTION_IDS

logger = logging.getLogger(__name__)

VOLUME_PERF_ACTION_IDS = VALID_VOLUME_PERF_ACTION_IDS

# Leading underscore (2026-07-29): purely a visual "this is tool-owned, not
# an operator's volume" signal on `rbd ls` output — Ceph itself doesn't
# treat it specially. Reused/created-once-then-kept across sweep runs
# (see _ensure_scratch_image) rather than created-then-deleted every time,
# to avoid repeatedly re-provisioning a 50G image for no benefit (RBD
# images are thin-provisioned — an unused one costs no real capacity).
SCRATCH_IMAGE_NAME = "_ceph_aiops_perf_probe"
SCRATCH_IMAGE_SIZE_GB = 50

# Same iodepth ladder the operator's own proven script used.
IODEPTH_STEPS = (1, 2, 4, 8, 16, 32, 64, 128, 256)
# Shorter than a textbook 60s+10s ramp per step (the operator's own script)
# — this sweep can run up to 9 steps, and every second here is real load on
# a production cluster; 25s/step (with early-stop once the knee is found,
# see run() below) keeps a full unsaturated sweep under ~4 minutes instead
# of ~10.
FIO_RUNTIME_SECONDS = 20
FIO_RAMP_SECONDS = 5

# Knee-detection thresholds (see _detect_knee) — deliberately two signals
# combined, not one: the operator's own example (~3% more IOPS for ~14x
# more latency) is an extreme case; requiring BOTH "IOPS basically
# plateaued" AND "latency grew disproportionately" avoids flagging a knee
# on ordinary noisy variance between two points that are both still
# scaling normally. The absolute cutoff is a safety net independent of
# growth shape, so a sweep doesn't grind on toward iodepth=256 once
# latency is already clearly unacceptable.
_KNEE_IOPS_PLATEAU_THRESHOLD = 0.15
_KNEE_LATENCY_GROWTH_MULTIPLE = 3.0
_KNEE_ABSOLUTE_LATENCY_MS = 20.0

_STEP_DEFS = [
    ("prepare", "Chuẩn bị scratch image + kiểm tra fio", 10),
    ("sweep", "Quét tải tăng dần (iodepth 1→256)", 90),
    ("diagnostics", "Thu thập QoS / bottleneck diagnostics", 100),
]


class VolumePerfError(Exception):
    """Raised by a step that must stop the whole sweep — mirrors
    worker/executor/cluster_deploy.py's own DeployPhaseError; kept as a
    separate class since this module has no phase-list dispatcher to
    share it with."""


def _make_step(key: str, label: str, pct: int) -> dict:
    return {
        "step": key,
        "label": label,
        "pct": pct,
        "status": "pending",
        "hosts": [],
        "message": None,
        "started_at": None,
        "finished_at": None,
    }


def _check_fio_available(ip: str) -> None:
    try:
        output = execute_command(ip, "command -v fio 2>/dev/null")
    except ExecutorError as exc:
        raise VolumePerfError(f"{ip}: không kiểm tra được fio: {exc}") from exc
    if not output.strip():
        raise VolumePerfError(
            f"{ip}: chưa cài fio (cần bản có hỗ trợ ioengine=rbd) — cài thủ công rồi thử lại, "
            "vd `yum install fio` / `apt install fio`."
        )


def _ensure_scratch_image(ip: str, pool: str) -> None:
    spec = shlex.quote(f"{pool}/{SCRATCH_IMAGE_NAME}")
    cmd = f"rbd info {spec} >/dev/null 2>&1 || rbd create --size {SCRATCH_IMAGE_SIZE_GB}G {spec}"
    try:
        execute_command(ip, cmd)
    except ExecutorError as exc:
        raise VolumePerfError(
            f"{ip}: không tạo được scratch image {pool}/{SCRATCH_IMAGE_NAME}: {exc}"
        ) from exc


def _parse_fio_json(output: str, iodepth: int) -> dict:
    # fio's stdout can carry a non-JSON banner line before the JSON blob on
    # some distro builds — take from the first '{' rather than assume the
    # whole output is clean JSON.
    start = output.find("{")
    if start == -1:
        raise VolumePerfError(f"fio không trả JSON hợp lệ ở iodepth={iodepth}: {output[:200]!r}")
    try:
        data = json.loads(output[start:])
        write = data["jobs"][0]["write"]
        iops = float(write["iops"])
        lat_avg_ms = float(write["clat_ns"]["mean"]) / 1_000_000
        lat_p99_ms = float(write["clat_ns"]["percentile"]["99.000000"]) / 1_000_000
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise VolumePerfError(f"fio JSON thiếu trường cần thiết ở iodepth={iodepth}: {exc}") from exc
    return {
        "iodepth": iodepth,
        "iops": round(iops, 1),
        "latency_avg_ms": round(lat_avg_ms, 3),
        "latency_p99_ms": round(lat_p99_ms, 3),
    }


def _run_fio_step(ip: str, pool: str, iodepth: int) -> dict:
    cmd = (
        "fio --name=sweep --ioengine=rbd "
        f"--pool={shlex.quote(pool)} --rbdname={shlex.quote(SCRATCH_IMAGE_NAME)} "
        f"--rw=randwrite --bs=4k --iodepth={iodepth} --numjobs=1 "
        f"--runtime={FIO_RUNTIME_SECONDS} --ramp_time={FIO_RAMP_SECONDS} --time_based --direct=1 "
        "--group_reporting --output-format=json"
    )
    try:
        output = execute_command(ip, cmd)
    except ExecutorError as exc:
        raise VolumePerfError(f"{ip}: fio thất bại ở iodepth={iodepth}: {exc}") from exc
    return _parse_fio_json(output, iodepth)


def _detect_knee(steps: list[dict]) -> dict | None:
    """Returns the LAST step before the cliff (the usable ceiling — "how
    far can this be pushed before it falls off"), or None if the sweep
    never saturated within the tested range (steps[-1] is then a
    lower-bound floor, not a real ceiling)."""
    for i in range(1, len(steps)):
        prev, cur = steps[i - 1], steps[i]
        iops_growth = (cur["iops"] - prev["iops"]) / prev["iops"] if prev["iops"] else 0.0
        prev_lat = prev["latency_p99_ms"] or 0.001
        latency_growth = (cur["latency_p99_ms"] - prev_lat) / prev_lat

        plateaued = iops_growth < _KNEE_IOPS_PLATEAU_THRESHOLD
        latency_spiked = latency_growth > _KNEE_LATENCY_GROWTH_MULTIPLE * max(iops_growth, 0.0)
        absolute_bad = cur["latency_p99_ms"] >= _KNEE_ABSOLUTE_LATENCY_MS

        if (plateaued and latency_spiked) or absolute_bad:
            return prev
    return None


def _collect_qos_notes(ip: str, pool: str) -> str | None:
    """Best-effort — rules out an artificial QoS cap being mistaken for a
    real performance ceiling (the operator's own first check-before-you-
    conclude item)."""
    spec = shlex.quote(f"{pool}/{SCRATCH_IMAGE_NAME}")
    try:
        output = execute_command(ip, f"rbd config image list {spec} 2>/dev/null | grep -i qos")
    except ExecutorError:
        return None
    return output.strip() or "Không có giới hạn QoS nào được đặt trên scratch image."


def _collect_bottleneck_notes(mon_ip: str, osd_ips: list[str]) -> str | None:
    """Best-effort supplementary evidence, raw and unparsed — surfaces the
    signal (disk %util, per-OSD commit latency) for the operator to read
    alongside the knee, same "don't overclaim a diagnosis" posture as this
    app's other read-only diagnostic tooling (shared/models.py's
    NodeDiagnosticRun). Captured right after the sweep's heaviest step
    rather than mid-load (concurrent-with-fio SSH capture would need a
    second connection racing the benchmark itself) — an approximation,
    documented as one, not a precise A/B on the exact bottlenecked moment.
    """
    parts: list[str] = []
    try:
        perf = execute_command(mon_ip, "ceph osd perf 2>/dev/null")
        if perf.strip():
            parts.append("ceph osd perf (commit_latency/apply_latency mỗi OSD):\n" + perf.strip())
    except ExecutorError:
        pass
    for ip in osd_ips[:5]:  # capped — an unbounded fan-out isn't worth it for a best-effort check
        try:
            iostat = execute_command(ip, "iostat -x 1 2 2>/dev/null")
        except ExecutorError:
            continue
        if iostat.strip():
            parts.append(f"iostat -x ({ip}, 2 mẫu cách nhau 1s — mẫu thứ 2 phản ánh %util gần nhất):\n" + iostat.strip())
    return "\n\n".join(parts) if parts else None


def run(
    action_pk: str,
    action_params: dict,
    incident_id: str,
    write_progress,
) -> bool:
    """Executes one load-sweep run. `action_params` must carry `pool`,
    `mon_ip` (pre-resolved by dashboard/routes/volumes.py at propose time —
    this module deliberately doesn't import watcher/ceph_client itself, same
    "self-contained worker executor" posture as cluster_deploy.py), and
    optionally `osd_ips` (best-effort diagnostics only) / `requested_by`.

    Writes live per-step progress via `write_progress` (same callback shape
    cluster_deploy.py's own run() uses) and, on completion or failure,
    persists the durable result to VolumePerfSweep — the Volumes page's
    chart reads that table directly, not Action.execution_progress."""
    pool = action_params.get("pool")
    mon_ip = action_params.get("mon_ip")
    osd_ips = action_params.get("osd_ips") or []
    requested_by = action_params.get("requested_by") or "unknown"

    if not pool or not mon_ip:
        logger.error("volume_perf.run: missing pool/mon_ip in action_params for action %s", action_pk)
        return False

    progress = [_make_step(key, label, pct) for key, label, pct in _STEP_DEFS]
    write_progress(action_pk, progress)

    sweep_id = str(uuid.uuid4())
    with db.SessionLocal() as session:
        session.add(
            VolumePerfSweep(
                id=sweep_id,
                action_id=action_pk,
                pool=pool,
                scratch_image=SCRATCH_IMAGE_NAME,
                requested_by=requested_by,
                status="RUNNING",
                steps_json="[]",
            )
        )
        session.commit()

    def _record_failure(step_index: int, message: str) -> None:
        progress[step_index]["status"] = "failed"
        progress[step_index]["message"] = message
        progress[step_index]["finished_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)
        with db.SessionLocal() as session:
            row = session.get(VolumePerfSweep, sweep_id)
            if row is not None:
                row.status = "FAILED"
                row.error_message = message
                row.finished_at = datetime.utcnow()
                session.commit()

    # --- prepare ---
    progress[0]["status"] = "running"
    progress[0]["started_at"] = datetime.utcnow().isoformat()
    write_progress(action_pk, progress)
    try:
        _check_fio_available(mon_ip)
        _ensure_scratch_image(mon_ip, pool)
    except VolumePerfError as exc:
        _record_failure(0, str(exc))
        return False
    progress[0]["status"] = "done"
    progress[0]["finished_at"] = datetime.utcnow().isoformat()
    write_progress(action_pk, progress)

    # --- sweep ---
    progress[1]["status"] = "running"
    progress[1]["started_at"] = datetime.utcnow().isoformat()
    write_progress(action_pk, progress)

    steps: list[dict] = []
    knee_confirmed_at: int | None = None
    for depth in IODEPTH_STEPS:
        host_entry = {"host": f"iodepth={depth}", "status": "running"}
        progress[1]["hosts"].append(host_entry)
        write_progress(action_pk, progress)

        try:
            step = _run_fio_step(mon_ip, pool, depth)
        except VolumePerfError as exc:
            host_entry["status"] = "failed"
            host_entry["message"] = str(exc)
            write_progress(action_pk, progress)
            _record_failure(1, str(exc))
            return False

        steps.append(step)
        host_entry["status"] = "done"
        host_entry["message"] = f"IOPS {step['iops']:.0f}, p99 {step['latency_p99_ms']:.2f}ms"
        write_progress(action_pk, progress)

        if knee_confirmed_at is None:
            if _detect_knee(steps) is not None:
                knee_confirmed_at = len(steps)  # run exactly one more step to confirm, then stop
        elif len(steps) > knee_confirmed_at:
            break

    progress[1]["status"] = "done"
    progress[1]["finished_at"] = datetime.utcnow().isoformat()
    write_progress(action_pk, progress)

    knee = _detect_knee(steps)

    # --- diagnostics (best-effort — must never fail an otherwise-good sweep) ---
    progress[2]["status"] = "running"
    progress[2]["started_at"] = datetime.utcnow().isoformat()
    write_progress(action_pk, progress)
    try:
        qos_notes = _collect_qos_notes(mon_ip, pool)
    except Exception:
        logger.exception("volume_perf.run: QoS diagnostics failed for action %s", action_pk)
        qos_notes = None
    try:
        bottleneck_notes = _collect_bottleneck_notes(mon_ip, osd_ips)
    except Exception:
        logger.exception("volume_perf.run: bottleneck diagnostics failed for action %s", action_pk)
        bottleneck_notes = None
    progress[2]["status"] = "done"
    progress[2]["finished_at"] = datetime.utcnow().isoformat()
    write_progress(action_pk, progress)

    with db.SessionLocal() as session:
        row = session.get(VolumePerfSweep, sweep_id)
        if row is not None:
            row.status = "DONE"
            row.steps_json = json.dumps(steps)
            if knee is not None:
                row.knee_iodepth = knee["iodepth"]
                row.knee_iops = knee["iops"]
                row.knee_latency_avg_ms = knee["latency_avg_ms"]
                row.knee_latency_p99_ms = knee["latency_p99_ms"]
            row.qos_notes = qos_notes
            row.bottleneck_notes = bottleneck_notes
            row.finished_at = datetime.utcnow()
            session.commit()

    return True
