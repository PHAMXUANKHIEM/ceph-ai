import json
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


def _clear_all_channels(monkeypatch):
    for _, token_field, chat_field in bot._CHANNELS:
        monkeypatch.setattr(bot.settings, token_field, "", raising=False)
        monkeypatch.setattr(bot.settings, chat_field, "", raising=False)


def _configure_channel(monkeypatch, channel: str, *, token="123:ABC", chat_id="-100999"):
    monkeypatch.setattr(bot.settings, f"telegram_{channel}_bot_token", token, raising=False)
    monkeypatch.setattr(bot.settings, f"telegram_{channel}_chat_id", chat_id, raising=False)


# --- _configured_channels() / _known_chat_ids() -----------------------------


def test_configured_channels_empty_when_nothing_set(monkeypatch):
    _clear_all_channels(monkeypatch)
    assert bot._configured_channels() == []


def test_configured_channels_requires_both_token_and_chat_id(monkeypatch):
    _clear_all_channels(monkeypatch)
    monkeypatch.setattr(bot.settings, "telegram_incident_bot_token", "123:ABC", raising=False)
    assert bot._configured_channels() == []  # chat id still blank

    monkeypatch.setattr(bot.settings, "telegram_incident_chat_id", "-100999", raising=False)
    assert bot._configured_channels() == [("incident", "123:ABC", "-100999")]


def test_known_chat_ids_aggregates_every_configured_channel(monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "backup", token="t1", chat_id="-1")
    _configure_channel(monkeypatch, "node", token="t2", chat_id="-2")

    assert bot._known_chat_ids() == {"-1", "-2"}


# --- _notify_pending_actions() — broadcast to every configured channel -----


def test_notify_broadcasts_to_every_configured_channel(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "backup", token="tb", chat_id="-1")
    _configure_channel(monkeypatch, "node", token="tn", chat_id="-2")
    action_id = _pending_action("inc-1")
    calls = []
    monkeypatch.setattr(
        bot,
        "send_telegram_message_with_keyboard",
        lambda token, chat_id, text, buttons: calls.append((token, chat_id)) or (111 if token == "tb" else 222),
    )

    bot._notify_pending_actions()

    assert set(calls) == {("tb", "-1"), ("tn", "-2")}
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert json.loads(action.telegram_message_ids) == {"backup": 111, "node": 222}
        assert action.telegram_notified_at is not None


def test_notify_does_not_resend_to_a_channel_already_notified(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="ti", chat_id="-3")
    action_id = _pending_action("inc-1")
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        action.telegram_message_ids = json.dumps({"incident": 1})
        action.telegram_notified_at = datetime.utcnow()
        session.commit()

    calls = []
    monkeypatch.setattr(bot, "send_telegram_message_with_keyboard", lambda *a: calls.append(a) or 1)

    bot._notify_pending_actions()

    assert calls == []


def test_notify_sends_to_a_channel_configured_after_the_action_already_existed(dashboard_client, monkeypatch):
    """A channel added/fixed AFTER an Action was already broadcast to other
    channels must still pick it up on the next scan — no restart, no "too
    late" window (this is the whole point of "phê duyệt là mặc định của
    mọi kênh đang bật")."""
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="ti", chat_id="-3")
    action_id = _pending_action("inc-1")
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        action.telegram_message_ids = json.dumps({"incident": 1})
        action.telegram_notified_at = datetime.utcnow()
        session.commit()

    _configure_channel(monkeypatch, "node", token="tn", chat_id="-4")
    calls = []
    monkeypatch.setattr(
        bot, "send_telegram_message_with_keyboard", lambda token, chat_id, text, buttons: calls.append(token) or 2
    )

    bot._notify_pending_actions()

    assert calls == ["tn"]
    with db_module.SessionLocal() as session:
        assert json.loads(session.get(Action, action_id).telegram_message_ids) == {"incident": 1, "node": 2}


def test_notify_skips_an_action_that_is_not_pending_approval(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident")
    action_id = _pending_action("inc-1")
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        action.status = ActionStatus.APPROVED.value
        session.commit()

    calls = []
    monkeypatch.setattr(bot, "send_telegram_message_with_keyboard", lambda *a: calls.append(a) or 1)

    bot._notify_pending_actions()

    assert calls == []


def test_notify_one_channel_failure_does_not_block_another_channel(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "backup", token="tb", chat_id="-1")
    _configure_channel(monkeypatch, "node", token="tn", chat_id="-2")
    action_id = _pending_action("inc-1")

    def fake_send(token, chat_id, text, buttons):
        if token == "tb":
            raise bot.TelegramSendError("boom")
        return 999

    monkeypatch.setattr(bot, "send_telegram_message_with_keyboard", fake_send)

    bot._notify_pending_actions()

    with db_module.SessionLocal() as session:
        # node succeeded and was recorded; backup failed and is left out so
        # it's retried on the next scan.
        assert json.loads(session.get(Action, action_id).telegram_message_ids) == {"node": 999}


def test_notify_one_action_failure_does_not_block_another_action(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="ti", chat_id="-3")
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


# --- _handle_callback_query() -----------------------------------------------


def _callback_query(action_id: str, decision: str, *, chat_id="-100999", message_id=555, username="opuser"):
    return {
        "id": "cbid-1",
        "data": f"{decision}:{action_id}",
        "from": {"id": 42, "username": username},
        "message": {"message_id": message_id, "chat": {"id": chat_id}},
    }


def test_handle_callback_approves_and_edits_message(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="123:ABC", chat_id="-100999")
    action_id = _pending_action("inc-1")
    edit_calls = []
    answer_calls = []
    monkeypatch.setattr(
        bot, "edit_telegram_message", lambda token, chat_id, msg_id, text: edit_calls.append((msg_id, text))
    )
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append((cb_id, text))
    )

    bot._handle_callback_query(_callback_query(action_id, "approve"), "123:ABC")

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

    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="123:ABC", chat_id="-100999")
    action_id = _pending_action("inc-1")
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    monkeypatch.setattr(bot, "answer_telegram_callback", lambda *a, **kw: None)

    bot._handle_callback_query(_callback_query(action_id, "approve", username="alice"), "123:ABC")

    with db_module.SessionLocal() as session:
        entry = session.query(AuditEntry).filter_by(action_id=action_id).one()
        assert entry.actor == "telegram:alice"


