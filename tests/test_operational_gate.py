from worker.operational_gate import evaluate


def _status(**pgmap):
    return {"health": {"status": "HEALTH_OK"}, "monmap": {"num_mons": 3},
            "quorum_names": ["a", "b", "c"], "pgmap": pgmap}


def test_healthy_idle_cluster_passes():
    assert evaluate(_status(pgs_by_state=[{"state_name": "active+clean", "count": 32}])).allowed


def test_unknown_and_health_err_fail_closed():
    assert not evaluate({}).allowed
    assert not evaluate({"health": {"status": "HEALTH_ERR"}}).allowed


def test_lost_quorum_and_unavailable_pgs_block():
    assert "quorum" in evaluate({**_status(), "quorum_names": ["a"]}).reason
    result = evaluate(_status(pgs_by_state=[{"state_name": "inactive+undersized", "count": 2}]))
    assert not result.allowed and "inactive" in result.reason


def test_recovery_uses_configured_ceiling():
    snapshot = _status(recovering_bytes_per_sec=101)
    assert not evaluate(snapshot, max_recovery_bytes_per_sec=100).allowed
    assert evaluate(snapshot, max_recovery_bytes_per_sec=101).allowed


def test_zero_recovery_ceiling_disables_the_guard():
    snapshot = _status(recovering_bytes_per_sec=38_756_004)
    assert evaluate(snapshot, max_recovery_bytes_per_sec=0).allowed


def test_active_latency_incident_blocks():
    result = evaluate(_status(), active_latency_incidents=1)
    assert not result.allowed and "latency" in result.reason
