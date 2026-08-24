from types import SimpleNamespace

from watcher import ceph_finding_verifier as verifier


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
