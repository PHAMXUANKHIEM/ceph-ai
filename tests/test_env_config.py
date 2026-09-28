import os
import subprocess
import sys

import pytest

from shared import env_config


def test_configured_env_path_prefers_container_shared_file(tmp_path, monkeypatch):
    shared_file = tmp_path / "config" / ".env"
    monkeypatch.setenv("CEPH_AI_ENV_FILE", str(shared_file))

    assert env_config._configured_env_path() == shared_file


def test_configured_env_path_falls_back_to_checkout_env(monkeypatch):
    monkeypatch.delenv("CEPH_AI_ENV_FILE", raising=False)

    assert env_config._configured_env_path() == env_config._LOCAL_ENV_PATH


def test_settings_loads_the_same_runtime_env_file_that_writer_uses(tmp_path):
    env_file = tmp_path / "shared" / ".env"
    env_file.parent.mkdir()
    env_file.write_text(
        "ROUTER_API_KEY=test-key\n"
        "ROUTER_BASE_URL=http://router.example.test:20128\n"
        "ROUTER_MODEL=test-model\n"
        "ROUTER_ENABLED=true\n"
    )
    process_env = os.environ.copy()
    process_env["CEPH_AI_ENV_FILE"] = str(env_file)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from config.settings import settings; "
                "print(settings.router_base_url); "
                "print(settings.router_model); "
                "print(settings.router_enabled)"
            ),
        ],
        cwd=str(env_config._LOCAL_ENV_PATH.parent),
        env=process_env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.splitlines() == ["http://router.example.test:20128", "test-model", "True"]


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