def test_handle_callback_rejects(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="123:ABC", chat_id="-100999")
    action_id = _pending_action("inc-1")
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    answer_calls = []
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append(text)
    )

    bot._handle_callback_query(_callback_query(action_id, "reject"), "123:ABC")

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.REJECTED.value
    assert answer_calls == ["Đã từ chối"]


def test_handle_callback_acknowledges_action_with_no_command(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "node", token="123:ABC", chat_id="-100999")
    action_id = _pending_action("inc-1", action_id="investigate_manually")
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    answer_calls = []
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append(text)
    )

    bot._handle_callback_query(_callback_query(action_id, "approve"), "123:ABC")

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.EXECUTED.value
    assert answer_calls == ["Đã xác nhận"]


def test_handle_callback_ignores_wrong_chat_id(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="123:ABC", chat_id="-100999")
    action_id = _pending_action("inc-1")
    edit_calls = []
    answer_calls = []
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: edit_calls.append(a))
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append(text)
    )

    bot._handle_callback_query(_callback_query(action_id, "approve", chat_id="-999999"), "123:ABC")

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.PENDING_APPROVAL.value  # untouched
    assert edit_calls == []
    assert answer_calls == ["Không có quyền"]


def test_handle_callback_accepts_chat_id_from_any_configured_channel(dashboard_client, monkeypatch):
    """A callback arriving on the Hardware channel's chat must be trusted
    just as much as one arriving on the Incident channel's — Duyệt/Từ chối
    is a default capability of EVERY configured channel, not scoped to
    "only the channel that owns this Action's category"."""
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="ti", chat_id="-100999")
    _configure_channel(monkeypatch, "node", token="tn", chat_id="-200999")
    action_id = _pending_action("inc-1")
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    monkeypatch.setattr(bot, "answer_telegram_callback", lambda *a, **kw: None)

    bot._handle_callback_query(_callback_query(action_id, "approve", chat_id="-200999"), "tn")

    with db_module.SessionLocal() as session:
        assert session.get(Action, action_id).status == ActionStatus.APPROVED.value


def test_handle_callback_matches_chat_id_across_int_and_str(dashboard_client, monkeypatch):
    # Telegram's real JSON gives chat.id as an int; settings values are
    # plain str from a form field — the comparison must not falsely reject
    # a real match just because of type mismatch.
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="123:ABC", chat_id="123456")
    action_id = _pending_action("inc-1")
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    monkeypatch.setattr(bot, "answer_telegram_callback", lambda *a, **kw: None)

    query = _callback_query(action_id, "approve", chat_id=123456)  # int, not str
    bot._handle_callback_query(query, "123:ABC")

    with db_module.SessionLocal() as session:
        assert session.get(Action, action_id).status == ActionStatus.APPROVED.value


def test_handle_callback_reports_not_found_for_unknown_action(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="123:ABC", chat_id="-100999")
    answer_calls = []
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append(text)
    )

    bot._handle_callback_query(_callback_query("does-not-exist", "approve"), "123:ABC")

    assert answer_calls == ["Không tìm thấy đề xuất này"]


def test_handle_callback_reports_conflict_and_leaves_action_pending(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="123:ABC", chat_id="-100999")
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

    bot._handle_callback_query(_callback_query(action_id, "approve"), "123:ABC")

    assert answer_calls == ["Đang có nâng cấp cụm"]
    with db_module.SessionLocal() as session:
        assert session.get(Action, action_id).status == ActionStatus.PENDING_APPROVAL.value


def test_handle_callback_already_handled_when_double_clicked(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="123:ABC", chat_id="-100999")
    action_id = _pending_action("inc-1")
    with db_module.SessionLocal() as session:
        session.get(Action, action_id).status = ActionStatus.APPROVED.value
        session.commit()
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    answer_calls = []
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append(text)
    )

    bot._handle_callback_query(_callback_query(action_id, "reject"), "123:ABC")

    assert answer_calls == ["Đã được xử lý từ trước"]


def test_handle_callback_ignores_unrecognized_callback_data(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="123:ABC", chat_id="-100999")
    action_id = _pending_action("inc-1")
    query = _callback_query(action_id, "approve")
    query["data"] = "some_other_button:xyz"

    bot._handle_callback_query(query, "123:ABC")  # must not raise

    with db_module.SessionLocal() as session:
        assert session.get(Action, action_id).status == ActionStatus.PENDING_APPROVAL.value
