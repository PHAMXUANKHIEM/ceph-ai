from pathlib import Path

import yaml

from shared.resource_limits import (
    HOST_RESERVED_CPU,
    JOB_CPU_LIMIT,
    REQUIRED_HOST_CPU,
    SERVICE_CPU_BUDGETS,
    fio_one_cpu_options,
    host_cpu_reserve_is_possible,
    total_service_cpu_budget,
)


ROOT = Path(__file__).resolve().parents[1]


def test_compose_resource_caps_match_policy():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = compose["services"]
    # code-repair is a host-supervised tool, not a Compose service.  Keep it
    # out of the container budget so this policy cannot drift from Compose.
    assert "code-repair" not in services
    assert "code-repair" not in SERVICE_CPU_BUDGETS
    assert set(SERVICE_CPU_BUDGETS) <= set(services)
    assert total_service_cpu_budget() == 5.75
    assert total_service_cpu_budget() + HOST_RESERVED_CPU <= 8

    for name, budget in SERVICE_CPU_BUDGETS.items():
        service = services[name]
        assert float(service["cpus"]) == budget.cpu
        assert service["memswap_limit"] == service["mem_limit"]
        assert service["pids_limit"] > 0

    assert float(services["worker"]["cpus"]) == 2.0
    assert services["worker"]["mem_limit"] == "2g"
    assert float(services["watcher"]["cpus"]) == 1.5
    assert services["watcher"]["mem_limit"] == "1g"


def test_budget_keeps_two_cpu_host_reserve():
    assert host_cpu_reserve_is_possible(8)
    assert not host_cpu_reserve_is_possible(8, extra_cpu=0.3)
    assert not host_cpu_reserve_is_possible(7)
    assert not host_cpu_reserve_is_possible(3)
    assert REQUIRED_HOST_CPU == 8
    assert JOB_CPU_LIMIT == 1.0


def test_fio_job_is_explicitly_one_cpu():
    options = fio_one_cpu_options()
    assert "--cpus_allowed=0" in options
    assert "--cpus_allowed_policy=split" in options
