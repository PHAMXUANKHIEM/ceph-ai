import json

import dashboard.routes.chat as chat_module
from shared import audit
from shared import db as db_module
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    AuditEntry,
    ChatMessage,
    Incident,
    IncidentStatus,
)

# Matches tests/conftest.py's TEST_CEPH_MON_NODES/TEST_CEPH_OSD_NODES.
A_MON_HOST = "10.20.1.150"
UNCONFIGURED_HOST = "9.9.9.9"


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _stage_proposal(
    message_id: str = "msg-1",
    action_id: str = "resync_ntp",
    target_nodes=None,
    status: str = "PENDING",
    params: dict | None = None,
) -> str:
    with db_module.SessionLocal() as session:
        session.add(
            ChatMessage(
                id=message_id,
                role="assistant",
                content="Đề xuất resync NTP.",
                proposed_action_id=action_id,
                proposed_target_nodes=json.dumps(target_nodes if target_nodes is not None else [A_MON_HOST]),
                proposed_action_params=json.dumps(params) if params else None,
                proposed_rationale="clock skew",
                proposed_command_preview="chronyc -a makestep",
                proposed_status=status,
            )
        )
        session.commit()
    return message_id


# --- GET /api/chat/messages ----------------------------------------------------
# Backs the floating widget on the Dashboard page (dashboard/static/chat_widget.js)
# — there is no server-rendered /chat page; the widget fetches its history here.


