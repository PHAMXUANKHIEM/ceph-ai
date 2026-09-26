"""Credential boundaries between services in compose.yaml (plan 6.2)."""

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SERVICES = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]
SECRET_DIRS = (
    "/var/lib/ceph-ai/full-executor-ssh",
    "/var/lib/ceph-ai/full-executor-secrets",
    "/var/lib/ceph-ai/full-executor-accounts",
)
READ_ONLY_SERVICES = ("dashboard-web", "telegram-ai", "watcher")
READ_ONLY_KEY = "/run/ceph-ai/credentials/readonly/id_ed25519"


def _sources(service):
    return [str(volume).split(":", 1)[0] for volume in SERVICES[service].get("volumes", [])]


def _masked(service):
    return {str(entry).split(":", 1)[0] for entry in SERVICES[service].get("tmpfs", [])}


def test_known_services_are_covered():
    assert set(SERVICES) == {"dashboard-web", "telegram-ai", "full-executor", "watcher", "vault-monitor", "worker"}


@pytest.mark.parametrize("service", [name for name in SERVICES if name != "full-executor"])
def test_services_mounting_the_state_dir_mask_every_secret_dir(service):
    if "/var/lib/ceph-ai" in _sources(service):
        assert set(SECRET_DIRS) <= _masked(service), f"{service} can read full-executor secrets"


@pytest.mark.parametrize("service", READ_ONLY_SERVICES)
def test_read_only_services_never_receive_the_mutation_key_or_executor_accounts(service):
    for source in _sources(service):
        assert not source.startswith("/var/lib/ceph-ai/full-executor-ssh"), service
        assert not source.startswith("/var/lib/ceph-ai/full-executor-accounts"), service
    assert SERVICES[service]["environment"]["SSH_KEY_PATH"] == READ_ONLY_KEY


def test_only_the_worker_and_single_full_executor_hold_the_mutation_key():
    holders = sorted(
        name for name in SERVICES
        if any(source.startswith("/var/lib/ceph-ai/full-executor-ssh/") for source in _sources(name))
    )
    assert holders == ["full-executor", "worker"]
    assert SERVICES["worker"]["environment"]["SSH_KEY_PATH"] == "/run/ceph-ai/credentials/mutation/id_ed25519"


def test_single_full_token_reaches_only_its_caller_and_the_executor():
    holders = sorted(
        name for name in SERVICES
        if any(source.startswith("/var/lib/ceph-ai/full-executor-secrets/") for source in _sources(name))
    )
    assert holders == ["full-executor", "telegram-ai"]
