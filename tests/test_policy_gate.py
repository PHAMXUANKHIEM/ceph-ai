from shared.models import ActionClassification, ActionPolicyOverride
from worker.policy.gate import classify_action


def test_classify_safe_action_id_returns_safe():
    assert classify_action("resync_ntp") == ActionClassification.SAFE


def test_classify_crash_archive_all_returns_safe():
    assert classify_action("crash_archive_all") == ActionClassification.SAFE


def test_classify_enable_mon_msgr2_returns_safe():
    assert classify_action("enable_mon_msgr2") == ActionClassification.SAFE


def test_classify_risky_action_id_returns_risky():
    assert classify_action("restart_osd_daemon") == ActionClassification.RISKY


def test_db_override_can_mark_risky_action_safe(db_session):
    db_session.add(ActionPolicyOverride(
        action_id="restart_osd_daemon", classification="SAFE",
        updated_by="admin", reason="Operator accepted bounded recovery",
    ))
    db_session.commit()
    assert classify_action("restart_osd_daemon", session=db_session) == ActionClassification.SAFE


def test_db_override_can_downgrade_destructive_when_admin_saved_it(db_session):
    db_session.add(ActionPolicyOverride(
        action_id="pg_repair_force", classification="SAFE",
        updated_by="admin", reason="explicit admin override",
    ))
    db_session.commit()
    assert classify_action("pg_repair_force", session=db_session) == ActionClassification.SAFE


def test_classify_pg_repair_force_returns_destructive():
    # AI roadmap Pha 0.4 (2026-08-18): moved from risky: to destructive: —
    # PG repair can discard/overwrite a copy of data it decides is
    # inconsistent (same class of risk roadmap Pha 5.4 calls out by name).
    # Still always requires explicit Dashboard approval, same as before —
    # this is a stricter classification, not a behavior change.
    assert classify_action("pg_repair_force") == ActionClassification.DESTRUCTIVE


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
    assert "finalize_osd_release" in gate.RISKY_ACTION_IDS
    # pg_repair_force moved to destructive: (Pha 0.4, 2026-08-18) — see
    # test_classify_pg_repair_force_returns_destructive above.
    assert "pg_repair_force" in gate.DESTRUCTIVE_ACTION_IDS

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
        "rbd_rename_volume",
        "rbd_trash_move_volume",
        "rbd_trash_restore_volume",
            "rbd_trash_purge_all",
            "cinder_attach_volume",
            "cinder_detach_volume",
            "cinder_create_snapshot",
            "finalize_pacific_osd_release",
    }


def test_management_action_ids_are_classified_safe():
    """`delete_pool` KHÔNG còn trong danh sách này — xem
    test_delete_pool_is_classified_destructive bên dưới."""
    for action_id in [
        "create_pool",
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


def test_delete_pool_is_classified_destructive():
    """2026-08-19 (quyết định của operator): chuyển `safe:` -> `destructive:`.

    Trước đó nó THỰC THI NGAY khi confirm trên Chat. Ba lý do đổi, ghi đầy
    đủ trong action_policy.yaml: (1) DoD của Pha 0.4 nêu đích danh "xóa
    pool" là thứ không được nằm trong luồng auto-run; (2) kill-switch —
    lớp chặn cuối cho mọi action tự chạy — đã bị gỡ 2026-08-11 nên "chạy
    ngay" giờ nguy hiểm hơn lúc quyết định cũ được đưa ra; (3) Pha 6 mở
    thêm một đường mà AI đọc dữ liệu người ngoài tác động được rồi đề xuất
    action_id.

    Hệ quả hành vi: confirm trên Chat giờ tạo Action PENDING_APPROVAL thay
    vì thực thi — operator vẫn xem lệnh đã resolve, rồi Duyệt lần hai trên
    Dashboard.
    """
    assert classify_action("delete_pool") == ActionClassification.DESTRUCTIVE


def test_delete_pool_stays_proposable_from_chat():
    """Chỉ đổi PHÂN LOẠI, không gỡ khỏi enum — Chat vẫn đề xuất được, chỉ
    khác là phải Duyệt thêm một bước."""
    import worker.policy.gate as gate

    assert "delete_pool" in gate.VALID_MANAGEMENT_ACTION_IDS


def test_rbd_trash_remove_is_classified_destructive():
    # 2026-07-28: unlike every other management_action_ids member above
    # (deliberately kept SAFE per an explicit earlier operator request, only
    # for Chat-with-AI's own extra safeguards), rbd_trash_remove
    # permanently destroys data with no equivalent per-click safeguard on
    # the Volumes page. Moved risky: -> destructive: in Pha 0.4
    # (2026-08-18) — always required explicit Dashboard approval either
    # way, this is a stricter classification, not a behavior change.
    assert classify_action("rbd_trash_remove") == ActionClassification.DESTRUCTIVE


def test_rbd_volume_mutations_are_classified_risky():
    assert classify_action("rbd_create_volume") == ActionClassification.RISKY
    assert classify_action("rbd_resize_volume") == ActionClassification.RISKY
    assert classify_action("rbd_rename_volume") == ActionClassification.RISKY
    assert classify_action("rbd_trash_move_volume") == ActionClassification.RISKY
    assert classify_action("rbd_trash_restore_volume") == ActionClassification.RISKY
    assert classify_action("cinder_attach_volume") == ActionClassification.RISKY
    assert classify_action("cinder_detach_volume") == ActionClassification.RISKY
    assert classify_action("cinder_create_snapshot") == ActionClassification.RISKY


def test_rbd_trash_purge_all_is_classified_destructive():
    # Pha 0.4 (2026-08-18): mass-purges every trashed image in a pool —
    # moved risky: -> destructive:, same "stricter label, not a behavior
    # change" reasoning as rbd_trash_remove above.
    assert classify_action("rbd_trash_purge_all") == ActionClassification.DESTRUCTIVE


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
    # 2026-07-28: converts every daemon's management style in place on a
    # live cluster — same conservative-by-default reasoning, never Safe.
    assert classify_action("convert_cluster_to_cephadm") == ActionClassification.RISKY


def test_delete_and_restore_cluster_action_ids_are_classified_destructive():
    # 2026-07-26, moved risky: -> destructive: in Pha 0.4 (2026-08-18):
    # irreversibly tears down a real cluster (and optionally wipes OSD disk
    # data) — always required explicit approval either way, this is a
    # stricter classification, not a behavior change.
    assert classify_action("delete_cluster_cephadm") == ActionClassification.DESTRUCTIVE
    assert classify_action("delete_cluster_manual") == ActionClassification.DESTRUCTIVE
    # 2026-07-31 (Story 9.7), same Pha 0.4 move: rebuilds a cluster from
    # scratch AND overwrites its RBD data from backup.
    assert classify_action("restore_cluster_from_backup") == ActionClassification.DESTRUCTIVE


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
