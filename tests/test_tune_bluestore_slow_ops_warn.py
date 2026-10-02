import pytest

from dashboard import chat_client
from shared.models import ActionClassification
from worker.executor import commands
from worker.executor.commands import ExecutorError, get_command, tune_bluestore_slow_ops_warn_rollback_command
from worker.policy import gate

ACTION = "tune_bluestore_slow_ops_warn"
PARAMS = {"device_class": "hdd", "threshold": 5, "lifetime_seconds": 3600}


def test_command_sets_both_options_for_one_device_class():
    command = get_command(ACTION, "10.3.53.1", PARAMS)

    assert "ceph config set osd/class:hdd bluestore_slow_ops_warn_threshold 5" in command
    assert "ceph config set osd/class:hdd bluestore_slow_ops_warn_lifetime 3600" in command


def test_preflight_records_current_values_and_refuses_an_existing_override():
    command = get_command(ACTION, "10.3.53.1", PARAMS)
    preflight, _, change = command.partition("ceph config set")

    assert preflight.startswith("ceph config dump | grep -E 'bluestore_slow_ops_warn_(threshold|lifetime)' || true; ")
    assert "class:hdd[[:space:]].*bluestore_slow_ops_warn_" in preflight
    assert "exit 3; fi; " in preflight
    assert change


def test_rollback_removes_exactly_the_two_overrides():
    assert tune_bluestore_slow_ops_warn_rollback_command(PARAMS) == (
        "ceph config rm osd/class:hdd bluestore_slow_ops_warn_threshold && "
        "ceph config rm osd/class:hdd bluestore_slow_ops_warn_lifetime"
    )


@pytest.mark.parametrize("bad", [
    {"device_class": "hdd; rm -rf /", "threshold": 5, "lifetime_seconds": 3600},
    {"device_class": None, "threshold": 5, "lifetime_seconds": 3600},
    {"device_class": "ssd", "threshold": 0, "lifetime_seconds": 3600},
    {"device_class": "ssd", "threshold": 1001, "lifetime_seconds": 3600},
    {"device_class": "ssd", "threshold": "5", "lifetime_seconds": 3600},
    {"device_class": "ssd", "threshold": True, "lifetime_seconds": 3600},
    {"device_class": "ssd", "threshold": 5, "lifetime_seconds": 59},
    {"device_class": "ssd", "threshold": 5, "lifetime_seconds": 86401},
    {"device_class": "ssd", "threshold": 5},
])
def test_invalid_params_are_rejected(bad):
    with pytest.raises(ExecutorError):
        get_command(ACTION, "10.3.53.1", bad)
    if bad.get("device_class") not in {"hdd", "ssd", "nvme"}:
        with pytest.raises(ExecutorError):
            tune_bluestore_slow_ops_warn_rollback_command(bad)


def test_cephadm_runs_it_inside_the_ceph_runtime():
    command = get_command(ACTION, "10.3.53.1", PARAMS, exec_mode="cephadm")

    assert command.startswith("cephadm shell -- bash -lc ")
    assert ACTION in commands._CEPH_RUNTIME_ACTION_IDS


def test_it_is_a_risky_chat_management_action():
    assert ACTION in gate.VALID_MANAGEMENT_ACTION_IDS
    assert ACTION in chat_client.CHAT_ACTION_IDS
    assert gate.classify_action(ACTION) == ActionClassification.RISKY
    assert chat_client._MANAGEMENT_REQUIRED_PARAMS[ACTION] == ("device_class", "threshold", "lifetime_seconds")
