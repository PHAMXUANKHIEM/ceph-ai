"""Failure Lab FL2: reversible real faults on the lab cluster (shared/failure_lab_fault.py)."""

from datetime import datetime, timedelta

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config.settings import settings
from shared import failure_lab_fault as fault
from shared.db import Base
from shared.models import Action, AuditEntry, Cluster, Incident

FSID = "11111111-2222-3333-4444-555555555555"


class FakeLab:
    """A tiny cluster: 4 OSDs, nearfull 0.85, the fullest OSD (osd.2) at 40 %."""

    def __init__(self, *, fsid=FSID, down_out=600, ok_to_stop=True, undo_fails=False, recovers=True):
        self.fsid, self.down_out, self.ok_to_stop = fsid, down_out, ok_to_stop
        self.undo_fails, self.recovers = undo_fails, recovers
        self.down: set[int] = set()
        self.nearfull = 0.85
        self.writes: list[str] = []
        self.extra_codes: set[str] = set()

    def codes(self):
        codes = set(self.extra_codes)
        if self.down:
            codes |= {"OSD_DOWN", "PG_DEGRADED"}
        if self.nearfull < 0.40:
            codes.add("OSD_NEARFULL")
        return codes

    def read(self, command):
        if command == "ceph fsid":
            return {"fsid": self.fsid}
        if command == "ceph health":
            return {"status": "HEALTH_WARN" if self.codes() else "HEALTH_OK", "checks": {c: {} for c in self.codes()}}
        if command == "ceph config get mon mon_osd_down_out_interval":
            return self.down_out
        if command == "ceph osd dump":
            return {"nearfull_ratio": self.nearfull,
                    "osds": [{"osd": i, "up": int(i not in self.down), "in": 1} for i in range(4)]}
        if command == "ceph osd df":
            return {"nodes": [{"id": i, "utilization": 40.0 if i == 2 else 20.0} for i in range(4)]}
        raise AssertionError(command)

    def write(self, command):
        self.writes.append(command)
        if command.startswith("ceph osd ok-to-stop") and not self.ok_to_stop:
            raise RuntimeError("would make PGs inactive")
        if command.startswith("ceph orch daemon stop osd."):
            self.down.add(int(command.rsplit(".", 1)[1]))
        elif command.startswith("ceph orch daemon start osd."):
            if self.undo_fails:
                raise RuntimeError("orchestrator unavailable")
            if self.recovers:
                self.down.discard(int(command.rsplit(".", 1)[1]))
        elif command.startswith("ceph osd set-nearfull-ratio"):
            self.nearfull = float(command.split()[-1])
        return ""


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, seconds):
        self.t += seconds

    def now(self):
        return self.t


def _db(environment="lab"):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        cluster = Cluster(name="CS-LAB", ceph_mon_nodes="10.0.0.1", ssh_user="root", is_default=True,
                          ssh_key_path="/tmp/k", autonomy_environment=environment)
        session.add(cluster)
        session.commit()
        return factory, cluster.id


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "failure_lab_fault_enabled", True)
    monkeypatch.setattr(settings, "failure_lab_cluster_fsid", FSID)
    monkeypatch.setattr(settings, "failure_lab_telegram_chat_id", "")


def _worker_reacts(factory, cluster_id, lab, code, diagnosis, action_id):
    """Stand-in for Watcher + Worker: once the fault shows, open and diagnose an incident."""
    state = {}

    def sleep(seconds):
        clock.sleep(seconds)
        if code in lab.codes() and "done" not in state:
            with factory() as session:
                incident = Incident(ceph_code=code, status="PENDING_APPROVAL", cluster_id=cluster_id,
                                    diagnosis_text=diagnosis, detected_at=datetime.utcnow(),
                                    created_at=datetime.utcnow())
                session.add(incident)
                session.flush()
                session.add(Action(incident_id=incident.id, action_id=action_id, classification="SAFE",
                                   status="PENDING_APPROVAL", target_nodes="[]"))
                session.commit()
            state["done"] = True

    clock = Clock()
    return sleep, clock.now


