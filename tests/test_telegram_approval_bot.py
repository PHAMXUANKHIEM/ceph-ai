import json
from datetime import datetime, timedelta

import dashboard.telegram_approval_bot as bot
from shared import db as db_module
from shared.models import Action, ActionClassification, ActionStatus, Cluster, Incident, IncidentStatus


def _pending_action(
    incident_id: str, *, action_id: str = "restart_osd_daemon", rationale: str = "stuck OSD", cluster_id: str | None = None
) -> str:
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id=incident_id, ceph_code="OSD_DOWN", status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(), cluster_id=cluster_id,
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


def _grace_action(incident_id: str) -> str:
    with db_module.SessionLocal() as session:
        incident = Incident(
            id=incident_id, ceph_code="MON_CLOCK_SKEW",
            status=IncidentStatus.GRACE_PENDING.value, detected_at=datetime.utcnow(),
        )
        session.add(incident); session.flush()
        action = Action(
            incident_id=incident.id, action_id="resync_ntp",
            classification=ActionClassification.SAFE.value,
            status=ActionStatus.GRACE_PENDING.value,
            grace_until=datetime.utcnow() + timedelta(minutes=5),
            target_nodes='["mon-a"]',
        )
        session.add(action); session.commit()
        return action.id


def _clear_all_channels(monkeypatch):
    for _, token_field, chat_field, _enabled_field in bot._CHANNELS:
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


def test_configured_channels_excludes_a_disabled_channel(monkeypatch):
    # 2026-08-07: a channel with a fully saved token+chat id but flipped
    # off (Alert Telegram page's "Tắt kênh này") must not receive Duyệt/Từ
    # chối broadcasts either -- see config.settings.py's `telegram_*_enabled`
    # docstring.
    _clear_all_channels(monkeypatch)
    monkeypatch.setattr(bot.settings, "telegram_incident_bot_token", "123:ABC", raising=False)
    monkeypatch.setattr(bot.settings, "telegram_incident_chat_id", "-100999", raising=False)
    monkeypatch.setattr(bot.settings, "telegram_incident_enabled", False, raising=False)

    assert bot._configured_channels() == []


def test_known_chat_ids_aggregates_every_configured_channel(monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "backup", token="t1", chat_id="-1")
    _configure_channel(monkeypatch, "node", token="t2", chat_id="-2")

    assert bot._known_chat_ids() == {"-1", "-2"}


# --- _notify_pending_actions() — broadcast to every configured channel -----


def test_action_message_is_compact_and_shows_proposed_solution(dashboard_client):
    action_id = _pending_action("inc-message", rationale="Khởi động lại OSD để phục hồi quorum.")
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        incident = session.get(Incident, "inc-message")
        incident.diagnosis_text = "OSD mất kết nối. " * 80
        text = bot._action_message_text(action, incident, session)

    assert "🔧 Giải pháp đề xuất: Khởi động lại OSD" in text
    assert "💻 Lệnh: docker restart ceph-osd-B" in text
    assert f"🆔 {action_id[:8]}" in text
    assert len(text) < 900


def test_action_message_has_solution_fallback_when_rationale_missing(dashboard_client):
    action_id = _pending_action("inc-fallback", rationale="")
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        incident = session.get(Incident, "inc-fallback")
        text = bot._action_message_text(action, incident, session)

    assert "🔧 Giải pháp đề xuất: Khởi động lại daemon OSD bị lỗi." in text


def test_pool_application_action_has_three_choice_buttons(dashboard_client):
    action_id = _pending_action("inc-pool-app")
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        action.action_id = "enable_pool_application"
        action.action_params = json.dumps({"pool_name": "rbd-data"})
        session.commit()
        buttons = bot._approval_keyboard(action)

    assert buttons[:3] == [
        ("💾 RBD", f"poolapp:rbd:{action_id}"),
        ("📁 CephFS", f"poolapp:cephfs:{action_id}"),
        ("🌐 RGW", f"poolapp:rgw:{action_id}"),
    ]


def test_legacy_pool_warning_has_three_choice_buttons(dashboard_client):
    action_id = _pending_action("inc-pool-legacy", action_id="investigate_manually")
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        incident = session.get(Incident, "inc-pool-legacy")
        incident.ceph_code = "POOL_APP_NOT_ENABLED"
        incident.diagnosis_text = "pool 'images' chưa được gắn nhãn ứng dụng"
        session.commit()
        buttons = bot._approval_keyboard(action, incident)

    assert buttons[:3] == [
        ("💾 RBD", f"poolapp:rbd:{action_id}"),
        ("📁 CephFS", f"poolapp:cephfs:{action_id}"),
        ("🌐 RGW", f"poolapp:rgw:{action_id}"),
    ]


