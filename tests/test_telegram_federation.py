import asyncio
from types import SimpleNamespace

from dashboard import telegram_chat
from shared import telegram_federation as federation


def test_database_sources_always_include_local_and_parse_remote_urls(monkeypatch):
    monkeypatch.setattr(federation.settings, "database_url", "sqlite:///hapu.db")
    monkeypatch.setattr(
        federation.settings,
        "telegram_federated_database_urls",
        "cs-lab=postgresql://user:pass@cs.example/ceph;broken-entry;CS-LAB=postgresql://duplicate",
    )

    sources = federation.database_sources()

    assert [(source.key, source.url) for source in sources] == [
        ("local", "sqlite:///hapu.db"),
        ("cs-lab", "postgresql://user:pass@cs.example/ceph"),
    ]


def test_active_clusters_are_qualified_by_database_source(monkeypatch):
    local = SimpleNamespace(id="hapu-id", name="Hapu-Lab", is_default=True, is_active=True)
    remote = SimpleNamespace(id="cs-id", name="CS-LAB", is_default=True, is_active=True)

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
            for cluster in ([local] if source.key == "local" else [remote])
        ],
    )

    targets = federation.active_clusters()

    assert [(target.qualified_id, target.name) for target in targets] == [
        ("cs-lab:cs-id", "CS-LAB"),
        ("local:hapu-id", "Hapu-Lab"),
    ]


def test_qualified_reference_routes_without_scanning_other_databases(monkeypatch):
    monkeypatch.setattr(federation.settings, "database_url", "postgresql://hapu")
    monkeypatch.setattr(
        federation.settings,
        "telegram_federated_database_urls",
        "cs-lab=postgresql://cs",
    )

    assert federation.database_urls_for_message_reference("cs-lab:duplicate-id") == [
        "postgresql://cs"
    ]
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
    monkeypatch.setattr(federation, "database_urls_for_message", lambda _id: ["hapu", "cs"])
    monkeypatch.setattr(federation, "database_urls_for_action", lambda _id: ["hapu", "cs"])

    assert federation.database_urls_for_message_reference("duplicate-id") == ["hapu", "cs"]
    assert federation.database_urls_for_action_reference("duplicate-id") == ["hapu", "cs"]


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
