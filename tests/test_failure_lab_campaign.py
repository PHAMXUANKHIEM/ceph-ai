import json
from datetime import datetime, timedelta

import pytest

from shared import failure_lab_campaign as campaign
from shared import failure_lab_config
from shared import failure_lab_fault as fault
from shared import reproduction_approval as approvals

FSID = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
NIGHT = datetime(2026, 10, 9, 19, 30)  # 02:30 on 10/10 in Hanoi


class Runner:
    """Stand-in for the FL2 runner: records calls, returns a scored result."""

    def __init__(self, *, failed_stages=(), refuse=None, halt_dir=None):
        self.calls, self.failed_stages, self.refuse, self.halt_dir = [], failed_stages, refuse, halt_dir

    def _result(self, job):
        self.calls.append(job)
        if self.refuse:
            raise fault.FaultRefused(self.refuse)
        if self.halt_dir is not None:
            (self.halt_dir / fault.HALT_FILE).write_text("not recovered", encoding="utf-8")
        stages = {stage: stage not in self.failed_stages for stage in ("detection", "diagnosis", "cleanup")}
        return {"passed": all(stages.values()), "stages": stages, "target": "osd.3"}

    def fault(self, factory, *, cluster_id, scenario_id, scheduled, state_dir):
        assert scheduled and cluster_id == "lab-1"
        return self._result(f"fault:{scenario_id}")

    def proposal(self, factory, *, cluster_id, proposal_id, proposals_dir, scheduled, state_dir):
        assert scheduled and cluster_id == "lab-1"
        return self._result(f"proposal:{proposal_id}")


@pytest.fixture
def lab_on():
    failure_lab_config.save("admin", cluster_id="lab-1", fsid=FSID, fault_enabled=True, window="02:00-05:00")


def _campaign(tmp_path, runner, *, now=NIGHT, notices=None, **kwargs):
    return campaign.run_campaign(
        None, state_dir=tmp_path, proposals_dir=tmp_path / "proposals", clock=lambda: now,
        run_fault=runner.fault, run_proposal=runner.proposal,
        notify=(notices if notices is not None else []).append, **kwargs)


def _approve(directory, proposal_id):
    directory.mkdir(exist_ok=True)
    (directory / f"{proposal_id}.json").write_text(json.dumps(
        {"id": proposal_id, "status": approvals.APPROVED, "decided_at": "2026-10-09T08:00:00"}), encoding="utf-8")


def test_nothing_runs_unless_switched_on_and_inside_the_window(tmp_path):
    runner = Runner()
    assert _campaign(tmp_path, runner)["status"] == "off"
    failure_lab_config.save("admin", cluster_id="lab-1", fsid=FSID, fault_enabled=True, window="02:00-05:00")
    assert _campaign(tmp_path, runner, now=NIGHT + timedelta(hours=6))["status"] == "outside_window"
    (tmp_path / fault.HALT_FILE).write_text("x", encoding="utf-8")
    assert _campaign(tmp_path, runner)["status"] == "halted"
    assert runner.calls == []


def test_approved_ai_reproductions_go_first_then_catalog_faults_once_per_night(lab_on, tmp_path):
    _approve(tmp_path / "proposals", "repro-0123456789")
    runner, notices = Runner(failed_stages=("diagnosis",)), []

    result = _campaign(tmp_path, runner, notices=notices)

    assert runner.calls == ["proposal:repro-0123456789", "fault:osd_down_fault", "fault:osd_nearfull_fault"]
    assert result["stopped_because"] == "đủ 3 lượt"  # a wrong diagnosis is learning data, not a stop
    assert notices[0].startswith("🌙 Chiến dịch Failure Lab đêm 2026-10-10: 3 lượt (0 đạt).")
    assert "• fault:osd_down_fault trên osd.3: TRƯỢT diagnosis" in notices[0]
    assert _campaign(tmp_path, runner, now=NIGHT + timedelta(minutes=15))["status"] == "done_tonight"
    assert len(runner.calls) == 3


def test_the_catalog_rotates_across_nights(lab_on, tmp_path):
    runner = Runner()
    _campaign(tmp_path, runner, max_runs=1)
    _campaign(tmp_path, runner, now=NIGHT + timedelta(days=1), max_runs=1)
    _campaign(tmp_path, runner, now=NIGHT + timedelta(days=2), max_runs=1)

    assert runner.calls == ["fault:osd_down_fault", "fault:osd_nearfull_fault", "fault:osd_down_fault"]


def test_a_refusal_or_a_halt_ends_the_night(lab_on, tmp_path):
    refused, notices = Runner(refuse="a real OSD_DOWN incident is already open"), []
    result = _campaign(tmp_path, refused, notices=notices)
    assert len(refused.calls) == 1 and "OSD_DOWN incident" in result["stopped_because"]
    assert "bị từ chối" in notices[0]

    next_night = tmp_path / "next-night"
    next_night.mkdir()
    halting = Runner(halt_dir=next_night)
    result = _campaign(next_night, halting)
    assert result["stopped_because"].startswith("HALT") and halting.calls == ["fault:osd_down_fault"]


def test_the_window_end_stops_the_campaign(lab_on, tmp_path):
    times = iter([NIGHT, NIGHT, NIGHT, NIGHT + timedelta(hours=3)])
    runner = Runner()

    result = campaign.run_campaign(None, state_dir=tmp_path, proposals_dir=tmp_path / "p", clock=lambda: next(times),
                                   run_fault=runner.fault, run_proposal=runner.proposal, notify=lambda text: None)

    assert result["stopped_because"] == "hết khung giờ" and len(runner.calls) == 1


def test_a_window_across_midnight_belongs_to_the_night_it_started():
    assert campaign.night_of(datetime(2026, 10, 9, 17, 0), "23:00-03:00") == "2026-10-09"  # 00:00 Hanoi
    assert campaign.night_of(datetime(2026, 10, 9, 16, 30), "23:00-03:00") == "2026-10-09"  # 23:30 Hanoi
    assert campaign.night_of(NIGHT, "02:00-05:00") == "2026-10-10"


def test_the_timer_is_installed_and_runs_inside_the_worker():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    service = (root / "scripts/deploy/systemd/ceph-ai-failure-lab.service").read_text(encoding="utf-8")
    assert "podman exec ceph-ai_worker_1 python -m scripts.lab.failure_lab_campaign" in service
    assert "OnCalendar=*:0/15" in (root / "scripts/deploy/systemd/ceph-ai-failure-lab.timer").read_text(encoding="utf-8")
    deploy = (root / "scripts/deploy/restart_container_stack.sh").read_text(encoding="utf-8")
    assert "systemctl enable --now ceph-ai-failure-lab.timer" in deploy
