from shared.natural_language import Finding, analyze_evidence, analyze_evidence_bundle


def _snap(data, *, stale=False, available=True, partial=False):
    return {
        "data": data,
        "meta": {"available": available, "stale": stale, "partial": partial},
    }


def test_health_warn_is_observed_and_stable():
    snapshot = _snap({"health": {"status": "HEALTH_WARN", "checks": {"OSD_DOWN": {}}}})

    first = analyze_evidence("health", snapshot, cluster_id="prod")
    second = analyze_evidence("health", snapshot, cluster_id="prod")

    assert first[0].code == "HEALTH_WARN"
    assert first[0].severity == "warning"
    assert first[0].status == "OBSERVED"
    assert first[0].finding_id == second[0].finding_id
    assert first[0].recommended_action is None


def test_missing_health_never_becomes_healthy():
    findings = analyze_evidence("health", _snap({}, available=False), cluster_id="prod")

    assert findings[0].status == "INSUFFICIENT_EVIDENCE"
    assert findings[0].code == "HEALTH_EVIDENCE_MISSING"
    assert findings[0].confidence == 0


def test_osd_analyzer_finds_down_out_and_nearfull():
    snapshot = _snap({"osds": [
        {"osd": 1, "up": 0, "in": 0, "utilization_percent": 94},
        {"osd": 2, "up": 1, "in": 1, "utilization_percent": 10},
    ]})

    findings = analyze_evidence("osd_health", snapshot, cluster_id="prod")
    codes = {item.code for item in findings}

    assert {"OSD_DOWN", "OSD_OUT", "OSD_NEARFULL"} <= codes
    assert all(item.recommended_action is None for item in findings)


def test_osd_analyzer_detects_down_from_aggregate_counters():
    findings = analyze_evidence("osd_health", _snap({
        "status": {"osdmap": {"num_osds": 5, "num_up_osds": 4}},
    }), cluster_id="prod")

    assert findings[0].code == "OSD_DOWN"
    assert findings[0].entities["count"] == 1


def test_pg_analyzer_counts_bad_states():
    snapshot = _snap({"pgs": [
        {"state_name": "active+degraded", "count": 3},
        {"state_name": "active+undersized", "count": 2},
        {"state_name": "active+clean", "count": 10},
    ]})

    findings = analyze_evidence("pg_health", snapshot, cluster_id="prod")

    assert {item.code for item in findings} == {"PG_DEGRADED", "PG_UNDERSIZED"}
    assert {item.entities["count"] for item in findings} == {2, 3}


def test_mon_analyzer_detects_degraded_quorum():
    findings = analyze_evidence("mon_health", _snap({
        "status": {"monmap": {"num_mons": 3, "mons": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
                  "quorum_names": ["a", "b"]}
    }), cluster_id="prod")

    assert findings[0].code == "MON_QUORUM_DEGRADED"
    assert findings[0].severity == "critical"


def test_pool_node_and_backup_analyzers_use_thresholds():
    pool = analyze_evidence("pool_capacity", _snap({"pools": [{"name": "volumes", "used_percent": 92}]}), cluster_id="prod")
    node = analyze_evidence("node_metrics", _snap({"nodes": [{"host": "10.0.0.1", "cpu_percent": 95}]}), cluster_id="prod")
    backup = analyze_evidence("backup_status", _snap({"backup": {"failed_jobs": 1, "rpo_breaches": 2}}), cluster_id="prod")

    assert pool[0].code == "POOL_NEARFULL"
    assert node[0].code == "NODE_CPU_HIGH"
    assert {item.code for item in backup} == {"BACKUP_FAILED", "BACKUP_RPO_BREACH"}


def test_stale_evidence_is_preserved_and_exposed():
    findings = analyze_evidence("osd_health", _snap({"osdmap": {"num_osds": 3}}, stale=True), cluster_id="prod")

    assert findings[0].stale is True
    assert findings[0].evidence_gaps


def test_rgw_missing_evidence_is_not_a_healthy_conclusion():
    findings = analyze_evidence("rgw_health", _snap({}, available=False), cluster_id="prod")

    assert findings[0].status == "INSUFFICIENT_EVIDENCE"
    assert findings[0].code == "RGW_EVIDENCE_MISSING"


def test_crush_missing_host_is_observed_as_warning():
    findings = analyze_evidence("crush_analysis", _snap({"crush": {"nodes": [
        {"type": "osd", "id": 1},
    ]}}), cluster_id="prod")

    assert findings[0].code == "CRUSH_OSD_HOST_MISSING"
    assert findings[0].severity == "warning"


def test_to_dict_is_json_friendly_and_unknown_analyzer_fails_closed():
    findings = analyze_evidence("health", _snap({"health": {"status": "HEALTH_OK"}}))
    payload = findings[0].to_dict()

    assert isinstance(payload["evidence"], list)
    assert payload["recommended_action"] is None
    try:
        analyze_evidence("shell_commands", {}, cluster_id="prod")
    except ValueError as exc:
        assert "unknown deterministic analyzer" in str(exc)
    else:
        raise AssertionError("unknown analyzer must fail closed")


def test_bundle_sorts_severity_and_keeps_missing_evidence_explicit():
    findings = analyze_evidence_bundle({
        "health": _snap({"health": {"status": "HEALTH_OK"}}),
        "osd_health": _snap({"osds": [{"osd": 1, "up": 0}]}),
    }, analyzers=("health", "osd_health", "pg_health"), cluster_id="prod")

    assert findings[0].severity == "critical"
    assert any(item.code == "PG_EVIDENCE_MISSING" for item in findings)
    assert len({item.finding_id for item in findings}) == len(findings)