def test_get_chat_messages_requires_login(dashboard_client):
    response = dashboard_client.get("/api/chat/messages", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_chat_messages_returns_history_oldest_first(dashboard_client):
    _stage_proposal(message_id="msg-1")
    with db_module.SessionLocal() as session:
        session.add(ChatMessage(id="msg-0", role="user", content="cluster có khoẻ không?", actor="admin"))
        session.commit()
        # Backdate so ordering is deterministic regardless of same-millisecond inserts.
        from datetime import datetime, timedelta

        msg0 = session.get(ChatMessage, "msg-0")
        msg1 = session.get(ChatMessage, "msg-1")
        msg0.created_at = datetime.utcnow() - timedelta(minutes=1)
        msg1.created_at = datetime.utcnow()
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.get("/api/chat/messages")

    assert response.status_code == 200
    messages = response.json()["messages"]
    assert [m["id"] for m in messages] == ["msg-0", "msg-1"]
    assert messages[1]["proposed_action_id"] == "resync_ntp"
    assert messages[1]["proposed_command_preview"] == "chronyc -a makestep"


def test_get_chat_messages_empty_when_no_history(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/api/chat/messages")

    assert response.status_code == 200
    assert response.json() == {"messages": [], "session_id": None}


def test_get_chat_messages_scopes_to_the_latest_session_only(dashboard_client):
    with db_module.SessionLocal() as session:
        session.add(ChatMessage(id="old-1", session_id="sess-old", role="user", content="tin nhắn cũ"))
        session.commit()
        from datetime import datetime, timedelta

        old = session.get(ChatMessage, "old-1")
        old.created_at = datetime.utcnow() - timedelta(minutes=5)
        session.commit()

        session.add(ChatMessage(id="new-1", session_id="sess-new", role="user", content="tin nhắn mới"))
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.get("/api/chat/messages")

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "sess-new"
    assert [m["id"] for m in body["messages"]] == ["new-1"]


def test_get_chat_messages_returns_legacy_null_session_history(dashboard_client):
    # Pre-migration rows (or the backfill's grouping id) — session_id can be
    # a real string too, but the None case specifically must not be
    # mistaken for "no history at all" (see _latest_session_id's docstring).
    with db_module.SessionLocal() as session:
        session.add(ChatMessage(id="legacy-1", session_id=None, role="user", content="tin nhắn cũ"))
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.get("/api/chat/messages")

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] is None
    assert [m["id"] for m in body["messages"]] == ["legacy-1"]


# --- POST /api/chat/sessions ----------------------------------------------------


def test_create_chat_session_requires_login(dashboard_client):
    response = dashboard_client.post("/api/chat/sessions", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_create_chat_session_returns_a_fresh_id_each_time(dashboard_client):
    _login(dashboard_client)

    first = dashboard_client.post("/api/chat/sessions")
    second = dashboard_client.post("/api/chat/sessions")

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["session_id"]
    assert first.json()["session_id"] != second.json()["session_id"]


def test_create_chat_session_writes_nothing_until_a_message_is_sent(dashboard_client):
    _login(dashboard_client)

    dashboard_client.post("/api/chat/sessions")

    with db_module.SessionLocal() as session:
        assert session.query(ChatMessage).count() == 0
    # GET still reports no history — nothing to show for a session with no
    # messages in it yet.
    assert dashboard_client.get("/api/chat/messages").json() == {"messages": [], "session_id": None}


def test_new_session_message_does_not_see_previous_session_as_context(dashboard_client, monkeypatch):
    with db_module.SessionLocal() as session:
        session.add(ChatMessage(id="old-1", session_id="sess-old", role="user", content="hỏi cũ", actor="admin"))
        session.add(ChatMessage(id="old-2", session_id="sess-old", role="assistant", content="trả lời cũ"))
        session.commit()

    received_history = {}

    async def fake_run_chat_turn(history, user_text, actor):
        received_history["value"] = list(history)
        return {"reply_text": "trả lời mới", "proposal": None, "tools_used": []}

    monkeypatch.setattr(chat_module, "run_chat_turn", fake_run_chat_turn)
    _login(dashboard_client)

    new_session_id = dashboard_client.post("/api/chat/sessions").json()["session_id"]
    response = dashboard_client.post(
        "/api/chat/messages", json={"content": "hỏi mới", "session_id": new_session_id}
    )

    assert response.status_code == 200
    assert received_history["value"] == []  # the old session's messages must not leak in as context

    # And the new message is filed under the new session, visible via GET.
    history_response = dashboard_client.get("/api/chat/messages").json()
    assert history_response["session_id"] == new_session_id
    assert [m["content"] for m in history_response["messages"]] == ["hỏi mới", "trả lời mới"]


def test_post_chat_message_without_session_id_starts_a_new_one(dashboard_client, monkeypatch):
    # A stale/cached frontend bundle that predates sessions entirely must
    # still work — falls back to a fresh session rather than erroring.
    async def fake_run_chat_turn(history, user_text, actor):
        assert history == []
        return {"reply_text": "ok", "proposal": None, "tools_used": []}

    monkeypatch.setattr(chat_module, "run_chat_turn", fake_run_chat_turn)
    _login(dashboard_client)

    response = dashboard_client.post("/api/chat/messages", json={"content": "hi"})

    assert response.status_code == 200
    assert response.json()["user_message"]["session_id"]  # non-empty, some id was generated


# --- POST /api/chat/messages ---------------------------------------------------


def test_post_chat_message_requires_login(dashboard_client):
    response = dashboard_client.post("/api/chat/messages", json={"content": "hi"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_post_chat_message_rejects_empty_content(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post("/api/chat/messages", json={"content": "   "})
    assert response.status_code == 400


def test_post_chat_message_persists_both_messages_and_returns_reply(dashboard_client, monkeypatch):
    async def fake_run_chat_turn(history, user_text, actor):
        assert user_text == "cluster có khoẻ không?"
        assert actor == "admin"
        return {"reply_text": "Cluster đang HEALTH_OK.", "proposal": None}

    monkeypatch.setattr(chat_module, "run_chat_turn", fake_run_chat_turn)
    _login(dashboard_client)

    response = dashboard_client.post("/api/chat/messages", json={"content": "cluster có khoẻ không?"})

    assert response.status_code == 200
    body = response.json()
    assert body["user_message"]["content"] == "cluster có khoẻ không?"
    assert body["user_message"]["actor"] == "admin"
    assert body["assistant_message"]["content"] == "Cluster đang HEALTH_OK."
    assert body["assistant_message"]["proposed_action_id"] is None
    with db_module.SessionLocal() as session:
        assert session.query(ChatMessage).count() == 2


def test_post_chat_message_with_prior_history_passes_plain_dicts_not_orm_rows(
    dashboard_client, monkeypatch
):
    """Regression test: run_chat_turn() used to receive raw ChatMessage ORM
    rows queried inside dashboard/routes/chat.py's `with db.SessionLocal()`
    block. session.commit() (a few lines later, saving the new user message)
    expires every object still attached to that session by default, and the
    session itself closes when the `with` block ends — so the FIRST attempt
    to read `.role`/`.content` off a history row after that point raised
    sqlalchemy.orm.exc.DetachedInstanceError. This only ever showed up once
    a conversation already had prior history (an empty first message's
    history=[] has nothing to detach), which is exactly why it shipped
    unnoticed — every existing test up to this point only ever exercised a
    fresh, empty conversation.
    """
    session_id = "sess-continuing"
    with db_module.SessionLocal() as db_session:
        db_session.add(
            ChatMessage(id="h1", session_id=session_id, role="user", content="tin nhắn trước đó", actor="admin")
        )
        db_session.add(
            ChatMessage(id="h2", session_id=session_id, role="assistant", content="trả lời trước đó")
        )
        db_session.commit()

    received_history = {}

    async def fake_run_chat_turn(history, user_text, actor):
        # The real bug: a ChatMessage ORM row detached from its (now closed)
        # session raises DetachedInstanceError on this exact attribute
        # access — a plain dict never can.
        received_history["value"] = [dict(item) for item in history]
        return {"reply_text": "ok", "proposal": None, "tools_used": []}

    monkeypatch.setattr(chat_module, "run_chat_turn", fake_run_chat_turn)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/api/chat/messages", json={"content": "tin nhắn thứ hai", "session_id": session_id}
    )

    assert response.status_code == 200
    assert received_history["value"] == [
        {"role": "user", "content": "tin nhắn trước đó"},
        {"role": "assistant", "content": "trả lời trước đó"},
    ]


def test_post_chat_message_persists_proposal_fields(dashboard_client, monkeypatch):
    async def fake_run_chat_turn(history, user_text, actor):
        return {
            "reply_text": "Đề xuất resync NTP.",
            "proposal": {
                "action_id": "resync_ntp",
                "target_nodes": [A_MON_HOST],
                "rationale": "clock skew",
                "command_preview": "chronyc -a makestep",
            },
        }

    monkeypatch.setattr(chat_module, "run_chat_turn", fake_run_chat_turn)
    _login(dashboard_client)

    response = dashboard_client.post("/api/chat/messages", json={"content": "sửa lệch giờ giúp tôi"})

    assert response.status_code == 200
    assistant = response.json()["assistant_message"]
    assert assistant["proposed_action_id"] == "resync_ntp"
    assert assistant["proposed_target_nodes"] == [A_MON_HOST]
    assert assistant["proposed_status"] == "PENDING"


def test_post_chat_message_claude_error_is_saved_not_500(dashboard_client, monkeypatch):
    async def fake_run_chat_turn(history, user_text, actor):
        raise chat_module.ChatTurnError("Lỗi gọi Claude API: boom")

    monkeypatch.setattr(chat_module, "run_chat_turn", fake_run_chat_turn)
    _login(dashboard_client)

    response = dashboard_client.post("/api/chat/messages", json={"content": "hi"})

    assert response.status_code == 200
    assert "boom" in response.json()["assistant_message"]["content"]


def test_post_chat_message_persists_and_returns_tools_used(dashboard_client, monkeypatch):
    async def fake_run_chat_turn(history, user_text, actor):
        return {
            "reply_text": "Cụm có 2 pool.",
            "proposal": None,
            "tools_used": ["get_pool_list", "get_df"],
        }

    monkeypatch.setattr(chat_module, "run_chat_turn", fake_run_chat_turn)
    _login(dashboard_client)

    response = dashboard_client.post("/api/chat/messages", json={"content": "cụm có bao nhiêu pool?"})

    assert response.status_code == 200
    assistant = response.json()["assistant_message"]
    assert assistant["tools_used"] == ["get_pool_list", "get_df"]

    # Persisted, not just echoed back live — survives a reload via GET history.
    history_response = dashboard_client.get("/api/chat/messages")
    assert history_response.json()["messages"][-1]["tools_used"] == ["get_pool_list", "get_df"]


def test_post_chat_message_without_ai_key_shows_settings_prompt_without_calling_router(
    dashboard_client, monkeypatch
):
    called = {"yes": False}

    async def fake_run_chat_turn(history, user_text, actor):
        called["yes"] = True
        return {"reply_text": "should not be reached", "proposal": None, "tools_used": []}

    monkeypatch.setattr(chat_module, "run_chat_turn", fake_run_chat_turn)
    monkeypatch.setattr(chat_module.settings, "router_api_key", "")
    _login(dashboard_client)

    response = dashboard_client.post("/api/chat/messages", json={"content": "cụm có bao nhiêu pool?"})

    assert response.status_code == 200
    assert response.json()["assistant_message"]["content"] == chat_module.MISSING_AI_CONFIG_MESSAGE
    assert called["yes"] is False  # never even attempts the router call


# --- POST /api/chat/messages/{id}/confirm-action -------------------------------


def test_confirm_action_requires_login(dashboard_client):
    message_id = _stage_proposal()
    response = dashboard_client.post(f"/api/chat/messages/{message_id}/confirm-action", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_confirm_action_unknown_message_returns_404(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post("/api/chat/messages/does-not-exist/confirm-action")
    assert response.status_code == 404


def test_confirm_action_message_without_proposal_returns_400(dashboard_client):
    with db_module.SessionLocal() as session:
        session.add(ChatMessage(id="plain-1", role="assistant", content="chỉ là câu trả lời"))
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.post("/api/chat/messages/plain-1/confirm-action")

    assert response.status_code == 400


def test_confirm_action_safe_action_auto_approves_and_audits(dashboard_client):
    message_id = _stage_proposal(action_id="resync_ntp")
    _login(dashboard_client)

    response = dashboard_client.post(f"/api/chat/messages/{message_id}/confirm-action")

    assert response.status_code == 200
    body = response.json()
    assert body["proposed_status"] == "CONFIRMED"

    with db_module.SessionLocal() as session:
        message = session.get(ChatMessage, message_id)
        assert message.proposed_status == "CONFIRMED"
        assert message.proposed_incident_id is not None

        incident = session.get(Incident, message.proposed_incident_id)
        assert incident.ceph_code == "CHAT_REQUEST"
        assert incident.status == IncidentStatus.APPROVED.value

        actions = session.query(Action).filter_by(incident_id=incident.id).all()
        assert len(actions) == 1
        assert actions[0].action_id == "resync_ntp"
        assert actions[0].classification == ActionClassification.SAFE.value
        assert actions[0].status == ActionStatus.APPROVED.value
        assert actions[0].target_nodes == json.dumps([A_MON_HOST])

        entries = session.query(AuditEntry).filter_by(incident_id=incident.id).all()
        event_types = {e.event_type for e in entries}
        assert audit.EVENT_CHAT_ACTION_REQUESTED in event_types
        assert audit.EVENT_RISKY_ACTION_PENDING_APPROVAL not in event_types


def test_confirm_action_risky_action_routes_to_pending_approval(dashboard_client):
    message_id = _stage_proposal(action_id="restart_osd_daemon")
    _login(dashboard_client)

    response = dashboard_client.post(f"/api/chat/messages/{message_id}/confirm-action")

    assert response.status_code == 200
    with db_module.SessionLocal() as session:
        message = session.get(ChatMessage, message_id)
        incident = session.get(Incident, message.proposed_incident_id)
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value

        action = session.query(Action).filter_by(incident_id=incident.id).one()
        assert action.classification == ActionClassification.RISKY.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value
    # 2026-07-23: the Dashboard no longer has a "Chờ duyệt" UI card (see
    # tests/test_dashboard_actions.py) — this RISKY Action row still exists
    # and is still approvable via POST /actions/{id}/approve, just not
    # surfaced by any page anymore.


def test_confirm_action_double_submit_is_a_no_op(dashboard_client):
    message_id = _stage_proposal(action_id="resync_ntp")
    _login(dashboard_client)

    first = dashboard_client.post(f"/api/chat/messages/{message_id}/confirm-action")
    second = dashboard_client.post(f"/api/chat/messages/{message_id}/confirm-action")

    assert first.status_code == 200 and second.status_code == 200
    with db_module.SessionLocal() as session:
        assert session.query(Incident).filter_by(ceph_code="CHAT_REQUEST").count() == 1


def test_confirm_action_rejects_host_no_longer_configured(dashboard_client):
    message_id = _stage_proposal(action_id="resync_ntp", target_nodes=[UNCONFIGURED_HOST])
    _login(dashboard_client)

    response = dashboard_client.post(f"/api/chat/messages/{message_id}/confirm-action")

    assert response.status_code == 400
    with db_module.SessionLocal() as session:
        assert session.query(Incident).filter_by(ceph_code="CHAT_REQUEST").count() == 0


# --- confirm-action: management actions (2026-07-23) ------------------------


def test_confirm_action_create_pool_auto_approves_with_resolved_command(dashboard_client):
    message_id = _stage_proposal(
        action_id="create_pool",
        target_nodes=[A_MON_HOST],
        params={"pool_name": "my_new_pool", "pg_num": 32},
    )
    _login(dashboard_client)

    response = dashboard_client.post(f"/api/chat/messages/{message_id}/confirm-action")

    assert response.status_code == 200
    with db_module.SessionLocal() as session:
        message = session.get(ChatMessage, message_id)
        incident = session.get(Incident, message.proposed_incident_id)
        action = session.query(Action).filter_by(incident_id=incident.id).one()
        assert action.action_id == "create_pool"
        assert action.classification == ActionClassification.SAFE.value
        assert action.status == ActionStatus.APPROVED.value
        assert json.loads(action.action_params) == {"pool_name": "my_new_pool", "pg_num": 32}
        # Confirms the endpoint re-resolves the command fresh from
        # action_params (not just trusting the staged preview text) — the
        # real pool name/pg_num are baked in, exactly what the operator
        # will have seen before clicking confirm.
        assert action.proposed_command == "ceph osd pool create my_new_pool 32"


def test_confirm_action_delete_pool_rejects_invalid_pool_name(dashboard_client):
    # A pool name starting with "-" would parse as a CLI flag — commands.py's
    # builder rejects it, and confirm-action must surface that as a 400
    # instead of ever reaching the Worker with a malformed command.
    message_id = _stage_proposal(
        action_id="delete_pool",
        target_nodes=[A_MON_HOST],
        params={"pool_name": "--yes-i-really-really-mean-it"},
    )
    _login(dashboard_client)

    response = dashboard_client.post(f"/api/chat/messages/{message_id}/confirm-action")

    assert response.status_code == 400
    with db_module.SessionLocal() as session:
        assert session.query(Incident).filter_by(ceph_code="CHAT_REQUEST").count() == 0


def test_confirm_action_management_action_rejects_more_than_one_target_node(dashboard_client):
    message_id = _stage_proposal(
        action_id="mark_osd_out",
        target_nodes=[A_MON_HOST, "10.20.1.249"],
        params={"osd_id": 3},
    )
    _login(dashboard_client)

    response = dashboard_client.post(f"/api/chat/messages/{message_id}/confirm-action")

    assert response.status_code == 400
    with db_module.SessionLocal() as session:
        assert session.query(Incident).filter_by(ceph_code="CHAT_REQUEST").count() == 0


# --- GET /api/chat/sessions (history list) --------------------------------------


def _seed_two_sessions():
    from datetime import datetime, timedelta

    with db_module.SessionLocal() as session:
        session.add(
            ChatMessage(
                id="s1-u1", session_id="sess-1", role="user", content="hỏi về pool đầu tiên", actor="admin"
            )
        )
        session.add(ChatMessage(id="s1-a1", session_id="sess-1", role="assistant", content="trả lời 1"))
        session.add(
            ChatMessage(
                id="s2-u1", session_id="sess-2", role="user", content="hỏi về OSD sau đó", actor="admin"
            )
        )
        session.commit()
        s1u = session.get(ChatMessage, "s1-u1")
        s1a = session.get(ChatMessage, "s1-a1")
        s2u = session.get(ChatMessage, "s2-u1")
        s1u.created_at = datetime.utcnow() - timedelta(minutes=10)
        s1a.created_at = datetime.utcnow() - timedelta(minutes=9)
        s2u.created_at = datetime.utcnow() - timedelta(minutes=1)
        session.commit()


def test_list_chat_sessions_requires_login(dashboard_client):
    response = dashboard_client.get("/api/chat/sessions", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_list_chat_sessions_returns_newest_active_first_with_preview_and_count(dashboard_client):
    _seed_two_sessions()
    _login(dashboard_client)

    response = dashboard_client.get("/api/chat/sessions")

    assert response.status_code == 200
    sessions = response.json()["sessions"]
    assert [s["session_id"] for s in sessions] == ["sess-2", "sess-1"]
    assert sessions[0]["preview"] == "hỏi về OSD sau đó"
    assert sessions[0]["message_count"] == 1
    assert sessions[0]["is_current"] is True
    assert sessions[1]["preview"] == "hỏi về pool đầu tiên"
    assert sessions[1]["message_count"] == 2
    assert sessions[1]["is_current"] is False


def test_list_chat_sessions_empty_when_no_history(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/api/chat/sessions")
    assert response.status_code == 200
    assert response.json() == {"sessions": []}


# --- DELETE /api/chat/sessions/{id} ---------------------------------------------


def test_delete_chat_session_requires_login(dashboard_client):
    response = dashboard_client.delete("/api/chat/sessions/sess-1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_delete_chat_session_removes_only_that_sessions_messages(dashboard_client):
    _seed_two_sessions()
    _login(dashboard_client)

    response = dashboard_client.delete("/api/chat/sessions/sess-1")

    assert response.status_code == 200
    assert response.json() == {"deleted": 2}
    with db_module.SessionLocal() as session:
        remaining = session.query(ChatMessage).all()
        assert sorted(m.id for m in remaining) == ["s2-u1"]


def test_delete_chat_session_unknown_session_returns_404(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.delete("/api/chat/sessions/does-not-exist")
    assert response.status_code == 404


def test_delete_chat_session_does_not_touch_incident_created_from_a_confirmed_proposal(dashboard_client):
    message_id = _stage_proposal(action_id="resync_ntp")
    with db_module.SessionLocal() as session:
        session.get(ChatMessage, message_id).session_id = "sess-with-action"
        session.commit()
    _login(dashboard_client)
    dashboard_client.post(f"/api/chat/messages/{message_id}/confirm-action")
    with db_module.SessionLocal() as session:
        incident_id = session.query(Incident).filter_by(ceph_code="CHAT_REQUEST").one().id

    response = dashboard_client.delete("/api/chat/sessions/sess-with-action")

    assert response.status_code == 200
    with db_module.SessionLocal() as session:
        assert session.get(Incident, incident_id) is not None  # untouched by the chat-session delete
