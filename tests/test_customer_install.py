"""Customer install profile (deploy/customer/compose.yaml, scripts/customer/, plan R8.1)."""

import io
import json
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.customer import build_env

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "customer" / "compose.yaml"
SCRIPT = ROOT / "scripts" / "customer" / "ceph-ai-customer.sh"
SECRETS = {"db_password": "d" * 48, "rabbitmq_password": "r" * 48, "session_secret": "s" * 64,
           "dashboard_password_hash": "$2b$12$abcdefghijklmnopqrstuuvwxyzABCDEFGHIJKLMNOPQRSTUVWX"}


def test_the_new_env_keeps_known_settings_and_owns_the_stack_secrets():
    old = {"CEPH_MON_NODES": "10.2.0.11,10.2.0.12", "SSH_USER": "root", "TELEGRAM_INCIDENT_CHAT_ID": "-100",
           "DATABASE_URL": "postgresql://old@db:31081/x", "AUTOPILOT_ENABLED": "true",
           "TELEGRAM_FEDERATED_DATABASE_URLS": "postgresql://old", "NOT_A_SETTING": "x"}

    values, report = build_env.build(old, SECRETS, build_env.allowed_keys())

    assert values["CEPH_MON_NODES"] == "10.2.0.11,10.2.0.12" and values["SSH_USER"] == "root"
    assert values["DATABASE_URL"] == f"postgresql+psycopg://ceph_ai:{'d' * 48}@postgres:5432/ceph_ai"
    assert values["RABBITMQ_URL"] == f"amqp://ceph_ai:{'r' * 48}@rabbitmq:5672/"
    assert values["AUTOPILOT_ENABLED"] == "false" and values["DASHBOARD_USERNAME"] == "admin"
    assert "TELEGRAM_FEDERATED_DATABASE_URLS" not in values and "NOT_A_SETTING" not in values
    assert report["dropped_unknown"] == ["NOT_A_SETTING"]
    assert report["dropped_on_purpose"] == ["TELEGRAM_FEDERATED_DATABASE_URLS"]
    assert report["replaced"] == ["AUTOPILOT_ENABLED", "DATABASE_URL"]
    assert "d" * 48 not in json.dumps(report)


@pytest.mark.parametrize("secrets", [{}, {**SECRETS, "db_password": ""}])
def test_missing_secrets_are_refused(secrets):
    with pytest.raises(ValueError, match="missing secrets"):
        build_env.build({}, secrets, build_env.allowed_keys())


def test_a_value_with_a_line_break_is_refused():
    with pytest.raises(ValueError, match="line break"):
        build_env.build({"SSH_USER": "root\nEVIL=1"}, SECRETS, build_env.allowed_keys())


def test_the_written_file_is_unquoted_and_not_world_readable(tmp_path, monkeypatch):
    old = tmp_path / "old.env"
    old.write_text("SSH_USER=ceph\nCEPH_MON_NODES='10.2.0.11'\n")
    out = tmp_path / "config" / ".env"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SECRETS)))

    assert build_env.main(["--out", str(out), "--old-env", str(old)]) == 0

    text = out.read_text()
    assert "CEPH_MON_NODES=10.2.0.11\n" in text and "SSH_USER=ceph\n" in text
    assert stat.S_IMODE(out.stat().st_mode) == 0o640


def test_the_generated_file_loads_as_settings(tmp_path, monkeypatch):
    out = tmp_path / ".env"
    values, _ = build_env.build({"SSH_USER": "ceph"}, SECRETS, build_env.allowed_keys())
    build_env.write(out, values)
    from config.settings import Settings
    settings = Settings(_env_file=str(out))
    assert settings.ssh_user == "ceph" and settings.autopilot_enabled is False


def _compose():
    return yaml.safe_load(COMPOSE.read_text())


def test_the_customer_stack_never_mounts_main_host_paths_or_publishes_backends():
    services = _compose()["services"]
    assert set(services) == {"postgres", "rabbitmq", "dashboard-web", "watcher", "remediation-watcher", "worker"}
    for name, service in services.items():
        for volume in service.get("volumes", []):
            assert not volume.startswith(("/root", "./")), (name, volume)
        if name != "dashboard-web":
            assert "ports" not in service, name
    assert services["dashboard-web"]["ports"] == ["${CEPH_AI_DASHBOARD_BIND:-0.0.0.0:8000}:8000"]
    for name in ("dashboard-web", "watcher", "remediation-watcher", "worker"):
        assert services[name]["image"].startswith("${CEPH_AI_IMAGE:?"), name
        assert services[name]["user"] == "10001:10001" and services[name]["read_only"] is True
    assert services["worker"]["environment"]["SSH_KEY_PATH"] == "/run/ceph-ai/credentials/ssh/id_ed25519"


def test_the_installer_is_valid_bash_and_never_sources_an_env_file():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    text = SCRIPT.read_text()
    assert "source " not in text and "\n. " not in text
    assert "ALLOW_UNVERIFIED" not in text
