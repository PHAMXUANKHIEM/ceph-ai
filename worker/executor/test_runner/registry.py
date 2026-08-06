"""Epic 10 Story 10.6: combines Groups A-D into one registry and builds the
first real TestRunContext (Stories 10.3-10.5 only ever built one by hand in
test fixtures -- see tests/test_test_runner_group_*.py). Kept separate from
framework.py because building a TestRunContext needs shared.cluster_nodes /
shared.models, which framework.py and the group_*.py modules have no other
reason to import.
"""

from __future__ import annotations

from typing import Optional

from shared.cluster_nodes import configured_nodes
from shared.models import TestRunnerConfig
from worker.executor.test_runner.framework import TestCase, TestRunContext
from worker.executor.test_runner.group_a import GROUP_A_TESTS
from worker.executor.test_runner.group_b import GROUP_B_TESTS
from worker.executor.test_runner.group_c import GROUP_C_TESTS
from worker.executor.test_runner.group_d import GROUP_D_TESTS
from worker.executor.test_runner.group_e import GROUP_E_TESTS

__all__ = [
    "ALL_TEST_CASES",
    "TEST_CASES_BY_ID",
    "build_test_run_context",
    "filter_selected",
]

ALL_TEST_CASES: list[type[TestCase]] = [
    *GROUP_A_TESTS,
    *GROUP_B_TESTS,
    *GROUP_C_TESTS,
    *GROUP_D_TESTS,
    *GROUP_E_TESTS,
]

# Every class is default-constructible (no __init__ override anywhere in
# group_a/b/c/d.py -- id/name/group/priority/background are plain class
# attributes), so instantiating once here and reusing the same instance
# across requests is safe: TestCase.run()/start()/poll() are stateless on
# `self`, all per-run state lives in the ctx/state arguments instead.
TEST_CASES_BY_ID: dict[str, TestCase] = {tc.id: tc() for tc in ALL_TEST_CASES}


def build_test_run_context(config: Optional[TestRunnerConfig]) -> TestRunContext:
    """Builds a TestRunContext from the live cluster node config
    (shared.cluster_nodes.configured_nodes(), roles are the uppercase
    "MON"/"MGR"/"OSD"/"RGW" strings) plus the Story 10.2 TestRunnerConfig
    singleton row (client_host, rgw_endpoint_*). `config` may be None (no
    row saved yet) -- every TestRunnerConfig-derived field is then None,
    same as an unconfigured row.
    """
    nodes = configured_nodes()
    mon_host = next((n["host"] for n in nodes if "MON" in n["roles"]), None)
    osd_hosts = [n["host"] for n in nodes if "OSD" in n["roles"]]
    rgw_hosts = [n["host"] for n in nodes if "RGW" in n["roles"]]
    return TestRunContext(
        mon_host=mon_host,
        osd_hosts=osd_hosts,
        rgw_hosts=rgw_hosts,
        client_host=config.client_host if config else None,
        rgw_endpoint_zone_a=config.rgw_endpoint_zone_a if config else None,
        rgw_endpoint_zone_b=config.rgw_endpoint_zone_b if config else None,
        rgw_endpoint_vip=config.rgw_endpoint_vip if config else None,
    )


def filter_selected(
    test_cases: dict[str, TestCase], test_groups: list[str], priorities: list[str]
) -> list[TestCase]:
    """An empty test_groups/priorities list means "show all" for listing
    purposes -- a fresh/never-saved TestRunnerConfig row has both empty by
    construction (Story 10.2's DB default), and nobody saves an explicit
    empty selection intending "run zero tests". Only a genuinely non-empty
    saved list narrows the result.

    Takes `test_cases` as a parameter rather than reading TEST_CASES_BY_ID
    off this module directly -- a caller (dashboard/routes/test_runner.py)
    that imports TEST_CASES_BY_ID via `from ... import` and later
    monkeypatches ITS OWN copy (e.g. in tests) would otherwise have that
    patch silently not apply here, since this function would still close
    over registry.py's own separate module-global binding. Same "qualified
    access, one true binding" lesson Story 10.4 already established for
    shared/test_runner_baselines.py.
    """
    groups = set(test_groups) if test_groups else None
    prios = set(priorities) if priorities else None
    return [
        tc
        for tc in test_cases.values()
        if (groups is None or tc.group.value in groups) and (prios is None or tc.priority.value in prios)
    ]
