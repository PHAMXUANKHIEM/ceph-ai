from datetime import datetime

import dashboard.telegram_approval_bot as bot
from shared import db as db_module
from shared.models import Action, ActionClassification, ActionStatus, Incident, IncidentStatus


def _pending_action(incident_id: str, *, action_id: str = "restart_osd_daemon", rationale: str = "stuck OSD") -> str:
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id=incident_id, ceph_code="OSD_DOWN", status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(),
            )
        )
        action = Action(
            incident_id=incident_id,
            action_id=action_id,
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=rationale,
            proposed_command="docker restart ceph-osd-B",
            target_nodes='["10.20.1.83"]',
        )
        session.add(action)
        session.commit()
        return action.id


def _enable(monkeypatch, chat_id="-100999", token="123:ABC"):
    monkeypatch.setattr(bot.settings, "telegram_approval_requests_enabled", True, raising=False)
    monkeypatch.setattr(bot.settings, "telegram_bot_token", token, raising=False)
    monkeypatch.setattr(bot.settings, "telegram_chat_id", chat_id, raising=False)


# --- _configured() -----------------------------------------------------


def test_configured_requires_enabled_and_token_and_chat_id(monkeypatch):
    monkeypatch.setattr(bot.settings, "telegram_approval_requests_enabled", False, raising=False)
    monkeypatch.setattr(bot.settings, "telegram_bot_token", "", raising=False)
    monkeypatch.setattr(bot.settings, "telegram_chat_id", "", raising=False)
    assert bot._configured() is False

    _enable(monkeypatch)
    assert bot._configured() is True

    monkeypatch.setattr(bot.settings, "telegram_bot_token", "", raising=False)
    assert bot._configured() is False


# --- _notify_pending_actions() ------------------------------------------


def test_notify_sends_message_and_stamps_action(dashboard_client, monkeypatch):
    _enable(monkeypatch)
    action_id = _pending_action("inc-1")
    calls = []
    monkeypatch.setattr(
        bot,
        "send_telegram_message_with_keyboard",
        lambda token, chat_id, text, buttons: calls.append((token, chat_id, text, buttons)) or 555,
    )

    bot._notify_pending_actions()

    assert len(calls) == 1
    token, chat_id, text, buttons = calls[0]
    assert token == "123:ABC"
    assert chat_id == "-100999"
    assert "restart_osd_daemon" in text
    assert "stuck OSD" in text
    assert "docker restart ceph-osd-B" in text
    assert buttons == [("✅ Duyệt", f"approve:{action_id}"), ("❌ Từ chối", f"reject:{action_id}")]

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.telegram_message_id == 555
        assert action.telegram_notified_at is not None


def test_notify_does_not_resend_an_already_notified_action(dashboard_client, monkeypatch):
    _enable(monkeypatch)
    action_id = _pending_action("inc-1")
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        action.telegram_message_id = 1
        action.telegram_notified_at = datetime.utcnow()
        session.commit()

    calls = []
    monkeypatch.setattr(
        bot, "send_telegram_message_with_keyboard", lambda *a: calls.append(a) or 1
    )

    bot._notify_pending_actions()

    assert calls == []


def test_notify_skips_an_action_that_is_not_pending_approval(dashboard_client, monkeypatch):
    _enable(monkeypatch)
    action_id = _pending_action("inc-1")
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        action.status = ActionStatus.APPROVED.value
        session.commit()

    calls = []
    monkeypatch.setattr(
        bot, "send_telegram_message_with_keyboard", lambda *a: calls.append(a) or 1
    )

    bot._notify_pending_actions()

    assert calls == []


def test_notify_one_send_failure_does_not_block_another_action(dashboard_client, monkeypatch):
    _enable(monkeypatch)
    failing_id = _pending_action("inc-fail")
    ok_id = _pending_action("inc-ok")

    def fake_send(token, chat_id, text, buttons):
        if failing_id in text:
            raise bot.TelegramSendError("boom")
        return 999

    monkeypatch.setattr(bot, "send_telegram_message_with_keyboard", fake_send)

    bot._notify_pending_actions()

    with db_module.SessionLocal() as session:
        assert session.get(Action, failing_id).telegram_notified_at is None
        assert session.get(Action, ok_id).telegram_notified_at is not None


# --- _handle_callback_query() -------------------------------------------


def _callback_query(action_id: str, decision: str, *, chat_id="-100999", message_id=555, username="opuser"):
    return {
        "id": "cbid-1",
        "data": f"{decision}:{action_id}",
        "from": {"id": 42, "username": username},
        "message": {"message_id": message_id, "chat": {"id": chat_id}},
    }


