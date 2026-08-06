"""Epic 10 Story 10.6: tests for worker/executor/test_runner/registry.py --
ALL_TEST_CASES/TEST_CASES_BY_ID composition, build_test_run_context()'s
role-derivation from configured_nodes(), and filter_selected()'s "empty
selection means all" semantics.
"""

from worker.executor.test_runner import registry
from worker.executor.test_runner.group_a import GROUP_A_TESTS
from worker.executor.test_runner.group_b import GROUP_B_TESTS
from worker.executor.test_runner.group_c import GROUP_C_TESTS
from worker.executor.test_runner.group_d import GROUP_D_TESTS
from worker.executor.test_runner.group_e import GROUP_E_TESTS


def test_all_test_cases_concatenates_every_group():
    assert len(registry.ALL_TEST_CASES) == len(GROUP_A_TESTS) + len(GROUP_B_TESTS) + len(GROUP_C_TESTS) + len(
        GROUP_D_TESTS
    ) + len(GROUP_E_TESTS)
    assert len(registry.ALL_TEST_CASES) == 67 + 52


def test_test_cases_by_id_has_no_duplicate_ids():
    # dict construction would silently drop a duplicate id -- assert the
    # count matches ALL_TEST_CASES exactly rather than just "no crash".
    assert len(registry.TEST_CASES_BY_ID) == len(registry.ALL_TEST_CASES)


def test_test_cases_by_id_instances_are_default_constructed():
    for test_id, instance in registry.TEST_CASES_BY_ID.items():
        assert instance.id == test_id


def test_build_test_run_context_derives_roles_from_configured_nodes(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "mon1,mon2")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "mon1")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "osd1,osd2")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "rgw1")

    ctx = registry.build_test_run_context(None)

    assert ctx.mon_host == "mon1"  # first configured MON host
    assert ctx.osd_hosts == ["osd1", "osd2"]
    assert ctx.rgw_hosts == ["rgw1"]
    assert ctx.client_host is None
    assert ctx.rgw_endpoint_zone_a is None


def test_build_test_run_context_with_no_mon_configured_is_none(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "osd1")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")

    ctx = registry.build_test_run_context(None)

    assert ctx.mon_host is None
    assert ctx.osd_hosts == ["osd1"]
    assert ctx.rgw_hosts == []


def test_build_test_run_context_reads_testrunnerconfig_fields(monkeypatch):
    from config.settings import settings
    from shared.models import TestRunnerConfig

    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")

    config = TestRunnerConfig(
        client_host="client01",
        rgw_endpoint_zone_a="http://rgw-a:7480",
        rgw_endpoint_zone_b="http://rgw-b:7480",
        rgw_endpoint_vip="http://rgw-vip:7480",
    )

    ctx = registry.build_test_run_context(config)

    assert ctx.client_host == "client01"
    assert ctx.rgw_endpoint_zone_a == "http://rgw-a:7480"
    assert ctx.rgw_endpoint_zone_b == "http://rgw-b:7480"
    assert ctx.rgw_endpoint_vip == "http://rgw-vip:7480"


def test_filter_selected_empty_selection_means_all():
    result = registry.filter_selected(registry.TEST_CASES_BY_ID, [], [])
    assert len(result) == len(registry.ALL_TEST_CASES)


def test_filter_selected_narrows_by_group():
    result = registry.filter_selected(registry.TEST_CASES_BY_ID, ["C"], [])
    assert len(result) == len(GROUP_C_TESTS)
    assert all(tc.group.value == "C" for tc in result)


def test_filter_selected_narrows_by_group_and_priority():
    result = registry.filter_selected(registry.TEST_CASES_BY_ID, ["A"], ["P1"])
    assert all(tc.group.value == "A" and tc.priority.value == "P1" for tc in result)
    expected_count = len([cls for cls in GROUP_A_TESTS if cls.priority.value == "P1"])
    assert len(result) == expected_count
    assert expected_count > 0  # sanity: this fixture assumption must hold for the assertion above to mean anything
