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
READ_ONLY_SERVICES = ("dashboard-web", "telegram-ai", "watcher", "remediation-watcher")


def _sources(service):
    return [str(volume).split(":", 1)[0] for volume in SERVICES[service].get("volumes", [])]


def _mounts(service):
    """(source, destination) for every bind mount of ``service``."""
    pairs = []
    for volume in SERVICES[service].get("volumes", []):
        parts = str(volume).split(":")
        if len(parts) >= 2:
            pairs.append((parts[0], parts[1]))
    return pairs


def _masked(service):
    """Secret dirs hidden by a tmpfs or by an over-mount from another source."""
    masked = {str(entry).split(":", 1)[0] for entry in SERVICES[service].get("tmpfs", [])}
    masked |= {dest for source, dest in _mounts(service) if dest in SECRET_DIRS and source != dest}
    return masked


def test_known_services_are_covered():
    assert set(SERVICES) == {
        "dashboard-web", "telegram-ai", "full-executor", "watcher", "remediation-watcher",
        "vault-monitor", "worker",
    }


def test_the_default_cluster_has_an_incident_creator():
    """watcher.main leaves default-cluster health Incidents to remediation_main."""
    service = SERVICES["remediation-watcher"]
    assert service["command"] == ["python", "-m", "watcher.remediation_main"]
    assert "/run/ceph-ai" in _sources("remediation-watcher")
    assert "remediation-watcher" in service["healthcheck"]["test"][1]
    deploy = (ROOT / "scripts/deploy/restart_container_stack.sh").read_text(encoding="utf-8")
    up = (ROOT / "container-up").read_text(encoding="utf-8")
    assert "remediation-watcher" in deploy.split("SERVICES=(", 1)[1].split(")", 1)[0]
    assert "remediation-watcher" in up.split("services=(dashboard-web", 1)[1].split(")", 1)[0]


@pytest.mark.parametrize("service", [name for name in SERVICES if name != "full-executor"])
def test_services_mounting_the_state_dir_mask_every_secret_dir(service):
    if "/var/lib/ceph-ai" in _sources(service):
        assert set(SECRET_DIRS) <= _masked(service), f"{service} can read full-executor secrets"


@pytest.mark.parametrize("service", READ_ONLY_SERVICES)
def test_read_only_services_never_receive_the_mutation_key_or_executor_accounts(service):
    for source in _sources(service):
        assert not source.startswith("/var/lib/ceph-ai/full-executor-ssh"), service
        assert not source.startswith("/var/lib/ceph-ai/full-executor-accounts/"), service
    key_path = SERVICES[service]["environment"]["SSH_KEY_PATH"]
    key_sources = [source for source, dest in _mounts(service) if dest == key_path]
    assert key_sources, f"{service}: SSH_KEY_PATH {key_path} is not a mounted key"
    assert not key_sources[0].startswith("/var/lib/ceph-ai/full-executor-ssh"), f"{service} uses the mutation key"


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


def test_single_full_shares_the_dashboard_ai_logins():
    """One Settings sign-in must serve Single Full too: the executor mounts the
    same Codex/Claude homes as the other AI services instead of a copy that
    silently goes stale."""
    executor = SERVICES["full-executor"]
    mounts = dict(_mounts("full-executor"))
    assert mounts.get("./.codex-account") == "/app/.codex-account"
    assert mounts.get("./.claude-account") == "/app/.claude-account"
    assert executor["environment"]["CODEX_HOME"] == "/app/.codex-account"
    assert executor["environment"]["CLAUDE_CONFIG_DIR"] == "/app/.claude-account"
    assert "/var/lib/ceph-ai/full-executor-accounts" not in mounts
    for service in ("dashboard-web", "telegram-ai"):
        assert dict(_mounts(service)).get("./.codex-account") == "/app/.codex-account"
