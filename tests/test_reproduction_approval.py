"""FL6.3: operators approve AI reproduction proposals on Telegram."""

import asyncio
import json

import pytest

from shared import reproduction_approval as ra

PID = "repro-0123456789"


def _record(tmp_path, status="PROPOSED", kind="stop_osd"):
    record = {"id": PID, "family": "network_heartbeat", "status": status,
              "evidence_examples": ["Chưa đủ bằng chứng: osd.1 mất heartbeat"],
              "proposal": {"fault_kind": kind, "expected_health_codes": ["OSD_DOWN", "PG_DEGRADED"],
                           "cause": "Một OSD daemon dừng nên MON đánh dấu down.",
                           "acceptable_action_ids": ["investigate_manually", "restart_osd_daemon"], "max_seconds": 300,
                           "citations": ["https://docs.ceph.com/en/latest/rados/operations/health-checks/#osd-down"]}}
    (tmp_path / f"{PID}.json").write_text(json.dumps(record))
    return record


def test_a_decision_is_written_once_with_who_and_when(tmp_path):
    _record(tmp_path)

    record = ra.decide(PID, approve=True, actor="telegram-chat:1 (op)", directory=tmp_path)

    assert record["status"] == "APPROVED" and record["decided_by"] == "telegram-chat:1 (op)"
    assert json.loads((tmp_path / f"{PID}.json").read_text())["status"] == "APPROVED"
    with pytest.raises(ra.ReproductionError, match="đã ở trạng thái APPROVED"):
        ra.decide(PID, approve=False, actor="x", directory=tmp_path)


@pytest.mark.parametrize(("status", "kind", "reason"), [
    ("REJECTED", "stop_osd", "trạng thái REJECTED"),
    ("PROPOSED", "NEW_KIND_NEEDED", "không còn trong bộ chạy"),
])
def test_rejected_or_unrunnable_proposals_cannot_be_approved(tmp_path, status, kind, reason):
    _record(tmp_path, status=status, kind=kind)

    with pytest.raises(ra.ReproductionError, match=reason):
        ra.decide(PID, approve=True, actor="x", directory=tmp_path)


@pytest.mark.parametrize("data", ["flrepro:ok:../../etc/passwd", "flrepro:ok:repro-XYZ", "flrepro:ok:"])
def test_button_data_must_be_a_proposal_id(data):
    with pytest.raises(ra.ReproductionError):
        ra.parse(data, ra.APPROVE_PREFIX)


def test_the_card_shows_what_will_run_and_the_safety_limits(tmp_path):
    text = ra.card_text(_record(tmp_path))

    assert "network_heartbeat" in text and "stop_osd" in text and "OSD_DOWN" in text
    assert "health-checks/#osd-down" in text and "chỉ chạy trên cụm lab".lower() in text.lower()
    assert [data for _label, data in ra.buttons(PID)] == [f"flrepro:ok:{PID}", f"flrepro:skip:{PID}"]


def _callback(data):
    return {"data": data, "from": {"id": 42, "username": "op"},
            "message": {"chat": {"id": -100}, "message_id": 7, "text": "🧪 Đề xuất tái hiện lỗi"}}


def test_only_operators_decide_from_telegram(monkeypatch, tmp_path):
    from dashboard import telegram_chat

    _record(tmp_path)
    monkeypatch.setattr(ra, "PROPOSALS_DIR", tmp_path)
    monkeypatch.setattr(telegram_chat, "is_allowed_callback", lambda *a: True)
    monkeypatch.setattr(telegram_chat, "_sender_can_use_full_access", lambda update: False)

    reply = asyncio.run(telegram_chat.handle_callback(_callback(f"flrepro:ok:{PID}"), "bot"))

    assert "operator" in reply and json.loads((tmp_path / f"{PID}.json").read_text())["status"] == "PROPOSED"


def test_an_operator_tap_approves_and_marks_the_card(monkeypatch, tmp_path):
    from dashboard import telegram_chat

    _record(tmp_path)
    edits = []
    monkeypatch.setattr(ra, "PROPOSALS_DIR", tmp_path)
    monkeypatch.setattr(telegram_chat, "is_allowed_callback", lambda *a: True)
    monkeypatch.setattr(telegram_chat, "_sender_can_use_full_access", lambda update: True)
    monkeypatch.setattr(telegram_chat, "edit_telegram_message", lambda *args: edits.append(args))

    reply = asyncio.run(telegram_chat.handle_callback(_callback(f"flrepro:ok:{PID}"), "bot"))

    assert reply == f"Đã cho phép {PID}." and "Cho phép bởi op" in edits[0][3]
    assert json.loads((tmp_path / f"{PID}.json").read_text())["status"] == "APPROVED"
    assert f"flrepro:ok:{PID}".startswith(telegram_chat.CALLBACK_PREFIXES)  # the gateway routes it here


def test_the_proposal_script_posts_a_card_or_a_new_kind_note(monkeypatch, tmp_path):
    from scripts.lab import propose_reproduction as cli

    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_chatbox_bot_token", "t")
    monkeypatch.setattr(settings, "telegram_chatbox_chat_id", "c")
    cards, notes = [], []
    record = _record(tmp_path)
    assert cli.announce(record, sender=lambda *a: cards.append(a), plain_sender=lambda *a: notes.append(a))
    new_kind = {**record, "status": "NEW_KIND_REQUESTED",
                "proposal": {**record["proposal"], "new_kind_request": {"name": "slow_bluestore"}}}
    assert cli.announce(new_kind, sender=lambda *a: cards.append(a), plain_sender=lambda *a: notes.append(a))
    assert not cli.announce({**record, "status": "REJECTED"})

    assert len(cards) == 1 and cards[0][3] == ra.buttons(PID)
    assert len(notes) == 1 and "slow_bluestore" in notes[0][2]
