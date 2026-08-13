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
import statistics
import uuid
from datetime import datetime

from shared import db
from shared.models import VolumePerfSweep
from worker.executor.ssh_executor import ExecutorError, execute_command
from worker.policy.gate import VALID_VOLUME_PERF_ACTION_IDS

logger = logging.getLogger(__name__)

VOLUME_PERF_ACTION_IDS = VALID_VOLUME_PERF_ACTION_IDS

# Leading underscore: purely a visual "this is tool-owned" signal. The image
# is deleted after every run (including failed runs), so each benchmark starts
# from a fresh thin-provisioned image and cannot silently retain 50 GiB of
# allocated random-write data between runs.
SCRATCH_IMAGE_NAME = "_ceph_aiops_perf_probe"
SCRATCH_IMAGE_SIZE_GB = 50

# Same iodepth ladder the operator's own proven script used.
IODEPTH_STEPS = (1, 2, 4, 8, 16, 32, 64, 128, 256)
# p99 from a 20-second window was too sensitive to short-lived recovery/
# scrub bursts. 30 seconds of measured traffic after a 10-second ramp is a
# better compromise for an operator-triggered ceiling test. With 3 samples
# this is intentionally a thorough test (up to ~18 minutes for all 9 depths,
# normally shorter because the sweep stops after a confirmed knee).
FIO_RUNTIME_SECONDS = 30
FIO_RAMP_SECONDS = 10
FIO_SAMPLES_PER_DEPTH = 3
# If the first 3 IOPS samples disagree by more than this, take 2 more and
# use the median of 5. A noisy cluster gets more evidence automatically;
# a stable cluster does not pay that extra load/time.
FIO_SAMPLE_CV_RETRY_PERCENT = 7.5
FIO_EXTRA_SAMPLES_ON_NOISE = 2

# Knee-detection thresholds (see _detect_knee) — deliberately two signals
# combined, not one: the operator's own example (~3% more IOPS for ~14x
# more latency) is an extreme case; requiring BOTH "IOPS basically
# plateaued" AND "latency grew disproportionately" avoids flagging a knee
# on ordinary noisy variance between two points that are both still
# scaling normally. The absolute cutoff is a safety net independent of
# growth shape, so a sweep doesn't grind on toward iodepth=256 once
# latency is already clearly unacceptable.
_KNEE_IOPS_PLATEAU_THRESHOLD = 0.15
_KNEE_LATENCY_TO_IOPS_GROWTH_RATIO = 3.0
_KNEE_MIN_LATENCY_DELTA_MS = 2.0
_KNEE_ABSOLUTE_LATENCY_MS = 20.0
_KNEE_CONFIRMING_TRANSITIONS = 2

