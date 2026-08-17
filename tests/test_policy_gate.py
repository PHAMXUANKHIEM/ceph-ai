from shared.models import ActionClassification
from worker.policy.gate import classify_action


def test_classify_safe_action_id_returns_safe():
    assert classify_action("resync_ntp") == ActionClassification.SAFE


def test_classify_crash_archive_all_returns_safe():
    assert classify_action("crash_archive_all") == ActionClassification.SAFE


def test_classify_risky_action_id_returns_risky():
    assert classify_action("restart_osd_daemon") == ActionClassification.RISKY
    assert classify_action("pg_repair_force") == ActionClassification.RISKY


def test_classify_action_absent_from_both_lists_returns_risky():
    # investigate_manually is a real action_id (in action_ids:) but
    # deliberately absent from both safe: and risky: — must still be RISKY.
    assert classify_action("investigate_manually") == ActionClassification.RISKY


def test_classify_completely_unknown_action_id_returns_risky():
    assert classify_action("this_action_id_does_not_exist_anywhere") == ActionClassification.RISKY


def test_classify_action_id_listed_in_both_safe_and_risky_is_conservative_risky(monkeypatch):
    import worker.policy.gate as gate

    monkeypatch.setattr(gate, "SAFE_ACTION_IDS", frozenset({"conflicted_action"}))
    monkeypatch.setattr(gate, "RISKY_ACTION_IDS", frozenset({"conflicted_action"}))

    assert gate.classify_action("conflicted_action") == ActionClassification.RISKY


def test_current_policy_yaml_has_no_safe_risky_conflicts():
    # Regression guard: the real action_policy.yaml should never list the
    # same action_id as both safe and risky (would trigger gate.py's
    # startup warning and rely on the conservative-override fallback).
    import worker.policy.gate as gate

    assert gate.SAFE_ACTION_IDS.isdisjoint(gate.RISKY_ACTION_IDS)


def test_risky_action_ids_loaded_from_policy_yaml():
    import worker.policy.gate as gate

    assert "restart_osd_daemon" in gate.RISKY_ACTION_IDS
    assert "pg_repair_force" in gate.RISKY_ACTION_IDS
    assert "finalize_osd_release" in gate.RISKY_ACTION_IDS

    import worker.llm.router_client as router_client

    assert "finalize_osd_release" in router_client.VALID_ACTION_IDS


def test_management_action_ids_loaded_from_policy_yaml():
    import worker.policy.gate as gate

    assert gate.VALID_MANAGEMENT_ACTION_IDS == {
        "execute_node_command",
        "create_pool",
        "delete_pool",
        "set_pool_size",
        "set_pool_pg_num",
        "edit_pool",
        "scrub_pool",
        "set_pool_protection",
        "mark_osd_out",
        "mark_osd_in",
        "mark_osd_down",
        "enable_pool_application",
        "rbd_trash_remove",
        "rbd_create_volume",
        "rbd_resize_volume",
        "finalize_pacific_osd_release",
    }


def test_management_action_ids_are_classified_safe():
    for action_id in [
        "create_pool",
        "delete_pool",
        "set_pool_size",
        "set_pool_pg_num",
        "edit_pool",
        "scrub_pool",
        "set_pool_protection",
        "mark_osd_out",
        "mark_osd_in",
        "mark_osd_down",
        "enable_pool_application",
    ]:
        assert classify_action(action_id) == ActionClassification.SAFE


def test_rbd_trash_remove_is_classified_risky():
    # 2026-07-28: unlike every other management_action_ids member above
    # (deliberately kept SAFE per an explicit earlier operator request, only
    # for Chat-with-AI's own extra safeguards), rbd_trash_remove
    # permanently destroys data with no equivalent per-click safeguard on
    # the Volumes page — AD-5's conservative default applies, must always
    # require explicit Dashboard approval.
    assert classify_action("rbd_trash_remove") == ActionClassification.RISKY


def test_rbd_volume_mutations_are_classified_risky():
    assert classify_action("rbd_create_volume") == ActionClassification.RISKY
    assert classify_action("rbd_resize_volume") == ActionClassification.RISKY


def test_finalize_pacific_osd_release_is_classified_risky():
    assert classify_action("finalize_pacific_osd_release") == ActionClassification.RISKY


def test_management_action_ids_disjoint_from_incident_diagnosis_action_ids():
    # Guards the deliberate separation (action_policy.yaml's own comment):
    # management_action_ids must never leak into worker/llm/router_client.py's
    # VALID_ACTION_IDS (the Incident-diagnosis tool schema enum).
    import worker.policy.gate as gate
    from worker.llm.router_client import VALID_ACTION_IDS

    assert gate.VALID_MANAGEMENT_ACTION_IDS.isdisjoint(VALID_ACTION_IDS)


