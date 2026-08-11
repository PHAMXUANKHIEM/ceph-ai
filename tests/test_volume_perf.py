import copy
import json
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.executor.volume_perf as volume_perf_module
from shared import db as db_module
from shared.db import Base
from shared.models import VolumePerfSweep
from worker.executor.volume_perf import VOLUME_PERF_ACTION_IDS, VolumePerfError, _detect_knee, run


@pytest.fixture()
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    yield engine


def _make_recording_progress_writer():
    calls = []

    def write_progress(action_pk, progress):
        calls.append((action_pk, copy.deepcopy(progress)))

    return write_progress, calls


def _never_blocked(incident_id):
    return False


def _fio_json(iops, lat_avg_ms, lat_p99_ms):
    return json.dumps(
        {
            "jobs": [
                {
                    "write": {
                        "iops": iops,
                        "clat_ns": {
                            "mean": lat_avg_ms * 1_000_000,
                            "percentile": {"99.000000": lat_p99_ms * 1_000_000},
                        },
                    }
                }
            ]
        }
    )


def test_volume_perf_action_ids_registered():
    assert VOLUME_PERF_ACTION_IDS == {"volume_perf_sweep"}


# --- _parse_fio_json -------------------------------------------------------


def test_parse_fio_json_extracts_iops_and_latency():
    output = _fio_json(1000, 2.5, 9.8)
    step = volume_perf_module._parse_fio_json(output, iodepth=8)
    assert step == {
        "iodepth": 8,
        "iops": 1000.0,
        "latency_avg_ms": 2.5,
        "latency_p99_ms": 9.8,
        "bandwidth_mib_s": 3.91,
    }


def test_parse_fio_json_skips_leading_banner_noise():
    # Some distro fio builds print a version banner line before the JSON
    # blob even with --output-format=json — must not assume clean JSON.
    output = "fio-3.27\nSome startup banner text\n" + _fio_json(500, 1.0, 3.0)
    step = volume_perf_module._parse_fio_json(output, iodepth=4)
    assert step["iops"] == 500.0


def test_parse_fio_json_raises_on_no_json():
    with pytest.raises(VolumePerfError):
        volume_perf_module._parse_fio_json("permission denied", iodepth=1)


def test_parse_fio_json_raises_on_missing_fields():
    with pytest.raises(VolumePerfError):
        volume_perf_module._parse_fio_json(json.dumps({"jobs": [{}]}), iodepth=1)


# --- _detect_knee ------------------------------------------------------


def _step(iodepth, iops, lat_p99):
    return {"iodepth": iodepth, "iops": iops, "latency_avg_ms": lat_p99 / 2, "latency_p99_ms": lat_p99}


def test_detect_knee_requires_two_consecutive_saturation_transitions():
    # Operator's own description: crossing the knee gets ~3% more IOPS but
    # ~14x more latency — the KNEE returned must be the step BEFORE that
    # jump (the last good one), not the blown-up one itself.
    steps = [
        _step(16, 8000, 1.0),
        _step(32, 12000, 1.5),
        _step(64, 15000, 2.0),
        _step(128, 15450, 28.0),  # +3% IOPS, 14x latency vs. previous step
        _step(256, 15500, 45.0),  # second bad transition confirms it was not noise
    ]
    knee = _detect_knee(steps)
    assert knee is not None
    assert knee["iodepth"] == 64


def test_detect_knee_returns_none_when_still_scaling_cleanly():
    steps = [_step(1, 1000, 1.0), _step(2, 1950, 1.1), _step(4, 3800, 1.3)]
    assert _detect_knee(steps) is None


def test_detect_knee_does_not_trigger_on_one_noisy_latency_sample():
    steps = [_step(16, 8000, 1.0), _step(32, 8400, 25.0)]
    assert _detect_knee(steps) is None


