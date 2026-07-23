import logging
import re

from watcher.ceph_client import run_command_on_node

logger = logging.getLogger(__name__)

# The remote script sleeps SAMPLE_INTERVAL_SECONDS between its two /proc
# samples, so the exec_command timeout must comfortably exceed that on top
# of normal SSH round-trip time — ceph_client's COMMAND_TIMEOUT_SECONDS (5s)
# is tuned for the near-instant `ceph health detail` query, not this.
METRICS_COMMAND_TIMEOUT_SECONDS = 8
SAMPLE_INTERVAL_SECONDS = 1

_MARKER_CPU1 = "===CPU1==="
_MARKER_DISK1 = "===DISK1==="
_MARKER_CPU2 = "===CPU2==="
_MARKER_DISK2 = "===DISK2==="
_MARKER_MEM = "===MEM==="

# Two /proc/stat + /proc/diskstats samples SAMPLE_INTERVAL_SECONDS apart, in
# one SSH round trip, so CPU%/IOPS/latency can be derived from the deltas —
# a single snapshot of either file is a cumulative counter, not a rate.
# /proc/meminfo only needs one sample (MemTotal/MemAvailable are already a
# point-in-time gauge, not a counter).
REMOTE_METRICS_SCRIPT = (
    f"echo {_MARKER_CPU1}; grep '^cpu ' /proc/stat; "
    f"echo {_MARKER_DISK1}; cat /proc/diskstats; "
    f"sleep {SAMPLE_INTERVAL_SECONDS}; "
    f"echo {_MARKER_CPU2}; grep '^cpu ' /proc/stat; "
    f"echo {_MARKER_DISK2}; cat /proc/diskstats; "
    f"echo {_MARKER_MEM}; cat /proc/meminfo"
)

# Whole-disk block devices only (sda, vdb, nvme0n1, ...) — excludes
# partitions (sda1, nvme0n1p1), loop devices, device-mapper/LVM (dm-*), and
# md/software-RAID. Without this filter a disk with N partitions would have
# its I/O counted N+1 times (once for the disk, once per partition).
_DISK_NAME_PATTERN = re.compile(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+)$")


class NodeMetricsError(Exception):
    """Raised when a node's /proc metrics can't be collected or parsed."""


def _extract_section(output: str, start_marker: str, end_marker: str | None) -> str:
    start = output.find(start_marker)
    if start == -1:
        raise NodeMetricsError(f"missing section {start_marker!r} in remote output")
    start += len(start_marker)
    end = len(output) if end_marker is None else output.find(end_marker, start)
    if end == -1:
        end = len(output)
    return output[start:end].strip()


def _parse_cpu_line(text: str) -> tuple[int, int]:
    """Returns (idle_ticks, total_ticks) from a `/proc/stat` 'cpu ' line.

    Fields (in order) are user, nice, system, idle, iowait, irq, softirq,
    steal, guest, guest_nice — guest/guest_nice are already folded into
    user/nice by the kernel, so only the first 8 fields are summed for
    `total` (see Documentation/filesystems/proc.rst).
    """
    fields = text.split()
    if len(fields) < 5 or fields[0] != "cpu":
        raise NodeMetricsError(f"unexpected /proc/stat cpu line: {text!r}")
    values = [int(v) for v in fields[1:9]]
    while len(values) < 8:
        values.append(0)
    idle = values[3] + values[4]  # idle + iowait
    total = sum(values)
    return idle, total


def _parse_diskstats(text: str) -> tuple[int, int, int, int]:
    """Returns (reads_completed, writes_completed, time_reading_ms,
    time_writing_ms) summed across whole-disk devices only — see
    Documentation/admin-guide/iostats.rst for the /proc/diskstats field
    layout (fields are 1-indexed there; `fields` below is 0-indexed).

    Kept as separate read/write totals (not combined) so the caller can
    report read IOPS and write IOPS as two distinct series — a single
    combined "IOPS" number hides the read/write split an SRE actually
    needs to tell a read-heavy scrub from a write-heavy backfill.
    """
    reads_completed = 0
    writes_completed = 0
    time_reading_ms = 0
    time_writing_ms = 0
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 11:
            continue
        name = fields[2]
        if not _DISK_NAME_PATTERN.match(name):
            continue
        reads_completed += int(fields[3])
        time_reading_ms += int(fields[6])
        writes_completed += int(fields[7])
        time_writing_ms += int(fields[10])
    return reads_completed, writes_completed, time_reading_ms, time_writing_ms


