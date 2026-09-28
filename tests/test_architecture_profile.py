from types import SimpleNamespace

from scripts.architecture_profile import build_installation_profile, build_stream_architecture_links, render_mermaid, _write_private


def _settings(**overrides):
    values = {
        "ceph_rgw_nodes": "",
        "ceph_rgw_s3_endpoint": "",
        "backup_target_a_transport": "",
        "backup_target_b_transport": "",
        "router_enabled": False,
        "codex_chat_enabled": False,
        "claude_chat_enabled": False,
        "dual_ai_fallback_enabled": False,
        "telegram_listener_enabled": False,
        "telegram_backup_enabled": True,
        "telegram_backup_bot_token": "",
        "telegram_backup_chat_id": "",
        "telegram_incident_enabled": True,
        "telegram_incident_bot_token": "",
        "telegram_incident_chat_id": "",
        "telegram_node_enabled": True,
        "telegram_node_bot_token": "",
        "telegram_node_chat_id": "",
        "telegram_rgw_enabled": True,
        "telegram_rgw_bot_token": "",
        "telegram_rgw_chat_id": "",
        "telegram_vault_enabled": True,
        "telegram_vault_bot_token": "",
        "telegram_vault_chat_id": "",
        "telegram_chatbox_enabled": True,
        "telegram_chatbox_bot_token": "",
        "telegram_chatbox_chat_id": "",
        "log_intel_enabled": False,
        "log_intel_source": "ssh",
        "log_intel_loki_url": "",
        "log_intel_elasticsearch_url": "",
        "vault_monitor_enabled": False,
        "vault_addr": "",
        "ceph_mon_nodes": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_installation_profile_reflects_per_cluster_capabilities_without_hosts_or_credentials():
    cluster = SimpleNamespace(
        id="private-cluster-id",
        name="customer-secret-name",
        is_active=True,
        is_default=True,
        ceph_exec_mode="cephadm",
        ceph_mon_nodes="10.0.0.1,10.0.0.2",
        ceph_mgr_nodes="10.0.0.3",
        ceph_osd_nodes="10.0.0.4,10.0.0.5",
        ceph_rgw_nodes="10.0.0.6",
        openstack_controller_nodes="10.0.0.7",
        openstack_compute_nodes="",
        backup_enabled=True,
        backup_tracked_images="volumes/disk-a",
        backup_transport="s3",
        backup_s3_endpoint="https://private.example",
        backup_s3_secret_key="backup-secret-value",
        telegram_enabled=True,
        telegram_bot_token="telegram-secret-value",
        telegram_chat_id="-99999",
    )
    profile = build_installation_profile(
        _settings(
            router_enabled=True,
            ceph_rgw_s3_endpoint="https://private-rgw.example",
            telegram_listener_enabled=True,
        ),
        [cluster],
        [],
        [SimpleNamespace(enabled=True, provider_type="ldap", secret_ref="vault:private/path#pw")],
        generated_at="2026-09-28T00:00:00+00:00",
    )

    assert profile["clusters"] == [
        {
            "label": "Ceph cluster 1",
            "active": True,
            "default": True,
            "execution_mode": "cephadm",
            "node_counts": {"mon": 2, "mgr": 1, "osd": 2, "rgw": 1},
            "backup": True,
        }
    ]
    assert profile["features"]["openstack"]["status"] == "configured"
    assert profile["features"]["object_storage"]["status"] == "configured"
    assert profile["features"]["federated_identity"]["enabled_provider_types"] == {"ldap": 1}
    assert profile["features"]["federated_identity"]["worker_role_mapping_support"] == ["oidc"]
    serialized = str(profile)
    for secret in ("10.0.0.1", "private-cluster-id", "customer-secret-name", "private.example", "backup-secret-value", "telegram-secret-value", "vault:private/path"):
        assert secret not in serialized
    assert profile["secrets_included"] is False


def test_installation_specific_mermaid_omits_unconfigured_integrations():
    ceph = SimpleNamespace(
        is_active=True,
        is_default=True,
        ceph_exec_mode="docker",
        ceph_mon_nodes="10.1.0.1",
        ceph_mgr_nodes="",
        ceph_osd_nodes="",
        ceph_rgw_nodes="",
        openstack_controller_nodes="",
        openstack_compute_nodes="",
        backup_enabled=False,
        backup_tracked_images="",
        backup_transport="",
        telegram_enabled=False,
        telegram_bot_token="",
        telegram_chat_id="",
    )
    profile = build_installation_profile(_settings(), [ceph])
    diagram = render_mermaid(profile)

    assert "Ceph clusters" in diagram
    assert "RGW / S3" not in diagram
    assert "OpenStack" not in diagram
    assert "AI provider" not in diagram
    assert "Telegram" not in diagram


def test_configured_features_add_only_their_installation_flow():
    profile = build_installation_profile(
        _settings(
            log_intel_enabled=True,
            log_intel_source="loki",
            log_intel_loki_url="https://loki.private",
            backup_target_a_transport="ssh",
            router_enabled=True,
        ),
        [],
    )
    diagram = render_mermaid(profile)

    assert "Configured backup targets" in diagram
    assert "AI provider" in diagram
    assert "Configured log source" in diagram
    assert "loki.private" not in diagram


def test_generated_profile_file_is_restricted_to_owner(tmp_path):
    target = tmp_path / "nested" / "profile.json"

    _write_private(target, '{"secrets_included": false}\n')

    assert target.read_text() == '{"secrets_included": false}\n'
    assert target.stat().st_mode & 0o777 == 0o600


def test_installation_profile_links_stream_nodes_to_reviewed_flows_and_tests():
    profile = build_installation_profile(_settings(), [])
    architecture = profile["architecture"]

    assert "flow.installation_stream" in {flow["id"] for flow in architecture["dashboard"]["flows"]}
    assert "tests/test_dashboard_installation_stream.py" in architecture["dashboard"]["tests"]
    assert "tests/test_dashboard_navigation.py" in architecture["browser"]["tests"]
    assert "tests/test_backup_engine.py" in architecture["backup"]["tests"]
    assert any(edge["kind"] == "requires_auth" for edge in architecture["dashboard"]["relationships"])
    assert profile["secrets_included"] is False


def test_stream_architecture_mapping_covers_every_rendered_component():
    links = build_stream_architecture_links()

    assert {"browser", "dashboard", "database", "watcher", "rabbit", "worker", "ceph"}.issubset(links)
    assert {"object_storage", "openstack", "ai_provider", "backup", "telegram", "log_intelligence", "vitastor", "federated_identity", "vault_monitoring"}.issubset(links)
    assert all(item["architecture_ids"] and item["tests"] for item in links.values())
