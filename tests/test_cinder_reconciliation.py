import json

import pytest

from worker.executor import cinder_reconciliation
from worker.executor.ssh_executor import ExecutorError


PARAMS = {
    "volume_id": "12345678-1234-4123-8123-1234567890ab",
    "server_id": "abcdefab-1234-4123-8123-1234567890ab",
}


def _output(attachments):
    return json.dumps({"id": PARAMS["volume_id"], "attachments": attachments})


def test_cinder_attach_and_detach_post_check_expected_server_state():
    cinder_reconciliation.reconcile(
        "cinder_attach_volume", PARAMS,
        _output([{"server_id": PARAMS["server_id"]}]),
    )
    cinder_reconciliation.reconcile("cinder_detach_volume", PARAMS, _output([]))


@pytest.mark.parametrize(
    ("action_id", "attachments"),
    [
        ("cinder_attach_volume", []),
        ("cinder_detach_volume", [{"server_id": PARAMS["server_id"]}]),
    ],
)
def test_cinder_post_check_fails_closed_on_state_mismatch(action_id, attachments):
    with pytest.raises(ExecutorError, match="lệch trạng thái"):
        cinder_reconciliation.reconcile(action_id, PARAMS, _output(attachments))


def test_cinder_post_check_rejects_malformed_output():
    with pytest.raises(ExecutorError, match="JSON"):
        cinder_reconciliation.reconcile("cinder_attach_volume", PARAMS, "not-json")
