"""The lab Telegram path is separate from ordinary chat and fails closed."""

from __future__ import annotations

import json
import asyncio
import fcntl
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from dashboard import failure_lab_telegram as lab
from shared import reproduction_approval as approvals
from shared.failure_lab_chat import LabChatRefused, card_text, proposal_from_answer
from shared.failure_lab_fault import FaultRefused, _exclusive, scenario_from_proposal
from worker import failure_lab_telegram_runner as runner


def _record() -> dict:
    return proposal_from_answer(
        "hãy mô phỏng một OSD down", json.dumps({"scenario_id": "osd_down_fault", "reason": "OSD down"}),
        actor="telegram-lab:77", cluster_id="cluster-1", cluster_name="CS-STAGING", fsid="lab-fsid",
        channel_id="channel-1",
    )


def test_ai_only_selects_a_reviewed_scenario():
    record = proposal_from_answer(
        "OSD down", json.dumps({"scenario_id": "osd_down_fault", "reason": "mô phỏng",
                               "command": "rm -rf /", "cluster_id": "production", "max_seconds": 99999}),
        actor="operator", cluster_id="lab", cluster_name="Lab", fsid="pinned", channel_id="channel",
    )
    assert record["cluster_id"] == "lab" and record["fsid"] == "pinned"
    assert record["proposal"]["max_seconds"] == 300
    assert "command" not in record["proposal"]
    assert "FSID pinned" in card_text(record)
    with pytest.raises(LabChatRefused):
        proposal_from_answer("reboot all", '{"scenario_id": "arbitrary_shell"}', actor="operator",
                             cluster_id="lab", cluster_name="Lab", fsid="pinned", channel_id="channel")


def test_runner_rechecks_exact_catalog_scenario():
    record = _record()
    fault, _replay = scenario_from_proposal(record)
    assert fault.kind == "stop_osd"
    record["family"] = "osd_nearfull_fault"
    with pytest.raises(FaultRefused):
        scenario_from_proposal(record)


def test_lab_messages_require_exact_bot_chat_and_explicit_operator(monkeypatch):
    monkeypatch.setattr(lab, "destination", lambda: ("channel", "lab-bot", "-100"))
    monkeypatch.setattr(lab.settings, "telegram_chatbox_full_access_user_ids", "77", raising=False)
    message = {"chat": {"id": -100}, "from": {"id": 77, "is_bot": False}}
    assert lab.is_lab_message(message, "lab-bot")
    assert not lab.is_lab_message(message, "production-bot")
    assert not lab.is_lab_message({**message, "from": {"id": 78}}, "lab-bot")
    assert not lab.is_lab_message({**message, "from": {"id": 77, "is_bot": True}}, "lab-bot")


def test_approval_rechecks_binding_and_fault_gate(monkeypatch, tmp_path):
    record = _record()
    approvals.create(record, tmp_path)
    monkeypatch.setattr(approvals, "PROPOSALS_DIR", tmp_path)
    monkeypatch.setattr(lab, "destination", lambda: ("channel-1", "lab-bot", "-100"))
    monkeypatch.setattr(lab.settings, "telegram_chatbox_full_access_user_ids", "77", raising=False)
    config = SimpleNamespace(cluster_id="cluster-1", fsid="lab-fsid", fault_enabled=False)
    monkeypatch.setattr(lab.failure_lab_config, "load", lambda: config)
    monkeypatch.setattr(lab, "edit_telegram_message", lambda *args: None)
    callback = {"data": f"{lab.APPROVE_PREFIX}{record['id']}", "from": {"id": 77},
                "message": {"chat": {"id": -100}, "message_id": 10, "text": "proposal"}}
    assert "Chưa bật" in lab.handle_callback(callback, "lab-bot")
    assert approvals.load(record["id"], tmp_path)["status"] == "PROPOSED"
    config.fault_enabled = True
    config.fsid = "changed"
    assert "đã đổi" in lab.handle_callback(callback, "lab-bot")
    config.fsid = "lab-fsid"
    assert "Đã duyệt" in lab.handle_callback(callback, "lab-bot")
    assert approvals.load(record["id"], tmp_path)["status"] == "APPROVED"
    assert "đã ở trạng thái" in lab.handle_callback(callback, "lab-bot")


