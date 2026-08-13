"""End-to-end, read-only disk benchmark executed inside a VM over SSH."""

import json
import logging
import re
import shlex
import statistics
from datetime import datetime

from worker.executor.ssh_executor import ExecutorError, execute_command
from worker.executor.volume_perf import _detect_knee

logger = logging.getLogger(__name__)

VM_PERF_ACTION_ID = "vm_perf_benchmark"
IODEPTH_STEPS = (1, 4, 16, 32, 64)
FIO_RUNTIME_SECONDS = 20
FIO_RAMP_SECONDS = 5
FIO_SAMPLES_PER_DEPTH = 3
_DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9._+-]+$")


def _step(key: str, label: str, pct: int) -> dict:
    return {
        "step": key,
        "label": label,
        "pct": pct,
        "status": "pending",
        "message": None,
        "started_at": None,
        "finished_at": None,
    }


def _parse_fio_json(output: str, iodepth: int) -> dict:
    start = output.find("{")
    if start < 0:
        raise ValueError(f"fio không trả JSON hợp lệ ở iodepth={iodepth}")
    try:
        read = json.loads(output[start:])["jobs"][0]["read"]
        iops = float(read["iops"])
        clat = read.get("clat_ns") or read["lat_ns"]
        avg_ms = float(clat["mean"]) / 1_000_000
        p99_ms = float(clat["percentile"]["99.000000"]) / 1_000_000
        bw_bytes = float(read.get("bw_bytes", iops * 4096))
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"fio JSON thiếu trường cần thiết ở iodepth={iodepth}: {exc}") from exc
    return {
        "iodepth": iodepth,
        "iops": round(iops, 1),
        "bandwidth_mib_s": round(bw_bytes / 1024 / 1024, 2),
        "latency_avg_ms": round(avg_ms, 3),
        "latency_p99_ms": round(p99_ms, 3),
    }


def _run_sample(vm_ip: str, ssh_user: str, ssh_key_path: str, device: str, iodepth: int) -> dict:
    command = (
        "fio --name=ceph-ai-vm-read --readonly --rw=randread --bs=4k "
        f"--filename={shlex.quote(device)} --ioengine=libaio --direct=1 "
        f"--iodepth={iodepth} --numjobs=1 --runtime={FIO_RUNTIME_SECONDS} "
        f"--ramp_time={FIO_RAMP_SECONDS} --time_based --group_reporting "
        "--lat_percentiles=1 --percentile_list=99 --output-format=json"
    )
    output = execute_command(vm_ip, command, user=ssh_user, key_path=ssh_key_path)
    return _parse_fio_json(output, iodepth)


def run(action_pk: str, action_params: dict, _incident_id: str, write_progress, *_unused) -> bool:
    vm_ip = str(action_params.get("vm_ip") or "").strip()
    ssh_user = str(action_params.get("ssh_user") or "").strip()
    ssh_key_path = str(action_params.get("ssh_key_path") or "").strip()
    device = str(action_params.get("device") or "").strip()
    if not vm_ip or not ssh_user or not ssh_key_path or not _DEVICE_RE.fullmatch(device):
        logger.error("vm_perf.run: invalid/missing parameters for action %s", action_pk)
        return False

    progress = [
        _step("prepare", "Kiểm tra SSH, fio và ổ đĩa trong VM", 10),
        _step("sweep", "Đo đọc ngẫu nhiên 4K từ trong VM", 90),
        _step("complete", "Tổng hợp kết quả end-to-end", 100),
    ]
    write_progress(action_pk, progress)

    try:
        progress[0]["status"] = "running"
        progress[0]["started_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)
        check = (
            "command -v fio >/dev/null 2>&1 || { echo 'VM chưa cài fio' >&2; exit 20; }; "
            f"test -b {shlex.quote(device)} || {{ echo '{shlex.quote(device)} không phải block device' >&2; exit 21; }}; "
            f"lsblk -dn -o NAME,SIZE,TYPE,RO {shlex.quote(device)}"
        )
        disk_info = execute_command(vm_ip, check, user=ssh_user, key_path=ssh_key_path).strip()
        progress[0].update(
            status="done",
            message=f"Đã xác minh {device}: {disk_info or 'block device'}; phép đo chỉ đọc.",
            finished_at=datetime.utcnow().isoformat(),
        )
        write_progress(action_pk, progress)

        progress[1]["status"] = "running"
        progress[1]["started_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)
        measured = []
        for depth in IODEPTH_STEPS:
            samples = [
                _run_sample(vm_ip, ssh_user, ssh_key_path, device, depth)
                for _ in range(FIO_SAMPLES_PER_DEPTH)
            ]
            iops_values = [sample["iops"] for sample in samples]
            median_iops = statistics.median(iops_values)
            measured.append(
                {
                    "iodepth": depth,
                    "iops": round(median_iops, 1),
                    "bandwidth_mib_s": round(statistics.median(s["bandwidth_mib_s"] for s in samples), 2),
                    "latency_avg_ms": round(statistics.median(s["latency_avg_ms"] for s in samples), 3),
                    "latency_p99_ms": round(statistics.median(s["latency_p99_ms"] for s in samples), 3),
                    "sample_count": len(samples),
                    "iops_cv_pct": round(
                        statistics.stdev(iops_values) / median_iops * 100
                        if len(iops_values) > 1 and median_iops else 0.0,
                        2,
                    ),
                }
            )
            progress[1]["message"] = f"Đã đo iodepth={depth} ({len(samples)} mẫu)."
            write_progress(action_pk, progress)
        progress[1].update(status="done", finished_at=datetime.utcnow().isoformat())

        knee = _detect_knee(measured)
        progress[2].update(
            status="done",
            message="Hoàn tất benchmark đọc, không ghi dữ liệu lên ổ đĩa.",
            started_at=datetime.utcnow().isoformat(),
            finished_at=datetime.utcnow().isoformat(),
            result={
                "vm_ip": vm_ip,
                "device": device,
                "profile": "4K random read, direct I/O, read-only",
                "disk_info": disk_info,
                "steps": measured,
                "knee": knee,
            },
        )
        write_progress(action_pk, progress)
        return True
    except (ExecutorError, ValueError) as exc:
        current = next((step for step in progress if step["status"] == "running"), progress[-1])
        current.update(status="failed", message=str(exc), finished_at=datetime.utcnow().isoformat())
        write_progress(action_pk, progress)
        return False
