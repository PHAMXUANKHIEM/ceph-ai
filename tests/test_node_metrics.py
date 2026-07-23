import pytest

import watcher.node_metrics as node_metrics
from watcher.ceph_client import CephQueryError
from watcher.node_metrics import (
    METRICS_COMMAND_TIMEOUT_SECONDS,
    NodeMetricsError,
    REMOTE_METRICS_SCRIPT,
    collect_node_metrics,
    parse_node_metrics,
)

CPU1 = "cpu  1000 0 500 8000 100 0 0 0 0 0"
CPU2 = "cpu  1010 0 510 8005 100 0 0 0 0 0"  # +25 total ticks, +5 idle ticks -> 80% busy

DISK1 = "\n".join(
    [
        "   8       0 sda 100 5 2000 50 200 10 4000 100 0 80 150",
        "   8       1 sda1 40 2 800 20 80 4 1600 40 0 30 60",  # partition — must be excluded
        "   7       0 loop0 10 0 20 1 0 0 0 0 0 1 1",  # loop device — must be excluded
        " 259       0 nvme0n1 500 20 9000 300 400 15 8000 200 0 400 500",
    ]
)
DISK2 = "\n".join(
    [
        "   8       0 sda 150 5 3000 80 250 10 5000 150 0 100 200",
        "   8       1 sda1 60 2 1200 30 100 4 2000 60 0 45 90",
        "   7       0 loop0 10 0 20 1 0 0 0 0 0 1 1",
        " 259       0 nvme0n1 520 20 9400 320 420 15 8400 220 0 430 540",
    ]
)

MEM = "\n".join(
    [
        "MemTotal:       16384000 kB",
        "MemFree:         2000000 kB",
        "MemAvailable:    4000000 kB",
        "Buffers:          500000 kB",
    ]
)


def _raw_output(cpu1=CPU1, disk1=DISK1, cpu2=CPU2, disk2=DISK2, mem=MEM) -> str:
    return (
        f"===CPU1===\n{cpu1}\n"
        f"===DISK1===\n{disk1}\n"
        f"===CPU2===\n{cpu2}\n"
        f"===DISK2===\n{disk2}\n"
        f"===MEM===\n{mem}\n"
    )


def test_parse_node_metrics_computes_cpu_percent_from_stat_delta():
    result = parse_node_metrics(_raw_output())
    assert result["cpu_percent"] == 80.0


def test_parse_node_metrics_computes_ram_from_meminfo():
    result = parse_node_metrics(_raw_output())
    assert result["mem_total_mb"] == pytest.approx(16000.0)
    assert result["mem_used_mb"] == pytest.approx(12093.75, abs=0.1)  # rounded to 1 decimal by parse_node_metrics
    assert result["mem_percent"] == pytest.approx(75.586, abs=0.1)  # rounded to 1 decimal by parse_node_metrics


def test_parse_node_metrics_reports_read_and_write_iops_separately():
    # sda: +50 reads +50 writes; nvme0n1: +20 reads +20 writes -> 70 reads, 70 writes.
    # sda1 (partition) and loop0 must NOT be double-counted or counted at all.
    result = parse_node_metrics(_raw_output())
    assert result["disk_read_iops"] == 70.0
    assert result["disk_write_iops"] == 70.0


def test_parse_node_metrics_computes_disk_latency_from_time_delta_over_io_count():
    # sda time delta: (80-50)+(150-100)=80ms; nvme0n1: (320-300)+(220-200)=40ms -> 120ms / 140 ios
    result = parse_node_metrics(_raw_output())
    assert result["disk_latency_ms"] == pytest.approx(120 / 140, abs=0.01)


def test_parse_node_metrics_zero_disk_delta_yields_zero_latency_not_division_error():
    result = parse_node_metrics(_raw_output(disk1=DISK1, disk2=DISK1))
    assert result["disk_read_iops"] == 0.0
    assert result["disk_write_iops"] == 0.0
    assert result["disk_latency_ms"] == 0.0


def test_parse_node_metrics_raises_on_missing_section():
    with pytest.raises(NodeMetricsError):
        parse_node_metrics("garbage output with no markers at all")


def test_parse_node_metrics_raises_on_malformed_cpu_line():
    with pytest.raises(NodeMetricsError):
        parse_node_metrics(_raw_output(cpu1="not a cpu line"))


def test_parse_node_metrics_raises_on_missing_memtotal():
    with pytest.raises(NodeMetricsError):
        parse_node_metrics(_raw_output(mem="SomeOtherField: 123 kB"))


def test_collect_node_metrics_runs_expected_script_with_extended_timeout(monkeypatch):
    captured = {}

    def fake_run_command_on_node(host, command, timeout=None):
        captured["host"] = host
        captured["command"] = command
        captured["timeout"] = timeout
        return _raw_output()

    monkeypatch.setattr(node_metrics, "run_command_on_node", fake_run_command_on_node)

    result = collect_node_metrics("10.20.1.150")

    assert captured["host"] == "10.20.1.150"
    assert captured["command"] == REMOTE_METRICS_SCRIPT
    assert captured["timeout"] == METRICS_COMMAND_TIMEOUT_SECONDS
    assert result["cpu_percent"] == 80.0


def test_collect_node_metrics_wraps_ssh_failure_as_node_metrics_error(monkeypatch):
    def fake_run_command_on_node(host, command, timeout=None):
        raise CephQueryError(f"{host}: command exited 1: boom")

    monkeypatch.setattr(node_metrics, "run_command_on_node", fake_run_command_on_node)

    with pytest.raises(NodeMetricsError):
        collect_node_metrics("10.20.1.150")
