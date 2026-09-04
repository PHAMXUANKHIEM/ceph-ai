import pytest

from shared import env_config


def test_apply_env_updates_rejects_newline_in_value():
    with pytest.raises(ValueError):
        env_config.apply_env_updates([], {"SOME_KEY": "line1\nSOME_OTHER_KEY=injected"})


def test_apply_env_updates_rejects_carriage_return_in_value():
    with pytest.raises(ValueError):
        env_config.apply_env_updates([], {"SOME_KEY": "line1\rSOME_OTHER_KEY=injected"})


def test_apply_env_updates_replaces_matching_line_preserving_order():
    existing = ["DASHBOARD_USERNAME=admin", "ROUTER_API_KEY=old-key", "OTHER=1"]
    result = env_config.apply_env_updates(existing, {"ROUTER_API_KEY": "new-key"})
    assert result == ["DASHBOARD_USERNAME=admin", "ROUTER_API_KEY=new-key", "OTHER=1"]


def test_apply_env_updates_appends_when_missing():
    result = env_config.apply_env_updates(["DASHBOARD_USERNAME=admin"], {"ROUTER_API_KEY": "brand-new-key"})
    assert result == ["DASHBOARD_USERNAME=admin", "ROUTER_API_KEY=brand-new-key"]


def test_update_env_file_replaces_existing_line_in_place(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DASHBOARD_USERNAME=admin\nROUTER_API_KEY=old-key\nOTHER=1\n")
    monkeypatch.setattr(env_config, "ENV_PATH", env_file)

    env_config.update_env_file("ROUTER_API_KEY", "new-key")

    lines = env_file.read_text().splitlines()
    assert lines == ["DASHBOARD_USERNAME=admin", "ROUTER_API_KEY=new-key", "OTHER=1"]


def test_update_env_file_appends_when_missing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", env_file)

    env_config.update_env_file("ROUTER_API_KEY", "brand-new-key")

    lines = env_file.read_text().splitlines()
    assert lines == ["DASHBOARD_USERNAME=admin", "ROUTER_API_KEY=brand-new-key"]


def test_update_env_file_creates_file_when_absent(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(env_config, "ENV_PATH", env_file)

    env_config.update_env_file("ROUTER_API_KEY", "brand-new-key")

    assert env_file.read_text().splitlines() == ["ROUTER_API_KEY=brand-new-key"]


def test_update_env_file_batch_writes_all_fields_atomically(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DASHBOARD_USERNAME=admin\nCEPH_MON_NODES=old\n")
    monkeypatch.setattr(env_config, "ENV_PATH", env_file)

    env_config.update_env_file_batch({"CEPH_MON_NODES": "10.0.0.1", "CEPH_EXEC_MODE": "cephadm"})

    lines = env_file.read_text().splitlines()
    assert lines == ["DASHBOARD_USERNAME=admin", "CEPH_MON_NODES=10.0.0.1", "CEPH_EXEC_MODE=cephadm"]


def test_update_env_file_batch_rejects_newline_before_writing_anything(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", env_file)

    with pytest.raises(ValueError):
        env_config.update_env_file_batch({"CEPH_MON_NODES": "10.0.0.1\nSESSION_SECRET_KEY=pwned"})

    # Nothing written — the bad field must not have leaked into .env.
    assert env_file.read_text() == "DASHBOARD_USERNAME=admin\n"


def test_write_env_lines_restricts_permissions_and_replaces_atomically(tmp_path, monkeypatch):
    import stat

    env_file = tmp_path / ".env"
    monkeypatch.setattr(env_config, "ENV_PATH", env_file)

    env_config.write_env_lines(["A=1", "B=2"])

    assert env_file.read_text() == "A=1\nB=2\n"
    mode = stat.S_IMODE(env_file.stat().st_mode)

    assert mode == stat.S_IRUSR | stat.S_IWUSR

def test_refresh_cluster_settings_prefers_current_file_over_stale_process_env(tmp_path, monkeypatch):
    import importlib

    settings_module = importlib.import_module("config.settings")
    env_file = tmp_path / ".env"
    env_file.write_text("CEPH_EXEC_MODE=cephadm\nCEPH_MON_NODES=10.3.53.1,10.3.53.69\n")
    monkeypatch.setattr(env_config, "ENV_PATH", env_file)
    monkeypatch.setenv("CEPH_EXEC_MODE", "none")
    monkeypatch.setattr(settings_module.settings, "ceph_exec_mode", "none")
    monkeypatch.setattr(settings_module.settings, "ceph_mon_nodes", "stale-node")

    settings_module.refresh_cluster_settings_from_env()

    assert settings_module.settings.ceph_exec_mode == "cephadm"
    assert settings_module.settings.ceph_mon_nodes == "10.3.53.1,10.3.53.69"
