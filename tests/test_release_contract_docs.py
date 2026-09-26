from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_contract_states_the_single_node_limitation():
    contract = (ROOT / "docs/operations/reliability-slo.md").read_text(encoding="utf-8")
    runbook = (ROOT / "docs/production-release-runbook.md").read_text(encoding="utf-8")
    assert "## Availability model: single node, no HA" in contract
    for component in ("RabbitMQ", "PostgreSQL", "/var/backups/ceph-ai", "No automatic failover"):
        assert component in contract
    assert "single node, no HA" in runbook
