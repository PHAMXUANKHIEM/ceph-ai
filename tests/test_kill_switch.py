from shared.kill_switch import is_kill_switch_enabled, set_kill_switch
from shared.models import SystemFlag


def test_is_kill_switch_enabled_true_when_flag_set(db_session):
    db_session.add(SystemFlag(key="kill_switch_enabled", value=True))
    db_session.commit()

    assert is_kill_switch_enabled(db_session) is True


def test_is_kill_switch_enabled_false_when_flag_set_false(db_session):
    db_session.add(SystemFlag(key="kill_switch_enabled", value=False))
    db_session.commit()

    assert is_kill_switch_enabled(db_session) is False


def test_is_kill_switch_enabled_fails_closed_when_row_missing(db_session):
    # Fail CLOSED (treated as enabled/blocking) — "can't determine the
    # state" must default to "don't execute", not "go ahead". In practice
    # the migration always seeds this row, but the function must default
    # conservatively if it's ever absent.
    assert is_kill_switch_enabled(db_session) is True


def test_set_kill_switch_updates_existing_row(db_session):
    db_session.add(SystemFlag(key="kill_switch_enabled", value=False))
    db_session.commit()

    set_kill_switch(db_session, True)
    db_session.commit()

    assert is_kill_switch_enabled(db_session) is True


def test_set_kill_switch_creates_row_when_missing(db_session):
    # Base.metadata.create_all() (this fixture) doesn't run the migration's
    # seed insert — set_kill_switch must still work against a bare table.
    set_kill_switch(db_session, True)
    db_session.commit()

    assert is_kill_switch_enabled(db_session) is True


def test_set_kill_switch_does_not_commit(db_session):
    db_session.add(SystemFlag(key="kill_switch_enabled", value=False))
    db_session.commit()

    set_kill_switch(db_session, True)
    db_session.rollback()

    assert is_kill_switch_enabled(db_session) is False
