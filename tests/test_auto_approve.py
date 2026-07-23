from shared.auto_approve import (
    AUTO_APPROVE_RESTART_OSD_KEY,
    is_auto_approve_restart_osd_enabled,
    set_auto_approve_restart_osd,
)
from shared.models import SystemFlag


def test_is_auto_approve_restart_osd_enabled_true_when_flag_set(db_session):
    db_session.add(SystemFlag(key=AUTO_APPROVE_RESTART_OSD_KEY, value=True))
    db_session.commit()

    assert is_auto_approve_restart_osd_enabled(db_session) is True


def test_is_auto_approve_restart_osd_enabled_false_when_flag_set_false(db_session):
    db_session.add(SystemFlag(key=AUTO_APPROVE_RESTART_OSD_KEY, value=False))
    db_session.commit()

    assert is_auto_approve_restart_osd_enabled(db_session) is False


def test_is_auto_approve_restart_osd_enabled_fails_closed_when_row_missing(db_session):
    # Fail CLOSED (still requires approval) — conservative-by-default,
    # same posture as AD-5's classification and the kill-switch.
    assert is_auto_approve_restart_osd_enabled(db_session) is False


def test_set_auto_approve_restart_osd_updates_existing_row(db_session):
    db_session.add(SystemFlag(key=AUTO_APPROVE_RESTART_OSD_KEY, value=False))
    db_session.commit()

    set_auto_approve_restart_osd(db_session, True)
    db_session.commit()

    assert is_auto_approve_restart_osd_enabled(db_session) is True


def test_set_auto_approve_restart_osd_creates_row_when_missing(db_session):
    set_auto_approve_restart_osd(db_session, True)
    db_session.commit()

    assert is_auto_approve_restart_osd_enabled(db_session) is True


def test_set_auto_approve_restart_osd_does_not_commit(db_session):
    db_session.add(SystemFlag(key=AUTO_APPROVE_RESTART_OSD_KEY, value=False))
    db_session.commit()

    set_auto_approve_restart_osd(db_session, True)
    db_session.rollback()

    assert is_auto_approve_restart_osd_enabled(db_session) is False
