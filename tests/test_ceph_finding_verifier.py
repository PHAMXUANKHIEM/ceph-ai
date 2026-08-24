from types import SimpleNamespace

import pytest

from watcher import ceph_finding_verifier as verifier


@pytest.fixture(autouse=True)
def _stub_runtime_discovery(monkeypatch):
    monkeypatch.setattr(verifier, "_ceph_vault_config", lambda cluster: ([], "not configured"))
    monkeypatch.setattr(verifier, "_rgw_orch_daemons", lambda cluster: ([], "not available"))


def _finding(**overrides):
    values = {
        "title": "RGW failed to retrieve actual key from Vault",
        "summary": "Vault key lookup failed",
        "root_cause_hypothesis": "token problem",
        "affected_hosts_json": '["rgw1"]',
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _pattern(sample):
    return SimpleNamespace(template="Vault token file '<PATH>' not found", sample_line=sample)


def _cluster():
    return SimpleNamespace(
        ceph_mon_nodes="mon1", ceph_mgr_nodes="", ceph_osd_nodes="", ceph_rgw_nodes="rgw1",
        ssh_user="root", ssh_key_path="/key", ceph_exec_mode="cephadm", ceph_container_name="",
    )


def test_missing_vault_token_is_external_config_not_learning(monkeypatch):
    monkeypatch.setattr(verifier, "_health", lambda cluster: ("HEALTH_WARN", None))
    monkeypatch.setattr(verifier, "_stat_token", lambda host, path, cluster: "MISSING")
    result = verifier.verify(_finding(), [_pattern("Vault token file '/etc/ceph/vault-token' not found")], _cluster())
    assert result.code == "VAULT_TOKEN_MISSING"
    assert result.eligible_for_learning is False
    assert "ceph_health=HEALTH_WARN" in result.live_facts


def test_present_token_classifies_auth_policy_or_key_without_reading_secret(monkeypatch):
    monkeypatch.setattr(verifier, "_health", lambda cluster: ("HEALTH_OK", None))
    monkeypatch.setattr(verifier, "_stat_token", lambda host, path, cluster: "PRESENT mode=600 owner=ceph:ceph size=95")
    result = verifier.verify(_finding(), [_pattern("Vault token file '/etc/ceph/vault-token' not found")], _cluster())
    assert result.code == "VAULT_AUTH_OR_KEY_LOOKUP_FAILURE"
    assert result.eligible_for_learning is False
    assert all("token=" not in fact.lower() for fact in result.live_facts)


def test_cluster_unreachable_wins_over_vault_guess(monkeypatch):
    monkeypatch.setattr(verifier, "_health", lambda cluster: (None, "All MON nodes failed"))
    result = verifier.verify(_finding(), [_pattern("Vault token file '/etc/ceph/vault-token' not found")], _cluster())
    assert result.code == "CEPH_UNREACHABLE"
    assert "chưa thể kết luận Vault" in result.summary


def test_token_stat_command_reads_metadata_only(monkeypatch):
    captured = {}
    monkeypatch.setattr(verifier, "resolve_ssh_creds", lambda cluster: ("root", "/key", "cephadm", ""))
    def fake_run(host, command, user, key, timeout):
        captured["command"] = command
        return "PRESENT mode=600 owner=ceph:ceph size=95"
    monkeypatch.setattr(verifier.ceph_client, "run_command_on_node_with", fake_run)
    assert verifier._stat_token("rgw1", "/etc/ceph/vault-token", _cluster()).startswith("PRESENT")
    assert "stat -c" in captured["command"]
    assert "cat " not in captured["command"]
    assert "sha" not in captured["command"]


def test_unsafe_token_path_is_never_sent_to_ssh(monkeypatch):
    monkeypatch.setattr(verifier, "_health", lambda cluster: ("HEALTH_OK", None))
    monkeypatch.setattr(verifier, "_stat_token", lambda *args: (_ for _ in ()).throw(AssertionError("must not SSH")))
    result = verifier.verify(_finding(), [_pattern("Vault token file '/tmp/x;id' not found")], _cluster())
    assert result.code == "VAULT_TOKEN_PATH_UNKNOWN"


def test_discovers_token_path_from_ceph_config_dump(monkeypatch):
    monkeypatch.setattr(verifier, "_health", lambda cluster: ("HEALTH_OK", None))
    monkeypatch.setattr(verifier, "_ceph_vault_config", lambda cluster: ([{
        "section": "client.rgw.sse.host.abc",
        "name": "rgw_crypt_sse_s3_vault_token_file",
        "value": "/etc/ceph/vault_token",
    }], None))
    monkeypatch.setattr(
        verifier, "_stat_token",
        lambda host, path, cluster, container_id=None: "PRESENT mode=600 owner=ceph:ceph size=29",
    )
    result = verifier.verify(_finding(), [_pattern("failed to retrieve actual key")], _cluster())
    assert result.code == "VAULT_AUTH_OR_KEY_LOOKUP_FAILURE"
    assert "ceph_config[client.rgw.sse.host.abc].rgw_crypt_sse_s3_vault_token_file=/etc/ceph/vault_token" in result.live_facts


def test_cephadm_stats_token_inside_orchestrated_rgw_container(monkeypatch):
    monkeypatch.setattr(verifier, "_health", lambda cluster: ("HEALTH_OK", None))
    monkeypatch.setattr(verifier, "_ceph_vault_config", lambda cluster: ([{
        "section": "client.rgw.sse.host.abc", "name": "rgw_crypt_vault_token_file",
        "value": "/opt/vault/vault_token",
    }], None))
    monkeypatch.setattr(verifier, "_rgw_orch_daemons", lambda cluster: ([{
        "container_id": "3a14dd52cc9e", "daemon_name": "rgw.sse.host.abc",
    }], None))
    calls = []
    def fake_stat(host, path, cluster, container_id=None):
        calls.append((host, path, container_id))
        return "PRESENT mode=600 owner=ceph:ceph size=29"
    monkeypatch.setattr(verifier, "_stat_token", fake_stat)
    result = verifier.verify(_finding(), [_pattern("failed to retrieve actual key")], _cluster())
    assert result.code == "VAULT_AUTH_OR_KEY_LOOKUP_FAILURE"
    assert calls == [("rgw1", "/opt/vault/vault_token", "3a14dd52cc9e")]
    assert "rgw_deployment=cephadm containers=1" in result.live_facts


def test_vault_recovery_requires_successful_live_token_lookup(monkeypatch):
    monkeypatch.setattr(verifier, "_health", lambda cluster: ("HEALTH_OK", None))
    monkeypatch.setattr(verifier, "_ceph_vault_config", lambda cluster: ([
        {"section": "client.rgw.sse.host.abc", "name": "rgw_crypt_vault_token_file",
         "value": "/opt/vault/vault_token"},
        {"section": "client.rgw.sse.host.abc", "name": "rgw_crypt_vault_addr",
         "value": "http://vault.internal:8200"},
    ], None))
    monkeypatch.setattr(verifier, "_rgw_orch_daemons", lambda cluster: ([{
        "container_id": "3a14dd52cc9e", "daemon_name": "rgw.sse.host.abc",
    }], None))
    monkeypatch.setattr(
        verifier, "_probe_vault_token",
        lambda *args: "VAULT_HEALTH_HTTP=200 TOKEN_LOOKUP_HTTP=200",
    )
    result = verifier.verify_vault_recovery(
        _finding(), [_pattern("failed to retrieve actual key")], _cluster(),
    )
    assert result.code == "VAULT_RECOVERY_VERIFIED"
    assert result.eligible_for_learning is True


def test_vault_recovery_rejects_invalid_token(monkeypatch):
    monkeypatch.setattr(verifier, "_health", lambda cluster: ("HEALTH_OK", None))
    monkeypatch.setattr(verifier, "_ceph_vault_config", lambda cluster: ([
        {"section": "client.rgw.sse.host.abc", "name": "rgw_crypt_vault_token_file",
         "value": "/opt/vault/vault_token"},
        {"section": "client.rgw.sse.host.abc", "name": "rgw_crypt_vault_addr",
         "value": "http://vault.internal:8200"},
    ], None))
    monkeypatch.setattr(verifier, "_rgw_orch_daemons", lambda cluster: ([{
        "container_id": "3a14dd52cc9e", "daemon_name": "rgw.sse.host.abc",
    }], None))
    monkeypatch.setattr(
        verifier, "_probe_vault_token",
        lambda *args: "VAULT_HEALTH_HTTP=200 TOKEN_LOOKUP_HTTP=403",
    )
    result = verifier.verify_vault_recovery(
        _finding(), [_pattern("failed to retrieve actual key")], _cluster(),
    )
    assert result.code == "VAULT_RECOVERY_UNVERIFIED"
    assert result.eligible_for_learning is False


def test_sse_s3_recovery_does_not_require_unrelated_legacy_kms_backend(monkeypatch):
    monkeypatch.setattr(verifier, "_health", lambda cluster: ("HEALTH_OK", None))
    monkeypatch.setattr(
        verifier, "_functional_rgw_recovery",
        lambda finding, patterns: ("sse_s3", ("functional_request=PUT 200 encryption=SSE-S3",)),
    )
    monkeypatch.setattr(verifier, "_ceph_vault_config", lambda cluster: ([
        {"section": "client.rgw.sse.host.abc", "name": "rgw_crypt_sse_s3_vault_token_file",
         "value": "/etc/ceph/vault_token"},
        {"section": "client.rgw.sse.host.abc", "name": "rgw_crypt_sse_s3_vault_addr",
         "value": "http://active-vault:8200"},
        {"section": "client.rgw.sse.host.abc", "name": "rgw_crypt_vault_token_file",
         "value": "/opt/vault/vault_token"},
        {"section": "client.rgw.sse.host.abc", "name": "rgw_crypt_vault_addr",
         "value": "http://retired-vault:8200"},
    ], None))
    monkeypatch.setattr(verifier, "_rgw_orch_daemons", lambda cluster: ([{
        "container_id": "3a14dd52cc9e", "daemon_name": "rgw.sse.host.abc",
    }], None))
    calls = []
    def fake_probe(host, path, addr, cluster, container_id=None):
        calls.append((path, addr))
        return "VAULT_HEALTH_HTTP=429 TOKEN_LOOKUP_HTTP=200"
    monkeypatch.setattr(verifier, "_probe_vault_token", fake_probe)
    result = verifier.verify_vault_recovery(
        _finding(), [_pattern("failed to retrieve actual key")], _cluster(),
    )
    assert result.code == "VAULT_RECOVERY_VERIFIED"
    assert calls == [("/etc/ceph/vault_token", "http://active-vault:8200")]