def test_cluster_upgrade_action_ids_loaded_from_policy_yaml():
    import worker.policy.gate as gate

    assert gate.VALID_CLUSTER_UPGRADE_ACTION_IDS == {
        "upgrade_ceph_cluster",
        "upgrade_ceph_cluster_package_download",
        "upgrade_ceph_cluster_package_local",
    }


def test_upgrade_ceph_cluster_is_classified_risky():
    # AD-5: a live cluster upgrade must never be in `safe:` — it always
    # requires an explicit Dashboard approval. Same for both package-based
    # (ceph-deploy) variants — if anything, more reason for approval, since
    # there's no cephadm orchestrator gating them.
    assert classify_action("upgrade_ceph_cluster") == ActionClassification.RISKY
    assert classify_action("upgrade_ceph_cluster_package_download") == ActionClassification.RISKY
    assert classify_action("upgrade_ceph_cluster_package_local") == ActionClassification.RISKY


def test_cluster_upgrade_action_ids_disjoint_from_other_families():
    import worker.policy.gate as gate
    from worker.llm.router_client import VALID_ACTION_IDS

    assert gate.VALID_CLUSTER_UPGRADE_ACTION_IDS.isdisjoint(VALID_ACTION_IDS)
    assert gate.VALID_CLUSTER_UPGRADE_ACTION_IDS.isdisjoint(gate.VALID_MANAGEMENT_ACTION_IDS)


def test_cluster_deploy_action_ids_loaded_from_policy_yaml():
    import worker.policy.gate as gate

    assert gate.VALID_CLUSTER_DEPLOY_ACTION_IDS == {
        "deploy_cluster_cephadm",
        "deploy_cluster_ceph_deploy",
        "deploy_cluster_rpm_local",
        "delete_cluster_cephadm",
        "delete_cluster_manual",
        "convert_cluster_to_cephadm",
        "restore_cluster_from_backup",
        "node_os_gate_prepare",
        "node_os_gate_abort",
        "node_os_gate_recover",
    }


def test_deploy_cluster_action_ids_are_classified_risky():
    # AD-5: bootstrapping a brand-new cluster (package installs, MON/MGR/OSD
    # creation, real disk formatting for the non-cephadm methods) must never
    # be in `safe:` — always requires an explicit Dashboard approval.
    assert classify_action("deploy_cluster_cephadm") == ActionClassification.RISKY
    assert classify_action("deploy_cluster_ceph_deploy") == ActionClassification.RISKY
    assert classify_action("deploy_cluster_rpm_local") == ActionClassification.RISKY
    # 2026-07-26: same reasoning, even more so — tearing down a real
    # cluster (and optionally wiping OSD disk data) must never be Safe.
    assert classify_action("delete_cluster_cephadm") == ActionClassification.RISKY
    assert classify_action("delete_cluster_manual") == ActionClassification.RISKY
    # 2026-07-28: converts every daemon's management style in place on a
    # live cluster — same conservative-by-default reasoning, never Safe.
    assert classify_action("convert_cluster_to_cephadm") == ActionClassification.RISKY
    # 2026-07-31 (Story 9.7): rebuilds a cluster from scratch AND overwrites
    # its RBD data from backup — same conservative-by-default reasoning.
    assert classify_action("restore_cluster_from_backup") == ActionClassification.RISKY


def test_cluster_deploy_action_ids_disjoint_from_other_families():
    import worker.policy.gate as gate
    from worker.llm.router_client import VALID_ACTION_IDS

    assert gate.VALID_CLUSTER_DEPLOY_ACTION_IDS.isdisjoint(VALID_ACTION_IDS)
    assert gate.VALID_CLUSTER_DEPLOY_ACTION_IDS.isdisjoint(gate.VALID_MANAGEMENT_ACTION_IDS)
    assert gate.VALID_CLUSTER_DEPLOY_ACTION_IDS.isdisjoint(gate.VALID_CLUSTER_UPGRADE_ACTION_IDS)
    assert gate.VALID_CLUSTER_DEPLOY_ACTION_IDS.isdisjoint(gate.VALID_PATCH_ACTION_IDS)


def test_volume_perf_action_ids_loaded_from_policy_yaml():
    import worker.policy.gate as gate

    assert gate.VALID_VOLUME_PERF_ACTION_IDS == {"volume_perf_sweep", "vm_perf_benchmark"}


def test_volume_perf_sweep_is_classified_risky():
    # 2026-07-29: writes real (scratch-only) I/O load to the cluster for
    # several minutes — must never be Safe, same conservative default as
    # every other action_id family in this file.
    assert classify_action("volume_perf_sweep") == ActionClassification.RISKY
    assert classify_action("vm_perf_benchmark") == ActionClassification.RISKY