def _run(factory, cluster_id, lab, tmp_path, scenario="osd_down_fault", **kwargs):
    sleep = kwargs.pop("sleep", None)
    monotonic = kwargs.pop("monotonic", None)
    if sleep is None:
        clock = Clock()
        sleep, monotonic = clock.sleep, clock.now
    return fault.run_fault(factory, cluster_id=cluster_id, scenario_id=scenario, lab_factory=lambda cluster: lab,
                           state_dir=tmp_path, poll_seconds=10, sleep=sleep, monotonic=monotonic, **kwargs)


def test_a_stopped_osd_is_detected_scored_and_always_started_again(enabled, tmp_path):
    factory, cluster_id = _db()
    lab = FakeLab()
    sleep, now = _worker_reacts(factory, cluster_id, lab, "OSD_DOWN", "osd.3 đang down trên host", "restart_osd_daemon")

    result = _run(factory, cluster_id, lab, tmp_path, sleep=sleep, monotonic=now)

    assert lab.writes == ["ceph osd ok-to-stop 3", "ceph orch daemon stop osd.3", "ceph orch daemon start osd.3"]
    assert result["target"] == "osd.3" and result["passed"], result["stages"]
    assert not (tmp_path / fault.HALT_FILE).exists()
    with factory() as session:
        assert session.query(AuditEntry).filter_by(event_type="failure_lab_fault_run").count() == 1
        from shared.models import IncidentTimelineEvent

        label = session.query(IncidentTimelineEvent).filter_by(event_type=fault.LABEL_EVENT).one()
        evidence = json.loads(label.evidence_json)
        # FL3: the run's known cause is kept with the incident it raised.
        assert evidence["target"] == "osd.3" and "dừng daemon osd.3" in evidence["cause"]
        assert evidence["acceptable_action_ids"] == ["investigate_manually", "restart_osd_daemon"]
        assert evidence["passed"] is True and evidence["stages"]["diagnosis"] is True


def test_the_diagnosis_must_name_the_osd_this_run_really_stopped(enabled, tmp_path):
    factory, cluster_id = _db()
    lab = FakeLab()
    sleep, now = _worker_reacts(factory, cluster_id, lab, "OSD_DOWN", "osd.0 down", "investigate_manually")

    result = _run(factory, cluster_id, lab, tmp_path, sleep=sleep, monotonic=now)

    assert result["stages"]["diagnosis"] is False and result["stages"]["cleanup"] is True


def test_nearfull_lowers_the_ratio_under_the_fullest_osd_and_restores_it(enabled, tmp_path):
    factory, cluster_id = _db()
    lab = FakeLab()
    sleep, now = _worker_reacts(factory, cluster_id, lab, "OSD_NEARFULL", "osd.2 nearfull", "investigate_manually")

    result = _run(factory, cluster_id, lab, tmp_path, scenario="osd_nearfull_fault", sleep=sleep, monotonic=now)

    assert lab.writes == ["ceph osd set-nearfull-ratio 0.39", "ceph osd set-nearfull-ratio 0.85"]
    assert result["target"] == "osd.2" and result["passed"], result["stages"]


@pytest.mark.parametrize(("change", "reason"), [
    (lambda m, lab: m.setattr(settings, "failure_lab_fault_enabled", False), "FAILURE_LAB_FAULT_ENABLED"),
    (lambda m, lab: m.setattr(settings, "failure_lab_cluster_fsid", ""), "no staging fsid is pinned"),
    (lambda m, lab: setattr(lab, "fsid", "another-cluster"), "not the pinned lab fsid"),
    (lambda m, lab: setattr(lab, "down_out", 300), "mon_osd_down_out_interval"),
    (lambda m, lab: setattr(lab, "ok_to_stop", False), "ok-to-stop"),
    (lambda m, lab: lab.extra_codes.add("OSD_DOWN"), "already raised"),
])
def test_a_failed_gate_changes_nothing(enabled, tmp_path, monkeypatch, change, reason):
    factory, cluster_id = _db()
    lab = FakeLab()
    change(monkeypatch, lab)

    with pytest.raises(fault.FaultRefused, match=reason):
        _run(factory, cluster_id, lab, tmp_path)

    assert not [w for w in lab.writes if "stop osd" in w or "set-nearfull" in w]