def test_detect_knee_absolute_latency_needs_plateau_and_confirmation():
    steps = [_step(16, 8000, 1.0), _step(32, 8400, 25.0), _step(64, 8450, 35.0)]
    knee = _detect_knee(steps)
    assert knee is not None
    assert knee["iodepth"] == 16


def test_run_fio_depth_collects_two_extra_samples_when_initial_cv_is_high(monkeypatch):
    iops = iter([1000, 1600, 1000, 1010, 990])

    def fake_step(_ip, _pool, depth):
        return {
            "iodepth": depth,
            "iops": next(iops),
            "latency_avg_ms": 2.0,
            "latency_p99_ms": 5.0,
            "bandwidth_mib_s": 4.0,
        }

    monkeypatch.setattr(volume_perf_module, "_run_fio_step", fake_step)
    result = volume_perf_module._run_fio_depth("host", "pool", 8)

    assert result["sample_count"] == 5
    assert result["iops"] == 1000
    assert result["bandwidth_mib_s"] == 4.0


def test_detect_knee_needs_at_least_two_steps():
    assert _detect_knee([_step(1, 1000, 1.0)]) is None
    assert _detect_knee([]) is None


# --- run() end-to-end (fake SSH dispatcher) -------------------------------


def _fake_execute_saturating(host, command):
    if command == "command -v fio 2>/dev/null":
        return "/usr/bin/fio\n"
    if command.startswith("rbd info"):
        return ""  # scratch image already exists, `||` branch never runs
    if command.startswith("fio --name=sweep"):
        depth = int(re.search(r"--iodepth=(\d+)", command).group(1))
        # Scales cleanly through iodepth=16, then saturates hard from 32
        # onward — matches the operator's own knee shape.
        if depth <= 16:
            return _fio_json(depth * 1000, 1.0, 1.0 + depth * 0.05)
        return _fio_json(16500, 1.0, 30.0)
    if "grep -i qos" in command:
        return ""
    if command == "ceph osd perf 2>/dev/null":
        return "osd.0 1 2\n"
    if command.startswith("iostat"):
        return "Linux ...\ndevice util...\n"
    return ""


def test_run_happy_path_detects_knee_and_stops_early(isolated_db, monkeypatch):
    commands = []

    def recording_execute(host, command):
        commands.append(command)
        return _fake_execute_saturating(host, command)

    monkeypatch.setattr(volume_perf_module, "execute_command", recording_execute)
    write_progress, calls = _make_recording_progress_writer()

    action_params = {"pool": "vms", "mon_ip": "10.20.1.112", "osd_ips": ["10.20.1.95"], "requested_by": "admin"}
    result = run("action-1", action_params, "incident-1", write_progress, _never_blocked)

    assert result is True
    final = calls[-1][1]
    assert all(step["status"] == "done" for step in final)

    with db_module.SessionLocal() as session:
        row = session.query(VolumePerfSweep).one()
        assert row.status == "DONE"
        assert row.pool == "vms"
        assert row.knee_iodepth == 16
        assert row.knee_iops == 16000.0
        steps = json.loads(row.steps_json)
        assert all(step["sample_count"] == 3 for step in steps)
        assert [s["iodepth"] for s in steps] == [1, 2, 4, 8, 16, 32, 64]
    assert any(command.startswith("if rbd info") and "then rbd rm" in command for command in commands)


def test_run_records_no_knee_when_never_saturated(isolated_db, monkeypatch):
    def fake_execute(host, command):
        if command == "command -v fio 2>/dev/null":
            return "/usr/bin/fio\n"
        if command.startswith("rbd info"):
            return ""
        if command.startswith("fio --name=sweep"):
            depth = int(re.search(r"--iodepth=(\d+)", command).group(1))
            return _fio_json(depth * 1000, 1.0, 1.0)  # scales perfectly forever
        return ""

    monkeypatch.setattr(volume_perf_module, "execute_command", fake_execute)
    write_progress, _calls = _make_recording_progress_writer()

    action_params = {"pool": "vms", "mon_ip": "10.20.1.112"}
    result = run("action-2", action_params, "incident-2", write_progress, _never_blocked)

    assert result is True
    with db_module.SessionLocal() as session:
        row = session.query(VolumePerfSweep).one()
        assert row.status == "DONE"
        assert row.knee_iodepth is None
        steps = json.loads(row.steps_json)
        # Ran the FULL ladder since it never saturated.
        assert [s["iodepth"] for s in steps] == list(volume_perf_module.IODEPTH_STEPS)


