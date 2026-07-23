from datetime import datetime

from shared import heartbeat
from shared.models import WatcherHeartbeat


def test_record_creates_row_when_none_exists(db_session):
    polled_at = datetime.utcnow()

    heartbeat.record(
        db_session, success=True, mon_node="10.20.1.150", error_message=None, polled_at=polled_at
    )
    db_session.commit()

    row = db_session.query(WatcherHeartbeat).one()
    assert row.id == 1
    assert row.success is True
    assert row.mon_node == "10.20.1.150"
    assert row.error_message is None
    assert row.polled_at == polled_at


def test_record_updates_existing_row_in_place_not_a_new_one(db_session):
    heartbeat.record(
        db_session, success=True, mon_node="10.20.1.150", error_message=None, polled_at=datetime.utcnow()
    )
    db_session.commit()

    second_polled_at = datetime.utcnow()
    heartbeat.record(
        db_session,
        success=False,
        mon_node=None,
        error_message="All MON nodes failed",
        polled_at=second_polled_at,
    )
    db_session.commit()

    assert db_session.query(WatcherHeartbeat).count() == 1
    row = db_session.query(WatcherHeartbeat).one()
    assert row.success is False
    assert row.mon_node is None
    assert row.error_message == "All MON nodes failed"
    assert row.polled_at == second_polled_at


def test_record_does_not_commit_itself(db_session):
    heartbeat.record(
        db_session, success=True, mon_node="10.20.1.150", error_message=None, polled_at=datetime.utcnow()
    )
    db_session.rollback()

    assert db_session.query(WatcherHeartbeat).count() == 0


def test_get_latest_returns_none_when_never_recorded(db_session):
    assert heartbeat.get_latest(db_session) is None


def test_get_latest_returns_the_recorded_row(db_session):
    polled_at = datetime.utcnow()
    heartbeat.record(
        db_session, success=True, mon_node="10.20.1.150", error_message=None, polled_at=polled_at
    )
    db_session.commit()

    result = heartbeat.get_latest(db_session)

    assert result is not None
    assert result.mon_node == "10.20.1.150"
    assert result.polled_at == polled_at