def test_cluster_approval_goes_only_to_incident_channel(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "backup", token="tb", chat_id="-1")
    _configure_channel(monkeypatch, "incident", token="ti", chat_id="-2")
    _configure_channel(monkeypatch, "node", token="tn", chat_id="-3")
    action_id = _pending_action("inc-1")
    calls = []
    monkeypatch.setattr(
        bot,
        "send_telegram_message_with_keyboard",
        lambda token, chat_id, text, buttons: calls.append((token, chat_id)) or 222,
    )

    bot._notify_pending_actions()

    assert calls == [("ti", "-2")]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert json.loads(action.telegram_message_ids) == {"incident": 222}
        assert action.telegram_notified_at is not None


def test_volume_perf_approval_goes_only_to_incident_channel(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "backup", token="tb", chat_id="-1")
    _configure_channel(monkeypatch, "incident", token="ti", chat_id="-2")
    _configure_channel(monkeypatch, "node", token="tn", chat_id="-3")
    action_id = _pending_action("inc-volume-perf", action_id="volume_perf_sweep")
    calls = []
    monkeypatch.setattr(
        bot,
        "send_telegram_message_with_keyboard",
        lambda token, chat_id, text, buttons: calls.append((token, chat_id)) or 222,
    )

    bot._notify_pending_actions()

    assert calls == [("ti", "-2")]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert json.loads(action.telegram_message_ids) == {"incident": 222}


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


def test_grace_notification_has_countdown_and_cancel_only(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="ti", chat_id="-3")
    action_id = _grace_action("inc-grace-notify")
    calls = []
    monkeypatch.setattr(
        bot, "send_telegram_message_with_keyboard",
        lambda token, chat_id, text, buttons: calls.append((text, buttons)) or 901,
    )

    bot._notify_pending_actions()

    assert len(calls) == 1
    text, buttons = calls[0]
    assert "Autopilot lab sẽ chạy" in text
    assert buttons == [("🛑 Hủy Autopilot", f"cancelgrace:{action_id}")]
    with db_module.SessionLocal() as session:
        assert json.loads(session.get(Action, action_id).telegram_message_ids) == {"incident": 901}


def test_notify_does_not_backfill_cluster_action_to_unrelated_channel(dashboard_client, monkeypatch):
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

    assert calls == []
    with db_module.SessionLocal() as session:
        assert json.loads(session.get(Action, action_id).telegram_message_ids) == {"incident": 1}


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


def test_notify_ignores_unrelated_configured_channels(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "backup", token="tb", chat_id="-1")
    _configure_channel(monkeypatch, "incident", token="ti", chat_id="-3")
    _configure_channel(monkeypatch, "node", token="tn", chat_id="-2")
    action_id = _pending_action("inc-1")

    def fake_send(token, chat_id, text, buttons):
        assert token == "ti"
        return 999

    monkeypatch.setattr(bot, "send_telegram_message_with_keyboard", fake_send)

    bot._notify_pending_actions()

    with db_module.SessionLocal() as session:
        assert json.loads(session.get(Action, action_id).telegram_message_ids) == {"incident": 999}


def test_notify_one_action_failure_does_not_block_another_action(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="ti", chat_id="-3")
    failing_id = _pending_action("inc-fail")
    ok_id = _pending_action("inc-ok")

    def fake_send(token, chat_id, text, buttons):
        if failing_id[:8] in text:
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


def test_handle_callback_cancels_grace_and_records_telegram_actor(dashboard_client, monkeypatch):
    from shared.models import AuditEntry

    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="123:ABC", chat_id="-100999")
    action_id = _grace_action("inc-grace-callback")
    edits, answers = [], []
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *args: edits.append(args))
    monkeypatch.setattr(
        bot, "answer_telegram_callback",
        lambda token, callback_id, text=None: answers.append(text),
    )

    bot._handle_callback_query(
        _callback_query(action_id, "cancelgrace", username="alice"), "123:ABC",
    )

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.REJECTED.value
        assert action.cancelled_by == "telegram:alice"
        entry = session.query(AuditEntry).filter_by(action_id=action_id).one()
        assert entry.actor == "telegram:alice"
    assert answers == ["Đã hủy Autopilot"]
    assert "ĐÃ HỦY AUTOPILOT" in edits[0][3]


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


def test_handle_callback_converts_legacy_pool_choice_to_executable_action(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="123:ABC", chat_id="-100999")
    action_id = _pending_action("inc-pool-choice", action_id="investigate_manually")
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "inc-pool-choice")
        incident.ceph_code = "POOL_APP_NOT_ENABLED"
        incident.diagnosis_text = "pool 'images' chưa được gắn nhãn ứng dụng"
        session.commit()
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    monkeypatch.setattr(bot, "answer_telegram_callback", lambda *a, **kw: None)

    bot._handle_callback_query(_callback_query(action_id, "poolapp:rbd"), "123:ABC")

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.action_id == "enable_pool_application"
        assert action.status == ActionStatus.APPROVED.value
        assert json.loads(action.action_params) == {"pool_name": "images", "app_name": "rbd"}
        assert action.proposed_command == "ceph osd pool application enable images rbd --yes-i-really-mean-it"


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