_STEP_DEFS = [
    ("prepare", "Chuẩn bị scratch image + kiểm tra fio", 10),
    ("sweep", "Quét tải tăng dần, median 3 mẫu; tự tăng 5 mẫu khi nhiễu (iodepth 1→256)", 85),
    ("diagnostics", "Thu thập QoS / bottleneck diagnostics", 95),
    ("cleanup", "Xóa scratch image 50 GiB", 100),
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
    # A stale probe can remain after a host/network crash that prevented the
    # previous run's cleanup. Remove it first instead of benchmarking against
    # previously allocated/randomized blocks.
    cmd = (
        f"if rbd info {spec} >/dev/null 2>&1; then rbd rm {spec} || exit $?; fi; "
        f"rbd create --size {SCRATCH_IMAGE_SIZE_GB}G {spec}"
    )
    try:
        execute_command(ip, cmd)
    except ExecutorError as exc:
        raise VolumePerfError(
            f"{ip}: không tạo được scratch image {pool}/{SCRATCH_IMAGE_NAME}: {exc}"
        ) from exc


def _remove_scratch_image(ip: str, pool: str) -> None:
    """Remove benchmark data completely; idempotent when prepare never made it."""
    spec = shlex.quote(f"{pool}/{SCRATCH_IMAGE_NAME}")
    try:
        execute_command(ip, f"if rbd info {spec} >/dev/null 2>&1; then rbd rm {spec}; fi")
    except ExecutorError as exc:
        raise VolumePerfError(
            f"{ip}: không xóa được scratch image {pool}/{SCRATCH_IMAGE_NAME}: {exc}"
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
        # fio 3.x exposes bytes/s here. Older builds/tests may omit it, so
        # keep this backward-compatible instead of rejecting an otherwise
        # valid latency/IOPS sample.
        bandwidth_bytes_s = float(write.get("bw_bytes", iops * 4096))
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise VolumePerfError(f"fio JSON thiếu trường cần thiết ở iodepth={iodepth}: {exc}") from exc
    return {
        "iodepth": iodepth,
        "iops": round(iops, 1),
        "latency_avg_ms": round(lat_avg_ms, 3),
        "latency_p99_ms": round(lat_p99_ms, 3),
        "bandwidth_mib_s": round(bandwidth_bytes_s / 1024 / 1024, 2),
    }


def _run_fio_step(ip: str, pool: str, iodepth: int) -> dict:
    cmd = (
        "fio --name=sweep --ioengine=rbd "
        f"--pool={shlex.quote(pool)} --rbdname={shlex.quote(SCRATCH_IMAGE_NAME)} "
        f"--rw=randwrite --bs=4k --iodepth={iodepth} --numjobs=1 "
        f"--runtime={FIO_RUNTIME_SECONDS} --ramp_time={FIO_RAMP_SECONDS} --time_based --direct=1 "
        "--invalidate=1 --randrepeat=0 --norandommap --thread=1 --group_reporting "
        "--lat_percentiles=1 --percentile_list=99 --output-format=json"
    )
    try:
        output = execute_command(ip, cmd)
    except ExecutorError as exc:
        raise VolumePerfError(f"{ip}: fio thất bại ở iodepth={iodepth}: {exc}") from exc
    return _parse_fio_json(output, iodepth)


def _coefficient_of_variation_percent(values: list[float]) -> float:
    median = statistics.median(values)
    return (statistics.stdev(values) / median * 100) if len(values) > 1 and median else 0.0


def _run_fio_depth(ip: str, pool: str, iodepth: int) -> dict:
    """Use median samples; automatically gather more evidence when noisy."""
    samples = [_run_fio_step(ip, pool, iodepth) for _ in range(FIO_SAMPLES_PER_DEPTH)]
    iops_values = [sample["iops"] for sample in samples]
    if _coefficient_of_variation_percent(iops_values) > FIO_SAMPLE_CV_RETRY_PERCENT:
        samples.extend(
            _run_fio_step(ip, pool, iodepth) for _ in range(FIO_EXTRA_SAMPLES_ON_NOISE)
        )
        iops_values = [sample["iops"] for sample in samples]
    iops_median = statistics.median(iops_values)
    return {
        "iodepth": iodepth,
        "iops": round(iops_median, 1),
        "latency_avg_ms": round(statistics.median(s["latency_avg_ms"] for s in samples), 3),
        "latency_p99_ms": round(statistics.median(s["latency_p99_ms"] for s in samples), 3),
        "bandwidth_mib_s": round(statistics.median(s["bandwidth_mib_s"] for s in samples), 2),
        "sample_count": len(samples),
        "iops_cv_pct": round(_coefficient_of_variation_percent(iops_values), 2),
    }


def _is_saturation_transition(prev: dict, cur: dict) -> bool:
    if not prev["iops"]:
        return False
    iops_growth = (cur["iops"] - prev["iops"]) / prev["iops"]
    prev_lat = max(prev["latency_p99_ms"], 0.001)
    latency_growth = (cur["latency_p99_ms"] - prev_lat) / prev_lat
    latency_delta = cur["latency_p99_ms"] - prev_lat
    plateaued = iops_growth < _KNEE_IOPS_PLATEAU_THRESHOLD
    # A knee is a SHAPE in the IOPS/latency curve, not merely an SLA
    # violation. A high absolute p99 at depth=1 means the cluster baseline
    # is already unhealthy; it does not prove that depth=1 is its physical
    # IOPS ceiling. Therefore the 20 ms threshold is surfaced separately as
    # an operational warning and never creates a knee by itself.
    latency_disproportionate = (
        latency_delta >= _KNEE_MIN_LATENCY_DELTA_MS
        and latency_growth
        > _KNEE_LATENCY_TO_IOPS_GROWTH_RATIO * max(iops_growth, 0.0)
    )
    return plateaued and latency_disproportionate


def _detect_knee(steps: list[dict]) -> dict | None:
    """Returns the LAST step before the cliff (the usable ceiling — "how
    far can this be pushed before it falls off"), or None if the sweep
    never saturated within the tested range (steps[-1] is then a
    lower-bound floor, not a real ceiling)."""
    for i in range(1, len(steps)):
        knee_candidate = steps[i - 1]
        if not _is_saturation_transition(knee_candidate, steps[i]):
            continue
        # One additional depth confirms that performance remains beyond the
        # same last-good point. We compare confirmation to knee_candidate,
        # not to the already-bad sample: latency may stay on a high plateau
        # instead of increasing again, and that still confirms the cliff.
        confirmation_index = i + _KNEE_CONFIRMING_TRANSITIONS - 1
        if confirmation_index < len(steps) and _is_saturation_transition(
            knee_candidate, steps[confirmation_index]
        ):
            return knee_candidate
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
    *_unused,
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

    def _record_failure(step_index: int, message: str, measured_steps: list[dict] | None = None) -> None:
        progress[step_index]["status"] = "failed"
        progress[step_index]["message"] = message
        progress[step_index]["finished_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)
        with db.SessionLocal() as session:
            row = session.get(VolumePerfSweep, sweep_id)
            if row is not None:
                row.status = "FAILED"
                row.error_message = message
                if measured_steps is not None:
                    row.steps_json = json.dumps(measured_steps)
                row.finished_at = datetime.utcnow()
                session.commit()

    def _cleanup() -> str | None:
        cleanup_step = progress[3]
        cleanup_step["status"] = "running"
        cleanup_step["started_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)
        try:
            _remove_scratch_image(mon_ip, pool)
        except VolumePerfError as exc:
            cleanup_step["status"] = "failed"
            cleanup_step["message"] = str(exc)
            cleanup_step["finished_at"] = datetime.utcnow().isoformat()
            write_progress(action_pk, progress)
            return str(exc)
        cleanup_step["status"] = "done"
        cleanup_step["message"] = "Đã xóa scratch image; lần đo sau sẽ tạo image mới."
        cleanup_step["finished_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)
        return None

    # --- prepare ---
    progress[0]["status"] = "running"
    progress[0]["started_at"] = datetime.utcnow().isoformat()
    write_progress(action_pk, progress)
    try:
        _check_fio_available(mon_ip)
        _ensure_scratch_image(mon_ip, pool)
    except VolumePerfError as exc:
        cleanup_error = _cleanup()
        message = str(exc) + (f"; cleanup cũng thất bại: {cleanup_error}" if cleanup_error else "")
        _record_failure(0, message)
        return False
    progress[0]["status"] = "done"
    progress[0]["finished_at"] = datetime.utcnow().isoformat()
    write_progress(action_pk, progress)

    # --- sweep ---
    progress[1]["status"] = "running"
    progress[1]["started_at"] = datetime.utcnow().isoformat()
    write_progress(action_pk, progress)

    steps: list[dict] = []
    for depth in IODEPTH_STEPS:
        host_entry = {"host": f"iodepth={depth}", "status": "running"}
        progress[1]["hosts"].append(host_entry)
        write_progress(action_pk, progress)

        try:
            step = _run_fio_depth(mon_ip, pool, depth)
        except VolumePerfError as exc:
            host_entry["status"] = "failed"
            host_entry["message"] = str(exc)
            write_progress(action_pk, progress)
            cleanup_error = _cleanup()
            message = str(exc) + (f"; cleanup cũng thất bại: {cleanup_error}" if cleanup_error else "")
            _record_failure(1, message, steps)
            return False

        steps.append(step)
        host_entry["status"] = "done"
        host_entry["message"] = (
            f"median {step['sample_count']} mẫu: IOPS {step['iops']:.0f}, "
            f"p99 {step['latency_p99_ms']:.2f}ms, độ lệch IOPS {step['iops_cv_pct']:.1f}%"
        )
        write_progress(action_pk, progress)

        # _detect_knee requires the first bad transition plus one additional
        # depth that remains beyond the same last-good point.
        if _detect_knee(steps) is not None:
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

    cleanup_error = _cleanup()
    if cleanup_error:
        _record_failure(3, cleanup_error, steps)
        return False

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
