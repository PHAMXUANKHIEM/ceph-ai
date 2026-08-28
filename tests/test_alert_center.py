from datetime import datetime, timedelta
from types import SimpleNamespace

from dashboard.alert_center import build_alert_groups
from dashboard.routes.incidents import _rca_evidence


def _incident(code, status, when, *, cluster_id=None, root=None, ident=None):
    return SimpleNamespace(
        id=ident or code + status + when.isoformat(),
        ceph_code=code,
        status=status,
        detected_at=when,
        cluster_id=cluster_id,
        group_root_incident_id=root,
    )


def test_alert_center_merges_repeated_code_and_tracks_open_rows():
    now = datetime(2026, 8, 28, 10, 0)
    groups = build_alert_groups([
        _incident("PERFORMANCE_RCA:x", "RESOLVED", now - timedelta(hours=2), ident="old"),
        _incident("PERFORMANCE_RCA:x", "NEW", now, ident="new"),
        _incident("OSD_DOWN", "RESOLVED", now - timedelta(minutes=1), ident="other"),
    ])

    assert [group["ceph_code"] for group in groups] == ["PERFORMANCE_RCA:x", "OSD_DOWN"]
    assert groups[0]["occurrence_count"] == 2
    assert groups[0]["merged_count"] == 1
    assert groups[0]["active_count"] == 1
    assert groups[0]["representative"].id == "new"


def test_rca_evidence_is_compact_and_deduplicates_hosts():
    evidence = _rca_evidence('''{
        "source": "performance_rca", "hypothesis": "latency_candidate",
        "confidence": 0.848, "current_latency_ms": 11.8,
        "baseline_latency_ms": 7.9, "topology": {"pgid": "8.3a"},
        "host_evidence": [
            {"host": "ceph1", "cpu_percent": 69.5},
            {"host": "ceph1", "cpu_percent": 69.5},
            {"host": "ceph2", "mem_percent": 85.9}
        ], "api_token": "must not be copied"
    }''')

    assert evidence["hypothesis"] == "latency_candidate"
    assert [row["host"] for row in evidence["hosts"]] == ["ceph1", "ceph2"]
    assert "api_token" not in evidence