def test_a_production_cluster_and_a_halted_lab_are_refused(enabled, tmp_path):
    factory, cluster_id = _db(environment="production")
    with pytest.raises(fault.FaultRefused, match="autonomy_environment=lab"):
        _run(factory, cluster_id, FakeLab(), tmp_path)

    factory, cluster_id = _db()
    (tmp_path / fault.HALT_FILE).write_text("earlier run did not recover")
    with pytest.raises(fault.FaultRefused, match="halted"):
        _run(factory, cluster_id, FakeLab(), tmp_path)


def test_a_scheduled_run_stays_inside_the_window(enabled, tmp_path, monkeypatch):
    assert fault.in_window(datetime(2026, 10, 7, 20, 30), "02:00-05:00")       # 03:30 in Hanoi
    assert not fault.in_window(datetime(2026, 10, 7, 3, 0), "02:00-05:00")     # 10:00 in Hanoi
    monkeypatch.setattr(fault, "utc_now", lambda: datetime(2026, 10, 7, 3, 0))
    factory, cluster_id = _db()
    with pytest.raises(fault.FaultRefused, match="Failure Lab window"):
        _run(factory, cluster_id, FakeLab(), tmp_path, scheduled=True)


def test_an_interrupted_run_still_undoes_the_fault_and_halts(enabled, tmp_path):
    factory, cluster_id = _db()
    lab = FakeLab()

    def interrupted(_seconds):
        raise KeyboardInterrupt

    clock = Clock()
    with pytest.raises(KeyboardInterrupt):
        _run(factory, cluster_id, lab, tmp_path, sleep=interrupted, monotonic=clock.now)

    assert lab.writes[-1] == "ceph orch daemon start osd.3" and not lab.down
    assert "interrupted" in (tmp_path / fault.HALT_FILE).read_text()


def test_a_cluster_that_does_not_recover_halts_further_runs(enabled, tmp_path):
    factory, cluster_id = _db()
    lab = FakeLab(recovers=False)

    result = _run(factory, cluster_id, lab, tmp_path)

    assert result["stages"]["recovery"] is False and not result["passed"]
    assert (tmp_path / fault.HALT_FILE).exists()


def test_the_worker_holds_incidents_inside_a_run_window(tmp_path):
    started = datetime(2026, 10, 7, 2, 0)
    fault._save_run(tmp_path, {"run_id": "r1", "cluster_id": "c1", "is_default": True,
                               "started_at": started.isoformat(), "ended_at": (started + timedelta(minutes=5)).isoformat(),
                               "deadline": (started + timedelta(minutes=15)).isoformat()})

    assert fault.holding_run("c1", started + timedelta(minutes=3), state_dir=tmp_path)["run_id"] == "r1"
    assert fault.holding_run(None, started + timedelta(minutes=19), state_dir=tmp_path) is not None  # grace, legacy row
    assert fault.holding_run("c1", started + timedelta(minutes=21), state_dir=tmp_path) is None
    assert fault.holding_run("c2", started + timedelta(minutes=3), state_dir=tmp_path) is None
    assert fault.holding_run("c1", started - timedelta(minutes=1), state_dir=tmp_path) is None


def test_the_catalog_only_has_known_kinds_and_safe_limits():
    catalog = fault.fault_scenarios()
    assert set(catalog) == {"osd_down_fault", "osd_nearfull_fault"}
    assert all(item.kind in fault.PREPARE and item.max_seconds <= 540 for item in catalog.values())


# --- FL6.4: operator-approved AI proposals (09/10/2026) ------------------------------------

def _proposal(directory, *, status="APPROVED", kind="stop_osd", seconds=120, pid="repro-00000000aa",
              decided_at="2026-10-09T08:00:00+00:00"):
    directory.mkdir(exist_ok=True)
    record = {"id": pid, "family": "network_heartbeat", "status": status, "decided_by": "telegram-chat:1 (op)",
              "decided_at": decided_at,
              "proposal": {"fault_kind": kind, "expected_health_codes": ["OSD_DOWN", "SLOW_OPS"],
                           "cause": "Một OSD daemon dừng nên MON đánh dấu down.", "max_seconds": seconds,
                           "acceptable_action_ids": ["investigate_manually"]}}
    (directory / f"{pid}.json").write_text(json.dumps(record))
    return pid