def test_volume_perf_action_ids_disjoint_from_other_families():
    import worker.policy.gate as gate
    from worker.llm.router_client import VALID_ACTION_IDS

    assert gate.VALID_VOLUME_PERF_ACTION_IDS.isdisjoint(VALID_ACTION_IDS)
    assert gate.VALID_VOLUME_PERF_ACTION_IDS.isdisjoint(gate.VALID_MANAGEMENT_ACTION_IDS)
    assert gate.VALID_VOLUME_PERF_ACTION_IDS.isdisjoint(gate.VALID_CLUSTER_UPGRADE_ACTION_IDS)
    assert gate.VALID_VOLUME_PERF_ACTION_IDS.isdisjoint(gate.VALID_PATCH_ACTION_IDS)
    assert gate.VALID_VOLUME_PERF_ACTION_IDS.isdisjoint(gate.VALID_CLUSTER_DEPLOY_ACTION_IDS)


def test_device_health_action_ids_loaded_from_policy_yaml():
    import worker.policy.gate as gate

    assert gate.VALID_DEVICE_HEALTH_ACTION_IDS == {"evacuate_predicted_failing_osd"}


def test_evacuate_predicted_failing_osd_is_classified_risky():
    # Story C: moves real data off a live OSD, system-proposed with no
    # operator having typed/reviewed the osd_id first — must never be Safe,
    # even though mark_osd_out (a DIFFERENT action_id, Chat-only) is.
    assert classify_action("evacuate_predicted_failing_osd") == ActionClassification.RISKY


def test_device_health_action_ids_disjoint_from_other_families():
    import worker.policy.gate as gate
    from worker.llm.router_client import VALID_ACTION_IDS

    assert gate.VALID_DEVICE_HEALTH_ACTION_IDS.isdisjoint(VALID_ACTION_IDS)
    assert gate.VALID_DEVICE_HEALTH_ACTION_IDS.isdisjoint(gate.VALID_MANAGEMENT_ACTION_IDS)
    assert gate.VALID_DEVICE_HEALTH_ACTION_IDS.isdisjoint(gate.VALID_CLUSTER_UPGRADE_ACTION_IDS)
    assert gate.VALID_DEVICE_HEALTH_ACTION_IDS.isdisjoint(gate.VALID_PATCH_ACTION_IDS)
    assert gate.VALID_DEVICE_HEALTH_ACTION_IDS.isdisjoint(gate.VALID_CLUSTER_DEPLOY_ACTION_IDS)
    assert gate.VALID_DEVICE_HEALTH_ACTION_IDS.isdisjoint(gate.VALID_VOLUME_PERF_ACTION_IDS)
    assert gate.VALID_DEVICE_HEALTH_ACTION_IDS.isdisjoint(gate.VALID_BACKUP_ACTION_IDS)


def test_bluestore_action_ids_loaded_from_policy_yaml():
    import worker.policy.gate as gate

    assert gate.VALID_BLUESTORE_ACTION_IDS == {"bluestore_omap_quick_fix"}


def test_bluestore_omap_quick_fix_is_classified_risky():
    # Known historical ceph-bluestore-tool corruption risk (see
    # worker/executor/commands.py's own comment) — must never be Safe.
    assert classify_action("bluestore_omap_quick_fix") == ActionClassification.RISKY


def test_bluestore_action_ids_disjoint_from_other_families():
    import worker.policy.gate as gate
    from worker.llm.router_client import VALID_ACTION_IDS

    assert gate.VALID_BLUESTORE_ACTION_IDS.isdisjoint(VALID_ACTION_IDS)
    assert gate.VALID_BLUESTORE_ACTION_IDS.isdisjoint(gate.VALID_MANAGEMENT_ACTION_IDS)
    assert gate.VALID_BLUESTORE_ACTION_IDS.isdisjoint(gate.VALID_CLUSTER_UPGRADE_ACTION_IDS)
    assert gate.VALID_BLUESTORE_ACTION_IDS.isdisjoint(gate.VALID_PATCH_ACTION_IDS)
    assert gate.VALID_BLUESTORE_ACTION_IDS.isdisjoint(gate.VALID_CLUSTER_DEPLOY_ACTION_IDS)
    assert gate.VALID_BLUESTORE_ACTION_IDS.isdisjoint(gate.VALID_VOLUME_PERF_ACTION_IDS)
    assert gate.VALID_BLUESTORE_ACTION_IDS.isdisjoint(gate.VALID_BACKUP_ACTION_IDS)
    assert gate.VALID_BLUESTORE_ACTION_IDS.isdisjoint(gate.VALID_DEVICE_HEALTH_ACTION_IDS)