def test_expired_lab_card_cannot_be_approved(tmp_path):
    record = _record()
    approvals.create(record, tmp_path)
    expired = datetime.fromisoformat(record["expires_at"]) + timedelta(seconds=1)
    with pytest.raises(approvals.ReproductionError, match="hết hạn"):
        approvals.decide(record["id"], approve=True, actor="operator", directory=tmp_path, now=expired)


def test_worker_refuses_changed_binding_without_starting_runner(monkeypatch, tmp_path):
    record = _record()
    record.update(status="APPROVED")
    approvals.create(record, tmp_path)
    monkeypatch.setattr(approvals, "PROPOSALS_DIR", tmp_path)
    monkeypatch.setattr(runner, "_binding_valid", lambda _record: False)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("runner started"))
    assert runner.run_one()
    assert approvals.load(record["id"], tmp_path)["status"] == "FAILED"


def test_worker_forwards_shutdown_to_active_fault_runner(monkeypatch):
    state = {"terminated": False}

    class Process:
        def poll(self):
            return None

        def terminate(self):
            state["terminated"] = True

    monkeypatch.setattr(runner, "_active_process", Process())
    runner.terminate_active()
    assert state["terminated"]


def test_worker_rejects_old_approval_before_database_lookup(monkeypatch):
    record = _record()
    record["decided_at"] = (datetime.fromisoformat(record["created_at"]) - timedelta(hours=1)).isoformat()
    monkeypatch.setattr(runner.db, "SessionLocal", lambda: pytest.fail("database queried for expired approval"))
    assert not runner._binding_valid(record)


def test_worker_only_picks_approved_lab_telegram_requests(tmp_path):
    record = _record()
    record["status"] = "APPROVED"
    approvals.create(record, tmp_path)
    old = dict(record, id="repro-0000000001", source="nightly")
    approvals.create(old, tmp_path)
    assert approvals.next_approved_telegram(tmp_path) == record["id"]


def test_lab_message_uses_ai_preview_but_does_not_run_fault(monkeypatch):
    record = _record()
    cards = []
    monkeypatch.setattr(lab, "destination", lambda: ("channel-1", "lab-bot", "-100"))
    monkeypatch.setattr(lab.settings, "telegram_chatbox_full_access_user_ids", "77", raising=False)
    monkeypatch.setattr(lab, "_create_request", lambda text, actor, channel: record)
    monkeypatch.setattr(lab.failure_lab_config, "load", lambda: SimpleNamespace(cluster_id="cluster-1"))
    monkeypatch.setattr(lab, "send_telegram_message_with_keyboard", lambda *args: cards.append(args))
    monkeypatch.setattr(runner, "run_one", lambda: pytest.fail("execution started before approval"))
    asyncio.run(lab._handle_message({"text": "/lab mô phỏng OSD down", "chat": {"id": -100},
                                     "from": {"id": 77}}, "lab-bot"))
    assert len(cards) == 1
    assert "CS-STAGING" in cards[0][2]
    assert cards[0][3][0][1] == f"{lab.APPROVE_PREFIX}{record['id']}"


def test_listener_includes_lab_bot_without_duplicate_poller(monkeypatch):
    from dashboard import telegram_approval_bot as gateway

    monkeypatch.setattr(gateway, "_configured_channels", lambda: [])
    monkeypatch.setattr(gateway, "_cluster_tokens_cached", lambda: set())
    monkeypatch.setattr(gateway.telegram_chat, "configured_token", lambda: "shared-bot")
    monkeypatch.setattr(gateway.failure_lab_telegram, "configured_token", lambda: "shared-bot")
    assert gateway._all_configured_tokens() == {"shared-bot"}


def test_active_fault_blocks_deploy_and_worker_restart(tmp_path, monkeypatch):
    from scripts.deploy import container_restart_helper as helper

    monkeypatch.setattr(helper, "LAB_DIR", str(tmp_path))
    with _exclusive(tmp_path):
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        with helper._lab_restart_guard("worker") as allowed:
            assert not allowed
    with helper._lab_restart_guard("worker") as allowed:
        assert allowed
