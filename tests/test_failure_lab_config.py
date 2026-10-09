import pytest

from config.settings import settings
from shared import failure_lab_config as config

FSID = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"


def test_without_a_file_the_env_settings_apply(monkeypatch):
    monkeypatch.setattr(settings, "failure_lab_cluster_fsid", FSID)
    monkeypatch.setattr(settings, "failure_lab_fault_enabled", True)

    loaded = config.load()

    assert (loaded.cluster_id, loaded.fsid, loaded.fault_enabled, loaded.window) == ("", FSID, True, "02:00-05:00")


def test_the_file_wins_over_env_and_records_who(monkeypatch):
    monkeypatch.setattr(settings, "failure_lab_fault_enabled", True)

    saved = config.save("admin", cluster_id="c1", fsid=FSID, fault_enabled=False, window="01:00-04:00",
                        telegram_chat_id="-1001234")

    assert (saved.fault_enabled, saved.window, saved.telegram_chat_id, saved.updated_by) == (
        False, "01:00-04:00", "-1001234", "admin")
    assert oct(config.CONFIG_PATH.stat().st_mode & 0o777) == "0o640"


@pytest.mark.parametrize(("changes", "message"), [
    ({"fault_enabled": True}, "ghim fsid"),
    ({"cluster_id": "c1", "fault_enabled": True}, "ghim fsid"),
    ({"window": "2h-5h"}, "HH:MM"),
    ({"fsid": "not-a-uuid"}, "UUID"),
    ({"telegram_chat_id": "@lab"}, "số"),
    ({"surprise": 1}, "không hợp lệ"),
])
def test_unsafe_or_malformed_changes_are_refused(changes, message):
    with pytest.raises(config.LabConfigError, match=message):
        config.save("admin", **changes)
    assert not config.CONFIG_PATH.exists()