def test_handle_callback_approves_and_edits_message(dashboard_client, monkeypatch):
    _enable(monkeypatch)
    action_id = _pending_action("inc-1")
    edit_calls = []
    answer_calls = []
    monkeypatch.setattr(
        bot, "edit_telegram_message", lambda token, chat_id, msg_id, text: edit_calls.append((msg_id, text))
    )
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append((cb_id, text))
    )

    bot._handle_callback_query(_callback_query(action_id, "approve"))

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.APPROVED.value

    assert answer_calls == [("cbid-1", "Đã duyệt")]
    assert len(edit_calls) == 1
    msg_id, text = edit_calls[0]
    assert msg_id == 555
    assert "ĐÃ DUYỆT" in text


def test_handle_callback_records_telegram_actor_in_audit_trail(dashboard_client, monkeypatch):
    from shared.models import AuditEntry

    _enable(monkeypatch)
    action_id = _pending_action("inc-1")
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    monkeypatch.setattr(bot, "answer_telegram_callback", lambda *a, **kw: None)

    bot._handle_callback_query(_callback_query(action_id, "approve", username="alice"))

    with db_module.SessionLocal() as session:
        entry = session.query(AuditEntry).filter_by(action_id=action_id).one()
        assert entry.actor == "telegram:alice"


def test_handle_callback_rejects(dashboard_client, monkeypatch):
    _enable(monkeypatch)
    action_id = _pending_action("inc-1")
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    answer_calls = []
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append(text)
    )

    bot._handle_callback_query(_callback_query(action_id, "reject"))

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.REJECTED.value
    assert answer_calls == ["Đã từ chối"]


def test_handle_callback_acknowledges_action_with_no_command(dashboard_client, monkeypatch):
    _enable(monkeypatch)
    action_id = _pending_action("inc-1", action_id="investigate_manually")
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    answer_calls = []
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append(text)
    )

    bot._handle_callback_query(_callback_query(action_id, "approve"))

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.EXECUTED.value
    assert answer_calls == ["Đã xác nhận"]


def test_handle_callback_ignores_wrong_chat_id(dashboard_client, monkeypatch):
    _enable(monkeypatch)
    action_id = _pending_action("inc-1")
    edit_calls = []
    answer_calls = []
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: edit_calls.append(a))
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append(text)
    )

    bot._handle_callback_query(_callback_query(action_id, "approve", chat_id="-999999"))

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.PENDING_APPROVAL.value  # untouched
    assert edit_calls == []
    assert answer_calls == ["Không có quyền"]


def test_handle_callback_matches_chat_id_across_int_and_str(dashboard_client, monkeypatch):
    # Telegram's real JSON gives chat.id as an int; settings.telegram_chat_id
    # is a plain str from a form field — the comparison must not falsely
    # reject a real match just because of type mismatch.
    _enable(monkeypatch, chat_id="123456")
    action_id = _pending_action("inc-1")
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    monkeypatch.setattr(bot, "answer_telegram_callback", lambda *a, **kw: None)

    query = _callback_query(action_id, "approve", chat_id=123456)  # int, not str
    bot._handle_callback_query(query)

    with db_module.SessionLocal() as session:
        assert session.get(Action, action_id).status == ActionStatus.APPROVED.value


def test_handle_callback_reports_not_found_for_unknown_action(dashboard_client, monkeypatch):
    _enable(monkeypatch)
    answer_calls = []
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append(text)
    )

    bot._handle_callback_query(_callback_query("does-not-exist", "approve"))

    assert answer_calls == ["Không tìm thấy đề xuất này"]


def test_handle_callback_reports_conflict_and_leaves_action_pending(dashboard_client, monkeypatch):
    _enable(monkeypatch)
    action_id = _pending_action("inc-1")
    monkeypatch.setattr(
        bot,
        "approve_action_core",
        lambda *a: (_ for _ in ()).throw(bot.ActionConflictError("Đang có nâng cấp cụm")),
    )
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    answer_calls = []
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append(text)
    )

    bot._handle_callback_query(_callback_query(action_id, "approve"))

    assert answer_calls == ["Đang có nâng cấp cụm"]
    with db_module.SessionLocal() as session:
        assert session.get(Action, action_id).status == ActionStatus.PENDING_APPROVAL.value


def test_handle_callback_already_handled_when_double_clicked(dashboard_client, monkeypatch):
    _enable(monkeypatch)
    action_id = _pending_action("inc-1")
    with db_module.SessionLocal() as session:
        session.get(Action, action_id).status = ActionStatus.APPROVED.value
        session.commit()
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    answer_calls = []
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append(text)
    )

    bot._handle_callback_query(_callback_query(action_id, "reject"))

    assert answer_calls == ["Đã được xử lý từ trước"]


def test_handle_callback_ignores_unrecognized_callback_data(dashboard_client, monkeypatch):
    _enable(monkeypatch)
    action_id = _pending_action("inc-1")
    query = _callback_query(action_id, "approve")
    query["data"] = "some_other_button:xyz"

    bot._handle_callback_query(query)  # must not raise

    with db_module.SessionLocal() as session:
        assert session.get(Action, action_id).status == ActionStatus.PENDING_APPROVAL.value
