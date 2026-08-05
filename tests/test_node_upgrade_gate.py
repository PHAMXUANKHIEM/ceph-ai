import uuid
from datetime import datetime

from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    Incident,
    NodeUpgradeGate,
    NodeUpgradeGateLock,
    NodeUpgradeGateState,
)
from shared.node_upgrade_gate import (
    LOCK_ID,
    claim_node_upgrade_gate_lock,
    is_node_upgrade_gate_pending,
    release_node_upgrade_gate_lock,
)


def _make_action(db_session) -> str:
    # NodeUpgradeGate.prepare_action_id/confirm_action_id/abort_action_id
    # are real FKs to actions.id — this fixture's SQLite enforces them, so
    # exclude_action_id tests need a real Action row, not a bare uuid4.
    incident = Incident(ceph_code="NODE_OS_GATE", detected_at=datetime.utcnow())
    db_session.add(incident)
    db_session.flush()
    action = Action(
        incident_id=incident.id,
        action_id="node_os_gate_prepare",
        classification=ActionClassification.RISKY.value,
        status=ActionStatus.PENDING_APPROVAL.value,
    )
    db_session.add(action)
    db_session.flush()
    return action.id


def _seed_lock(db_session, active_gate_id=None):
    # Base.metadata.create_all() (the db_session fixture) doesn't run the
    # migration's seed insert — same reasoning as
    # test_set_kill_switch_creates_row_when_missing for SystemFlag.
    db_session.add(NodeUpgradeGateLock(id=LOCK_ID, active_gate_id=active_gate_id))
    db_session.commit()


def test_claim_succeeds_when_lock_is_free(db_session):
    _seed_lock(db_session)
    gate_id = str(uuid.uuid4())

    assert claim_node_upgrade_gate_lock(db_session, gate_id) is True
    db_session.commit()

    assert db_session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id == gate_id


def test_claim_fails_when_lock_already_held(db_session):
    held_by = str(uuid.uuid4())
    _seed_lock(db_session, active_gate_id=held_by)

    challenger = str(uuid.uuid4())
    assert claim_node_upgrade_gate_lock(db_session, challenger) is False
    db_session.commit()

    # The original holder must be unchanged — no exemption of any kind.
    assert db_session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id == held_by


def test_claim_does_not_commit(db_session):
    _seed_lock(db_session)
    gate_id = str(uuid.uuid4())

    claim_node_upgrade_gate_lock(db_session, gate_id)
    db_session.rollback()

    assert db_session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id is None


def test_release_frees_lock_held_by_matching_gate_id(db_session):
    gate_id = str(uuid.uuid4())
    _seed_lock(db_session, active_gate_id=gate_id)

    release_node_upgrade_gate_lock(db_session, gate_id)
    db_session.commit()

    assert db_session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id is None


def test_release_is_noop_for_non_matching_gate_id(db_session):
    held_by = str(uuid.uuid4())
    _seed_lock(db_session, active_gate_id=held_by)

    release_node_upgrade_gate_lock(db_session, str(uuid.uuid4()))
    db_session.commit()

    assert db_session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id == held_by


def test_release_does_not_commit(db_session):
    gate_id = str(uuid.uuid4())
    _seed_lock(db_session, active_gate_id=gate_id)

    release_node_upgrade_gate_lock(db_session, gate_id)
    db_session.rollback()

    assert db_session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id == gate_id


def test_release_then_reclaim_succeeds(db_session):
    first_gate_id = str(uuid.uuid4())
    _seed_lock(db_session, active_gate_id=first_gate_id)

    release_node_upgrade_gate_lock(db_session, first_gate_id)
    db_session.commit()

    second_gate_id = str(uuid.uuid4())
    assert claim_node_upgrade_gate_lock(db_session, second_gate_id) is True


def test_is_node_upgrade_gate_pending_false_with_no_rows(db_session):
    assert is_node_upgrade_gate_pending(db_session) is False


