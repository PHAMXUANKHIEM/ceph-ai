from shared.natural_language.incident_bridge import build_incident_candidate_signals


def _finding(**overrides):
    value = {
        "finding_id": "finding-1",
        "code": "OSD_DOWN",
        "severity": "critical",
        "status": "OBSERVED",
        "summary": "OSD is down",
        "entities": {"osd_id": 7},
        "evidence_gaps": [],
        "stale": False,
    }
    value.update(overrides)
    return value


def test_bridge_emits_bounded_deduplicated_cluster_scoped_signal():
    signals = build_incident_candidate_signals(
        [_finding(), _finding(finding_id="finding-2")], cluster_id="cluster-a"
    )
    assert len(signals) == 1
    signal = signals[0]
    assert signal.cluster_id == "cluster-a"
    assert signal.status == "CANDIDATE"
    assert signal.ceph_code == "OSD_DOWN"
    assert signal.to_dict()["schema_version"] == "incident-candidate-v1"


def test_bridge_fails_closed_for_info_stale_and_insufficient_evidence():
    findings = [
        _finding(severity="info"),
        _finding(stale=True),
        _finding(status="INSUFFICIENT_EVIDENCE"),
    ]
    assert build_incident_candidate_signals(findings, cluster_id="cluster-a") == ()


def test_bridge_does_not_mutate_finding_entities():
    entities = {"node": "ceph-a"}
    finding = _finding(entities=entities)
    signals = build_incident_candidate_signals([finding], cluster_id="cluster-a")
    signals[0].entities["extra"] = "only-signal"
    assert entities == {"node": "ceph-a"}