def _parse_meminfo(text: str) -> tuple[float, float]:
    """Returns (used_mb, total_mb) from `/proc/meminfo`."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        key = key.strip()
        if key in ("MemTotal", "MemAvailable"):
            values[key] = int(rest.strip().split()[0])  # kB
    if "MemTotal" not in values:
        raise NodeMetricsError("missing MemTotal in /proc/meminfo output")
    total_kb = values["MemTotal"]
    # MemAvailable (kernel >= 3.14) accounts for reclaimable caches, unlike
    # MemFree — falls back to "everything is used" only if truly absent.
    available_kb = values.get("MemAvailable", 0)
    used_kb = max(total_kb - available_kb, 0)
    return used_kb / 1024, total_kb / 1024


def parse_node_metrics(raw_output: str) -> dict:
    cpu1_text = _extract_section(raw_output, _MARKER_CPU1, _MARKER_DISK1)
    disk1_text = _extract_section(raw_output, _MARKER_DISK1, _MARKER_CPU2)
    cpu2_text = _extract_section(raw_output, _MARKER_CPU2, _MARKER_DISK2)
    disk2_text = _extract_section(raw_output, _MARKER_DISK2, _MARKER_MEM)
    mem_text = _extract_section(raw_output, _MARKER_MEM, None)

    idle1, total1 = _parse_cpu_line(cpu1_text)
    idle2, total2 = _parse_cpu_line(cpu2_text)
    delta_total = total2 - total1
    cpu_percent = 0.0
    if delta_total > 0:
        cpu_percent = max(0.0, min(100.0, (1 - (idle2 - idle1) / delta_total) * 100))

    reads1, writes1, time_r1, time_w1 = _parse_diskstats(disk1_text)
    reads2, writes2, time_r2, time_w2 = _parse_diskstats(disk2_text)
    delta_reads = max(reads2 - reads1, 0)
    delta_writes = max(writes2 - writes1, 0)
    delta_time_r_ms = max(time_r2 - time_r1, 0)
    delta_time_w_ms = max(time_w2 - time_w1, 0)
    disk_read_iops = delta_reads / SAMPLE_INTERVAL_SECONDS
    disk_write_iops = delta_writes / SAMPLE_INTERVAL_SECONDS
    # Latency stays one combined number (read+write time / read+write count) —
    # a single average that answers "is disk I/O slow right now", rather than
    # two separate lines that would double the panel for a distinction that
    # matters less than IOPS's read-vs-write split.
    delta_io_count = delta_reads + delta_writes
    delta_io_time_ms = delta_time_r_ms + delta_time_w_ms
    disk_latency_ms = (delta_io_time_ms / delta_io_count) if delta_io_count > 0 else 0.0

    mem_used_mb, mem_total_mb = _parse_meminfo(mem_text)
    mem_percent = (mem_used_mb / mem_total_mb * 100) if mem_total_mb > 0 else 0.0

    return {
        "cpu_percent": round(cpu_percent, 1),
        "mem_used_mb": round(mem_used_mb, 1),
        "mem_total_mb": round(mem_total_mb, 1),
        "mem_percent": round(mem_percent, 1),
        "disk_read_iops": round(disk_read_iops, 1),
        "disk_write_iops": round(disk_write_iops, 1),
        "disk_latency_ms": round(disk_latency_ms, 2),
    }


def collect_node_metrics(host: str) -> dict:
    """SSH to `host` and sample `/proc/stat`, `/proc/diskstats`, and
    `/proc/meminfo` (~1s apart) to compute host-level CPU%, RAM%, and disk
    IOPS/latency.

    Deliberately host-level (via /proc), not per-OSD Ceph op latency
    (`ceph osd perf`'s apply/commit latency): reusing the same SSH path as
    ceph_client's health/log queries here needs no osd-id -> hostname
    mapping, which watcher/collector.py's identify_relevant_nodes() notes
    is NOT cheaply available in this codebase (no `ceph osd tree`/`ceph
    osd find` query wired up). Host-level CPU/RAM/disk-IOPS/latency is
    still directly useful for spotting a saturated node and is what a
    Grafana "node exporter" style panel shows anyway.
    """
    try:
        raw_output = run_command_on_node(
            host, REMOTE_METRICS_SCRIPT, timeout=METRICS_COMMAND_TIMEOUT_SECONDS
        )
    except NodeMetricsError:
        raise
    except Exception as exc:
        raise NodeMetricsError(f"{host}: failed to collect metrics: {exc}") from exc
    return parse_node_metrics(raw_output)