def test_is_node_upgrade_gate_pending_true_with_preparing_row(db_session):
    db_session.add(
        NodeUpgradeGate(
            host="10.20.1.83", target_version="18.2.4", state=NodeUpgradeGateState.PREPARING.value
        )
    )
    db_session.commit()

    assert is_node_upgrade_gate_pending(db_session) is True


def test_is_node_upgrade_gate_pending_true_with_recovering_row(db_session):
    db_session.add(
        NodeUpgradeGate(
            host="10.20.1.83", target_version="18.2.4", state=NodeUpgradeGateState.RECOVERING.value
        )
    )
    db_session.commit()

    assert is_node_upgrade_gate_pending(db_session) is True


def test_is_node_upgrade_gate_pending_false_with_only_terminal_rows(db_session):
    db_session.add(
        NodeUpgradeGate(host="10.20.1.83", target_version="18.2.4", state=NodeUpgradeGateState.DONE.value)
    )
    db_session.add(
        NodeUpgradeGate(host="10.20.1.78", target_version="18.2.4", state=NodeUpgradeGateState.FAILED.value)
    )
    db_session.commit()

    assert is_node_upgrade_gate_pending(db_session) is False


def test_is_node_upgrade_gate_pending_excludes_own_prepare_action(db_session):
    # Self-exemption: approving node_os_gate_prepare's own Action must not
    # be blocked by its own gate row being non-terminal.
    own_action_id = _make_action(db_session)
    db_session.add(
        NodeUpgradeGate(
            host="10.20.1.83",
            target_version="18.2.4",
            state=NodeUpgradeGateState.PREPARING.value,
            prepare_action_id=own_action_id,
        )
    )
    db_session.commit()

    assert is_node_upgrade_gate_pending(db_session, exclude_action_id=own_action_id) is False


def test_is_node_upgrade_gate_pending_excludes_own_confirm_action(db_session):
    own_action_id = _make_action(db_session)
    db_session.add(
        NodeUpgradeGate(
            host="10.20.1.83",
            target_version="18.2.4",
            state=NodeUpgradeGateState.PREPARED.value,
            confirm_action_id=own_action_id,
        )
    )
    db_session.commit()

    assert is_node_upgrade_gate_pending(db_session, exclude_action_id=own_action_id) is False


def test_is_node_upgrade_gate_pending_excludes_own_abort_action(db_session):
    own_action_id = _make_action(db_session)
    db_session.add(
        NodeUpgradeGate(
            host="10.20.1.83",
            target_version="18.2.4",
            state=NodeUpgradeGateState.PREPARED.value,
            abort_action_id=own_action_id,
        )
    )
    db_session.commit()

    assert is_node_upgrade_gate_pending(db_session, exclude_action_id=own_action_id) is False


def test_is_node_upgrade_gate_pending_still_detects_a_different_nodes_gate_when_excluding(db_session):
    # NULL-safety regression test: a naive `column != exclude_action_id`
    # filter (three-valued SQL logic) would wrongly drop this row from the
    # result just because its confirm_action_id/abort_action_id are NULL,
    # even though it belongs to a completely different node/action and
    # must still be detected as "something else pending".
    excluded_action_id = _make_action(db_session)
    db_session.add(
        NodeUpgradeGate(
            host="10.20.1.83",
            target_version="18.2.4",
            state=NodeUpgradeGateState.PREPARING.value,
            prepare_action_id=excluded_action_id,
        )
    )
    other_node_action_id = _make_action(db_session)
    db_session.add(
        NodeUpgradeGate(
            host="10.20.1.78",
            target_version="18.2.4",
            state=NodeUpgradeGateState.PREPARING.value,
            prepare_action_id=other_node_action_id,
        )
    )
    db_session.commit()

    assert is_node_upgrade_gate_pending(db_session, exclude_action_id=excluded_action_id) is True