def test_run_fails_when_fio_not_installed(isolated_db, monkeypatch):
    def fake_execute(host, command):
        if command == "command -v fio 2>/dev/null":
            return ""  # not found
        return ""

    monkeypatch.setattr(volume_perf_module, "execute_command", fake_execute)
    write_progress, calls = _make_recording_progress_writer()

    action_params = {"pool": "vms", "mon_ip": "10.20.1.112"}
    result = run("action-3", action_params, "incident-3", write_progress, _never_blocked)

    assert result is False
    prepare_step = calls[-1][1][0]
    assert prepare_step["status"] == "failed"
    assert "fio" in prepare_step["message"]

    with db_module.SessionLocal() as session:
        row = session.query(VolumePerfSweep).one()
        assert row.status == "FAILED"


def test_run_fails_when_a_sweep_step_ssh_fails(isolated_db, monkeypatch):
    from worker.executor.ssh_executor import ExecutorError

    commands = []

    def fake_execute(host, command):
        commands.append(command)
        if command == "command -v fio 2>/dev/null":
            return "/usr/bin/fio\n"
        if command.startswith("rbd info"):
            return ""
        if command.startswith("fio --name=sweep"):
            raise ExecutorError("connection reset")
        return ""

    monkeypatch.setattr(volume_perf_module, "execute_command", fake_execute)
    write_progress, calls = _make_recording_progress_writer()

    action_params = {"pool": "vms", "mon_ip": "10.20.1.112"}
    result = run("action-4", action_params, "incident-4", write_progress, _never_blocked)

    assert result is False
    with db_module.SessionLocal() as session:
        row = session.query(VolumePerfSweep).one()
        assert row.status == "FAILED"
        assert "connection reset" in row.error_message
    assert any(command.startswith("if rbd info") and "then rbd rm" in command for command in commands)


def test_run_is_failed_when_scratch_cleanup_fails(isolated_db, monkeypatch):
    from worker.executor.ssh_executor import ExecutorError

    def fake_execute(host, command):
        if command == "command -v fio 2>/dev/null":
            return "/usr/bin/fio\n"
        if command.startswith("if rbd info") and "; rbd create" in command:
            return ""
        if command.startswith("fio --name=sweep"):
            depth = int(re.search(r"--iodepth=(\d+)", command).group(1))
            return _fio_json(depth * 1000, 1.0, 1.0)
        if command.startswith("if rbd info") and "then rbd rm" in command:
            raise ExecutorError("rbd image still busy")
        return ""

    monkeypatch.setattr(volume_perf_module, "execute_command", fake_execute)
    write_progress, _calls = _make_recording_progress_writer()

    result = run("action-cleanup", {"pool": "vms", "mon_ip": "10.20.1.112"}, "incident", write_progress)

    assert result is False
    with db_module.SessionLocal() as session:
        row = session.query(VolumePerfSweep).one()
        assert row.status == "FAILED"
        assert "không xóa được scratch image" in row.error_message


def test_run_returns_false_when_pool_or_mon_ip_missing(isolated_db):
    write_progress, calls = _make_recording_progress_writer()
    assert run("action-6", {"pool": "vms"}, "incident-6", write_progress, _never_blocked) is False
    assert run("action-7", {"mon_ip": "10.20.1.112"}, "incident-7", write_progress, _never_blocked) is False
    with db_module.SessionLocal() as session:
        assert session.query(VolumePerfSweep).count() == 0