# --- Multi-tenant remediation Phase 2: per-cluster channel scoping ---------
#
# `channels_for_incident()` narrows delivery/trust to a non-default
# cluster's OWN configured channel instead of the 3 global ones -- these
# are the core security-property tests for that narrowing (see this
# session's own plan doc, "Verification" section).


def _make_observed_cluster(session, *, telegram_bot_token="", telegram_chat_id="", telegram_enabled=True) -> str:
    cluster = Cluster(
        name="cluster-b",
        ceph_mon_nodes="10.30.1.10",
        ssh_user="root",
        ssh_key_path="/root/.ssh/key",
        ceph_exec_mode="docker",
        is_default=False,
        is_active=True,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        telegram_enabled=telegram_enabled,
    )
    session.add(cluster)
    session.commit()
    session.refresh(cluster)
    return cluster.id


def test_notify_routes_non_default_cluster_to_its_own_channel_only(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="global-token", chat_id="-1")
    with db_module.SessionLocal() as session:
        cluster_id = _make_observed_cluster(
            session, telegram_bot_token="cluster-token", telegram_chat_id="-500"
        )
    action_id = _pending_action("inc-cluster-b", cluster_id=cluster_id)
    calls = []
    monkeypatch.setattr(
        bot,
        "send_telegram_message_with_keyboard",
        lambda token, chat_id, text, buttons: calls.append((token, chat_id)) or 111,
    )

    bot._notify_pending_actions()

    # Only the cluster's own channel -- never the global "incident" one,
    # even though it's fully configured and would have covered this action
    # before Phase 2.
    assert calls == [("cluster-token", "-500")]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert json.loads(action.telegram_message_ids) == {f"cluster:{cluster_id}": 111}


def test_notify_sends_nothing_for_non_default_cluster_without_own_channel(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="global-token", chat_id="-1")
    with db_module.SessionLocal() as session:
        cluster_id = _make_observed_cluster(session)  # no telegram fields set
    _pending_action("inc-cluster-b", cluster_id=cluster_id)
    calls = []
    monkeypatch.setattr(
        bot, "send_telegram_message_with_keyboard", lambda *a: calls.append(a)
    )

    bot._notify_pending_actions()

    # Narrowed to "nothing", not "fall through to the 3 global channels".
    assert calls == []


def test_handle_callback_rejects_global_chat_id_for_a_clusters_own_action(dashboard_client, monkeypatch):
    """The actual security property Phase 2 exists to add: a chat id that IS
    a legitimately configured channel (the global "incident" one) must NOT
    be able to approve an action belonging to a DIFFERENT cluster that has
    its own channel."""
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="global-token", chat_id="-1")
    with db_module.SessionLocal() as session:
        cluster_id = _make_observed_cluster(
            session, telegram_bot_token="cluster-token", telegram_chat_id="-500"
        )
    action_id = _pending_action("inc-cluster-b", cluster_id=cluster_id)
    answer_calls = []
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append(text)
    )
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: (_ for _ in ()).throw(AssertionError("must not edit")))

    # Callback arrives via the GLOBAL "incident" channel's own chat id.
    bot._handle_callback_query(_callback_query(action_id, "approve", chat_id="-1"), "global-token")

    assert answer_calls == ["Không có quyền"]
    with db_module.SessionLocal() as session:
        assert session.get(Action, action_id).status == ActionStatus.PENDING_APPROVAL.value


def test_handle_callback_accepts_the_clusters_own_chat_id(dashboard_client, monkeypatch):
    _clear_all_channels(monkeypatch)
    _configure_channel(monkeypatch, "incident", token="global-token", chat_id="-1")
    with db_module.SessionLocal() as session:
        cluster_id = _make_observed_cluster(
            session, telegram_bot_token="cluster-token", telegram_chat_id="-500"
        )
    action_id = _pending_action("inc-cluster-b", cluster_id=cluster_id)
    monkeypatch.setattr(bot, "edit_telegram_message", lambda *a: None)
    answer_calls = []
    monkeypatch.setattr(
        bot, "answer_telegram_callback", lambda token, cb_id, text=None: answer_calls.append(text)
    )

    bot._handle_callback_query(_callback_query(action_id, "approve", chat_id="-500"), "cluster-token")

    assert answer_calls == ["Đã duyệt"]
    with db_module.SessionLocal() as session:
        assert session.get(Action, action_id).status == ActionStatus.APPROVED.value
