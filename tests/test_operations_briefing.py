from datetime import datetime, timedelta
from types import SimpleNamespace

from watcher import operations_briefing as subject


class Query:
    def __init__(self, rows): self.rows = rows
    def filter(self, *_args): return self
    def order_by(self, *_args): return self
    def all(self): return self.rows


class Session:
    def __init__(self, rows): self.rows = rows
    def __enter__(self): return self
    def __exit__(self, *_args): pass
    def query(self, *_args): return Query(self.rows)


def _deps(monkeypatch, rows=(), disk=None, failure=None, capacity=None):
    monkeypatch.setattr(subject.db, "SessionLocal", lambda: Session(list(rows)))
    monkeypatch.setattr(subject, "predict", lambda *_args, **_kwargs: disk or {
        "osd_count": 3, "predictions": [], "_citations": [{"source_id": "osd-distribution:c"}],
    })
    monkeypatch.setattr(subject, "simulate", lambda *_args: failure or {"scenarios": []})
    monkeypatch.setattr(subject, "forecasts", lambda *_args: capacity or {"forecasts": []})


def test_briefing_prioritizes_lost_telemetry_and_serious_incident(monkeypatch):
    now = datetime(2026, 8, 22, 12)
    incident = SimpleNamespace(id="i1", cluster_id="c", ceph_code="OSD_DOWN", status="FAILED",
                               severity="HEALTH_ERR", detected_at=now - timedelta(hours=1))
    _deps(monkeypatch, [incident])
    result = subject.build("c", heartbeat_stale=True, now=now)
    assert [item["title"] for item in result["priorities"][:2]] == [
        "Mất telemetry từ cụm", "1 incident cần xử lý ngay",
    ]
    assert result["incidents_24h"] == 1
    assert result["priorities"][1]["evidence"][0]["source_id"] == "incident:i1"


def test_briefing_surfaces_capacity_failure_and_disk_risk(monkeypatch):
    disk = {"osd_count": 2, "predictions": [{"osd_id": 4, "risk_score": 82,
            "risk_level": "CRITICAL", "confidence": .8, "citations": [{"source_id": "incident:d"}]}]}
    failure = {"scenarios": [{"domain_type": "host", "domain_name": "ceph3",
                "max_osd_projected_percent": 91}], "_citations": [{"source_id": "crush:x"}]}
    _deps(monkeypatch, disk=disk, failure=failure)
    result = subject.build("c", now=datetime(2026, 8, 22, 12))
    assert result["priorities"][0]["title"] == "osd.4 có nguy cơ hỏng"
    assert "ceph3" in result["priorities"][1]["title"]


def test_briefing_healthy_state_is_honest_about_available_evidence(monkeypatch):
    _deps(monkeypatch)
    result = subject.build("c", now=datetime(2026, 8, 22, 12))
    assert result["priorities"][0]["severity"] == "LOW"
    assert result["disk_coverage"] == 3
    assert result["smart_metrics_available"] is False