def test_an_approved_proposal_narrows_the_reviewed_fault_of_its_kind(tmp_path):
    pid = _proposal(tmp_path / "proposals", seconds=7200)
    record = json.loads((tmp_path / "proposals" / f"{pid}.json").read_text())

    scenario, replay = fault.scenario_from_proposal(record)

    base = fault.fault_scenarios()["osd_down_fault"]
    assert scenario.id == pid and scenario.kind == "stop_osd" and scenario.max_seconds == base.max_seconds
    assert scenario.expected_health_codes == base.expected_health_codes | {"SLOW_OPS"}
    assert replay.acceptable_action_ids == ("investigate_manually",)
    with pytest.raises(fault.FaultRefused, match="no reviewed fault"):
        fault.scenario_from_proposal({**record, "proposal": {**record["proposal"], "fault_kind": "rm_rf"}})


def test_an_approved_proposal_runs_once_and_its_label_names_the_proposal(enabled, tmp_path):
    factory, cluster_id = _db()
    lab = FakeLab()
    sleep, now = _worker_reacts(factory, cluster_id, lab, "OSD_DOWN", "osd.3 đang down trên host", "investigate_manually")
    proposals = tmp_path / "proposals"
    pid = _proposal(proposals)

    result = fault.run_proposal(factory, cluster_id=cluster_id, proposal_id=pid, proposals_dir=proposals,
                                lab_factory=lambda cluster: lab, state_dir=tmp_path, poll_seconds=10,
                                sleep=sleep, monotonic=now)

    assert result["passed"] and lab.writes[-1] == "ceph orch daemon start osd.3"  # always undone
    stored = json.loads((proposals / f"{pid}.json").read_text())
    assert stored["status"] == "DONE" and stored["run_id"] == result["run_id"] and stored["target"] == "osd.3"
    with factory() as session:
        from shared.models import IncidentTimelineEvent

        label = json.loads(session.query(IncidentTimelineEvent).filter_by(event_type=fault.LABEL_EVENT).one().evidence_json)
    assert label["proposal_id"] == pid and label["approved_by"] == "telegram-chat:1 (op)"
    assert label["proposed_cause"].startswith("Một OSD daemon dừng")
    with pytest.raises(fault.FaultRefused, match="DONE, not APPROVED"):
        fault.run_proposal(factory, cluster_id=cluster_id, proposal_id=pid, proposals_dir=proposals)


def test_an_unapproved_or_refused_proposal_changes_nothing(enabled, tmp_path):
    factory, cluster_id = _db(environment="production")
    proposals = tmp_path / "proposals"
    waiting = _proposal(proposals, status="PROPOSED", pid="repro-00000000bb")
    approved = _proposal(proposals, pid="repro-00000000cc")
    lab = FakeLab()

    with pytest.raises(fault.FaultRefused, match="PROPOSED, not APPROVED"):
        fault.run_proposal(factory, cluster_id=cluster_id, proposal_id=waiting, proposals_dir=proposals)
    with pytest.raises(fault.FaultRefused):  # a production cluster fails the FL2 gate
        fault.run_proposal(factory, cluster_id=cluster_id, proposal_id=approved, proposals_dir=proposals,
                           lab_factory=lambda cluster: lab, state_dir=tmp_path)

    assert lab.writes == []
    stored = json.loads((proposals / f"{approved}.json").read_text())
    assert stored["status"] == "APPROVED" and stored["last_refusal"]  # left for a later, valid run


def test_next_runs_the_oldest_approved_proposal(tmp_path):
    from shared import reproduction_approval

    proposals = tmp_path / "proposals"
    _proposal(proposals, pid="repro-00000000dd", decided_at="2026-10-09T09:00:00+00:00")
    _proposal(proposals, pid="repro-00000000ee", decided_at="2026-10-09T07:00:00+00:00")
    _proposal(proposals, pid="repro-00000000ff", status="PROPOSED", decided_at="2026-10-09T06:00:00+00:00")

    assert reproduction_approval.next_approved(proposals) == "repro-00000000ee"
    assert reproduction_approval.next_approved(tmp_path / "missing") is None
