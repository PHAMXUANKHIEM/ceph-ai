from shared import online_learning_controls
from shared.model_registry import block_candidate, register_candidate, set_status


def test_pause_resume_is_scoped_and_append_only(db_session):
    row = online_learning_controls.set_status(
        db_session,
        cluster_id="cluster-a",
        host="node-a",
        metric="cpu",
        status=online_learning_controls.PAUSED,
        actor="admin",
        reason="investigate drift",
    )
    assert row.status == "PAUSED"
    assert online_learning_controls.is_paused(
        db_session, cluster_id="cluster-a", host="node-a", metric="cpu",
    ) is True
    assert online_learning_controls.is_paused(
        db_session, cluster_id="cluster-b", host="node-a", metric="cpu",
    ) is False

    online_learning_controls.set_status(
        db_session,
        cluster_id="cluster-a",
        host="node-a",
        metric="cpu",
        status=online_learning_controls.RUNNING,
        actor="admin",
        reason="scope reviewed",
    )
    assert online_learning_controls.is_paused(
        db_session, cluster_id="cluster-a", host="node-a", metric="cpu",
    ) is False
    assert [row.action for row in online_learning_controls.list_audit(
        db_session, cluster_id="cluster-a",
    )] == ["RESUME", "PAUSE"]


def test_reset_deletes_only_selected_state_and_audits(db_session):
    from shared.models import OnlineLearnerState

    db_session.add_all([
        OnlineLearnerState(
            cluster_key="cluster-a", host="node-a", metric="cpu",
            model_version="test-v1", algorithm="test", feature_schema="cpu-v1",
            state_json="{}", state_checksum="x", sample_count=1,
        ),
        OnlineLearnerState(
            cluster_key="cluster-a", host="node-b", metric="cpu",
            model_version="test-v1", algorithm="test", feature_schema="cpu-v1",
            state_json="{}", state_checksum="x", sample_count=1,
        ),
    ])
    db_session.flush()

    assert online_learning_controls.reset_state(
        db_session,
        cluster_id="cluster-a",
        host="node-a",
        metric="cpu",
        actor="admin",
        reason="rebuild corrupted state",
    ) == 1
    assert online_learning_controls.reset_state(
        db_session,
        cluster_id="cluster-a",
        host="node-a",
        metric="cpu",
        actor="admin",
        reason="verify idempotent reset",
    ) == 0
    assert online_learning_controls.list_audit(db_session, cluster_id="cluster-a")[0].action == "RESET_STATE"


def test_block_candidate_never_changes_active_model(db_session):
    common = dict(
        scope_type="NODE_RESOURCE", scope_key="cluster-a|node-a|cpu",
        name="resource", feature_schema="cpu-v1", training_window_hours=24,
    )
    active = register_candidate(db_session, version="linear:24h", algorithm="linear", **common)
    candidate = register_candidate(db_session, version="river:24h", algorithm="river_mean", **common)
    set_status(db_session, active, status="ACTIVE")
    set_status(db_session, candidate, status="SHADOW")

    blocked = block_candidate(
        db_session, candidate_id=candidate.id, actor="admin", reason="drift guard failed",
    )
    assert blocked.status == "BLOCKED"
    assert db_session.get(type(active), active.id).status == "ACTIVE"


import pytest


def test_normalize_scope_rejects_malformed_scope():
    with pytest.raises(ValueError, match="host is required"):
        online_learning_controls.normalize_scope(
            cluster_id="cluster-a", host="   ", metric="cpu",
        )
    with pytest.raises(ValueError, match="at most 255"):
        online_learning_controls.normalize_scope(
            cluster_id="cluster-a", host="n" * 256, metric="cpu",
        )
    with pytest.raises(ValueError, match="metric must be cpu or ram"):
        online_learning_controls.normalize_scope(
            cluster_id="cluster-a", host="node-a", metric="disk",
        )


def test_set_status_rejects_invalid_status_and_missing_reason(db_session):
    with pytest.raises(ValueError, match="status must be RUNNING or PAUSED"):
        online_learning_controls.set_status(
            db_session,
            cluster_id="cluster-a",
            host="node-a",
            metric="cpu",
            status="ACTIVE",
            actor="admin",
            reason="invalid status",
        )
    with pytest.raises(ValueError, match="reason is required"):
        online_learning_controls.set_status(
            db_session,
            cluster_id="cluster-a",
            host="node-a",
            metric="cpu",
            status=online_learning_controls.PAUSED,
            actor="admin",
            reason=" ",
        )
