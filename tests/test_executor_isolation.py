import os
from pathlib import Path

import yaml

from dashboard.dual_ai_chat import _provider_process_environment


ROOT = Path(__file__).resolve().parents[1]


def test_full_executor_has_a_non_root_read_only_container_boundary():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["full-executor"]
    volumes = service["volumes"]

    assert service["user"] == "10001:10001"
    assert service["read_only"] is True
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["cap_drop"] == ["ALL"]
    assert "privileged" not in service or service["privileged"] is not True
    assert not any(volume.endswith(":/app:rw") for volume in volumes)
    assert not any(volume.endswith(":/app:ro") for volume in volumes)
    assert "/run/dbus/system_bus_socket:/run/dbus/system_bus_socket" not in volumes
    assert "/var/lib/ceph-ai:/var/lib/ceph-ai:rw" not in volumes


def test_full_access_provider_does_not_receive_parent_service_secrets(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHATBOX_BOT_TOKEN", "must-not-leak")
    monkeypatch.setenv("SINGLE_FULL_EXECUTOR_TOKEN", "must-not-leak")
    monkeypatch.setenv("SESSION_SECRET_KEY", "must-not-leak")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    child_env = _provider_process_environment(
        full_access=True,
        extra_env={
            "DATABASE_URL": "postgresql+psycopg://selected-db",
            "CEPH_AI_SELECTED_CLUSTER_ID": "cluster-1",
        },
    )

    assert child_env["DATABASE_URL"] == "postgresql+psycopg://selected-db"
    assert child_env["CEPH_AI_SELECTED_CLUSTER_ID"] == "cluster-1"
    assert "TELEGRAM_CHATBOX_BOT_TOKEN" not in child_env
    assert "SINGLE_FULL_EXECUTOR_TOKEN" not in child_env
    assert "SESSION_SECRET_KEY" not in child_env
    assert child_env["CEPH_AI_ENV_FILE"] == "/dev/null"
