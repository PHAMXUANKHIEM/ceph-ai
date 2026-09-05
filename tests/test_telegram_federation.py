import asyncio
from types import SimpleNamespace

from dashboard import telegram_chat
from shared import telegram_federation as federation


def test_database_sources_are_local_only_even_with_legacy_remote_config(monkeypatch):
    monkeypatch.setattr(federation.settings, "database_url", "sqlite:///hapu.db")
    monkeypatch.setattr(
        federation.settings,
        "telegram_federated_database_urls",
        "cs-lab=postgresql://user:pass@cs.example/ceph;broken-entry;CS-LAB=postgresql://duplicate",
    )

    sources = federation.database_sources()

    assert [(source.key, source.url) for source in sources] == [("local", "sqlite:///hapu.db")]


def test_active_clusters_only_load_the_local_database(monkeypatch):
    local = SimpleNamespace(id="hapu-id", name="Hapu-Lab", is_default=True, is_active=True)

    monkeypatch.setattr(
        federation.settings,
        "database_url",
        "postgresql://hapu",
    )
    monkeypatch.setattr(
        federation.settings,
        "telegram_federated_database_urls",
        "cs-lab=postgresql://cs",
    )
    monkeypatch.setattr(
        federation,
        "_load_active_with_models",
        lambda source: [
            (
                federation.ClusterTarget(source, str(cluster.id), cluster.name, cluster.is_default),
                cluster,
            )
            for cluster in [local]
        ],
    )

    targets = federation.active_clusters()

    assert [(target.qualified_id, target.name) for target in targets] == [("local:hapu-id", "Hapu-Lab")]


def test_qualified_reference_routes_without_scanning_other_databases(monkeypatch):
    monkeypatch.setattr(federation.settings, "database_url", "postgresql://hapu")
    monkeypatch.setattr(
        federation.settings,
        "telegram_federated_database_urls",
        "cs-lab=postgresql://cs",
    )

    assert federation.database_urls_for_message_reference("cs-lab:duplicate-id") == []
    assert federation.database_urls_for_action_reference("local:duplicate-id") == [
        "postgresql://hapu"
    ]


def test_legacy_lookup_returns_all_matching_database_urls(monkeypatch):
    monkeypatch.setattr(federation.settings, "database_url", "postgresql://hapu")
    monkeypatch.setattr(
        federation.settings,
        "telegram_federated_database_urls",
        "cs-lab=postgresql://cs",
    )
    monkeypatch.setattr(federation, "database_urls_for_message", lambda _id: ["hapu"])
    monkeypatch.setattr(federation, "database_urls_for_action", lambda _id: ["hapu"])

    assert federation.database_urls_for_message_reference("duplicate-id") == ["hapu"]
    assert federation.database_urls_for_action_reference("duplicate-id") == ["hapu"]


def test_legacy_chat_callback_is_rejected_when_id_is_ambiguous(monkeypatch):
    monkeypatch.setattr(
        telegram_chat.telegram_federation,
        "database_urls_for_message_reference",
        lambda _reference: ["postgresql://hapu", "postgresql://cs"],
    )

    result = telegram_chat.run_callback_sync(
        {"data": "chatconfirm:duplicate-id"}, "123456:token"
    )

    assert result == "Yêu cầu cũ bị trùng giữa các DB; hãy gửi lại yêu cầu sau khi chọn đúng cụm."


def test_mode_selection_clears_old_cluster_and_prompts_again(monkeypatch):
    sent = []
    cleared = []
    selectors = []

    async def fake_send(_token, _chat_id, text):
        sent.append(text)

    async def fake_selector(_token, _chat_id, mode=None):
        selectors.append(mode)
        return True

    monkeypatch.setattr(telegram_chat, "_set_mode", lambda _actor, _mode: None)
    monkeypatch.setattr(telegram_chat, "_clear_cluster", cleared.append)
    monkeypatch.setattr(telegram_chat, "_send", fake_send)
    monkeypatch.setattr(telegram_chat, "_send_cluster_selector", fake_selector)

    result = asyncio.run(
        telegram_chat._select_mode(
            "123456:token", "123456", "telegram:123456", "single", full_access_allowed=False
        )
    )

    assert result == telegram_chat._MODE_LABELS["single"]
    assert cleared == ["telegram:123456"]
    assert selectors == ["single"]
    assert sent == ["Đã chuyển sang chế độ Chat với một AI."]


def test_actor_without_cluster_selection_has_no_default_fallback(monkeypatch):
    monkeypatch.setattr(telegram_chat, "_selected_cluster_id", lambda _actor: None)

    assert telegram_chat._resolve_cluster_for_actor("telegram:123456") is None


def test_mode_command_does_not_resolve_previous_cluster(monkeypatch):
    calls = []

    async def fake_handle(message, bot_token, *, cluster_override):
        calls.append((message["text"], bot_token, cluster_override))

    monkeypatch.setattr(telegram_chat, "is_allowed_message", lambda _message, _token: True)
    monkeypatch.setattr(telegram_chat, "_actor", lambda _message: "telegram:123456")
    monkeypatch.setattr(
        telegram_chat,
        "_resolve_cluster_for_actor",
        lambda _actor: (_ for _ in ()).throw(AssertionError("mode command resolved a DB cluster")),
    )
    monkeypatch.setattr(telegram_chat, "_handle_message_impl", fake_handle)

    asyncio.run(telegram_chat.handle_message({"text": "/single@Ceph_chat_ai_bot"}, "token"))

    assert calls == [("/single@Ceph_chat_ai_bot", "token", None)]


def test_cluster_selector_times_out_instead_of_blocking(monkeypatch):
    sent = []

    def slow_inventory(*_args):
        import time
        time.sleep(0.1)
        return []

    async def fake_send(_token, _chat_id, text):
        sent.append(text)

    monkeypatch.setattr(telegram_chat, "_CLUSTER_LOOKUP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(telegram_chat, "_active_clusters", slow_inventory)
    monkeypatch.setattr(telegram_chat, "_send", fake_send)

    result = asyncio.run(telegram_chat._send_cluster_selector("token", "123456", "single"))

    assert result is False
    assert sent == ["DB cụm đang phản hồi chậm; hãy thử lại sau vài giây."]
