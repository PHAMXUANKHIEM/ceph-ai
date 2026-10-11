

def test_an_extra_cluster_on_the_read_only_key_uses_this_processes_own_identity(monkeypatch):
    """11/10/2026: the Worker refused every action on CS-STAGING because the row held the read-only key."""
    from types import SimpleNamespace

    from config.settings import settings
    from shared.cluster_nodes import READ_ONLY_KEY_PATH, resolve_ssh_creds

    staging = SimpleNamespace(ssh_user="root", ssh_key_path=READ_ONLY_KEY_PATH, ceph_exec_mode="cephadm",
                              ceph_container_name="")
    own = SimpleNamespace(ssh_user="ops", ssh_key_path="/etc/ceph-ai/cluster2-key", ceph_exec_mode="none",
                          ceph_container_name="")

    monkeypatch.setattr(settings, "ssh_key_path", "/run/ceph-ai/credentials/mutation/id_ed25519")  # the Worker
    assert resolve_ssh_creds(staging) == ("root", "/run/ceph-ai/credentials/mutation/id_ed25519", "cephadm", "")
    assert resolve_ssh_creds(own)[1] == "/etc/ceph-ai/cluster2-key"  # a cluster's own key is kept

    monkeypatch.setattr(settings, "ssh_key_path", READ_ONLY_KEY_PATH)  # the Watcher stays read-only
    assert resolve_ssh_creds(staging)[1] == READ_ONLY_KEY_PATH
