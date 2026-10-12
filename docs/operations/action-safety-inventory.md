# Kiểm kê bước an toàn của hành động (Plan readiness-gap-review R4)

**Ngày:** 12/10/2026 · **Sinh lại:** `python scripts/action_safety_inventory.py`

Bảng dưới liệt kê mọi hành động trong `worker/policy/action_policy.yaml` và những bước an toàn mà tầng
policy **khai báo** cho nó: contract trong `worker/policy/playbook_registry.py`, hoặc các pha (với hành động
vòng đời) trong `worker/executor/cluster_deploy.py`. Một executor có thể tự kiểm tra thêm bên trong; bảng
này cho thấy những gì policy dựa vào được. "Preflight"/"Postcheck" của hành động vòng đời được suy ra từ tên
pha (`*preflight*`, `*check*`, `*wait*`, `*verify*`, `*health*`, `*_up`).

## Tóm tắt

| | Số hành động |
|---|---:|
| Tổng | 77 (SAFE 17, RISKY 44, DESTRUCTIVE 16) |
| Có playbook contract | 13 |
| Khai báo preflight | 24 |
| Khai báo postcheck | 20 |
| Khai báo rollback | 0 |

## Phát hiện cần xử lý

1. **Chốt node đích của Worker bị tắt dưới pytest** (`_live_action_target_safety_reason`). Ba lỗi chặn hành
   động trên cụm thật chỉ lộ ra khi nghiệm thu trên CS-STAGING (12/10/2026): thay OSD (C1) và thêm node (C3)
   nhắm sai node; mọi hành động Cinder nhắm controller OpenStack. Đã sửa ở `cand/r2-osd-replace-target`,
   `cand/expand-running-cluster`, `cand/r2-cinder-target-allowlist`, mỗi bản có test chạy chốt thật.
   **Việc tiếp:** một test tổng quát dựng đề xuất của mọi route rồi chạy chốt thật cho `target_nodes`.
2. **Duyệt trên Telegram bỏ qua chuỗi xác nhận** của hành động vòng đời. Đã sửa ở
   `cand/telegram-typed-confirmation` (`/xacnhan <mã> <chuỗi>`).
3. **SAFE nhưng tác động dữ liệu:** `set_pool_size` (đổi số bản sao), `mark_osd_out`, `mark_osd_down`,
   `edit_pool`, `retention_sweep_delete` không có contract, preflight hay postcheck khai báo. Autopilot không chạy
   được chúng (cần contract), nhưng SAFE nghĩa là chạy ngay khi operator xác nhận trong chat, không qua duyệt.
   Quyết định 23/07/2026 trong policy; **cần chủ dự án xem lại** (đề xuất: `set_pool_size` khi giảm và
   `mark_osd_down/out` lên RISKY).
4. **DESTRUCTIVE không khai báo preflight + postcheck:** 12/16, gồm `delete_pool`, `rbd_trash_purge_all`,
   `rbd_move_volume`, `delete_cluster_*`, `restore_rbd_image_to_production`. Phần lớn có kiểm tra trong
   executor; cần ghi chúng thành contract để policy và báo cáo thấy được.
5. **Không hành động nào khai báo rollback.** Với hành động có thể đảo (`mark_osd_out` ↔ `mark_osd_in`,
   `set_pool_size`, cờ OSD), contract nên trỏ tới hành động đảo.

## Bảng đầy đủ

| Hành động | Mức | Vòng đời | Contract | Preflight | Postcheck | Rollback | Max targets |
|---|---|---|---|---|---|---|---|
| `backup_delete_manual` | DESTRUCTIVE | — | — | — | — | — | — |
| `backup_metadata_run` | SAFE | — | — | — | — | — | — |
| `backup_repair_missing_copy` | RISKY | — | — | — | — | — | — |
| `bluestore_omap_quick_fix` | RISKY | — | — | — | — | — | — |
| `cephfs_create_subvolume` | RISKY | — | — | — | — | — | — |
| `cephfs_create_volume` | RISKY | — | — | — | — | — | — |
| `cinder_attach_volume` | RISKY | — | — | — | — | — | — |
| `cinder_create_snapshot` | RISKY | — | — | — | — | — | — |
| `cinder_delete_snapshot` | DESTRUCTIVE | — | — | — | — | — | — |
| `cinder_detach_volume` | RISKY | — | — | — | — | — | — |
| `convert_cluster_to_cephadm` | RISKY | ✓ | — | ✓ | ✓ | — | — |
| `crash_archive_all` | SAFE | — | ✓ | ✓ | ✓ | — | 1 |
| `create_pool` | SAFE | — | — | — | — | — | — |
| `deep_scrub_omap_pg` | RISKY | — | ✓ | ✓ | ✓ | — | 1 |
| `delete_cluster_cephadm` | DESTRUCTIVE | ✓ | — | ✓ | — | — | — |
| `delete_cluster_manual` | DESTRUCTIVE | ✓ | — | ✓ | — | — | — |
| `delete_pool` | DESTRUCTIVE | — | — | — | — | — | — |
| `deploy_cluster_ceph_deploy` | RISKY | ✓ | — | ✓ | ✓ | — | — |
| `deploy_cluster_cephadm` | RISKY | ✓ | — | ✓ | ✓ | — | — |
| `deploy_cluster_rpm_local` | RISKY | ✓ | — | ✓ | ✓ | — | — |
| `edit_pool` | SAFE | — | — | — | — | — | — |
| `enable_mon_msgr2` | SAFE | — | ✓ | ✓ | ✓ | — | 1 |
| `enable_pool_application` | SAFE | — | ✓ | ✓ | ✓ | — | 1 |
| `enable_pool_pg_autoscaler` | RISKY | — | ✓ | ✓ | ✓ | — | 1 |
| `evacuate_predicted_failing_osd` | RISKY | — | — | — | — | — | — |
| `execute_node_command` | RISKY | — | — | — | — | — | — |
| `finalize_osd_release` | RISKY | — | ✓ | ✓ | ✓ | — | 1 |
| `finalize_pacific_osd_release` | RISKY | — | — | — | — | — | — |
| `finish_remove_cluster_nodes` | RISKY | ✓ | — | ✓ | — | — | — |
| `finish_replace_osd` | DESTRUCTIVE | ✓ | — | ✓ | ✓ | — | — |
| `hard_reboot_node` | RISKY | — | ✓ | ✓ | ✓ | — | 1 |
| `mark_osd_down` | SAFE | — | — | — | — | — | — |
| `mark_osd_in` | SAFE | — | — | — | — | — | — |
| `mark_osd_out` | SAFE | — | — | — | — | — | — |
| `nfs_create_cluster` | RISKY | — | — | — | — | — | — |
| `nfs_create_export` | RISKY | — | — | — | — | — | — |
| `node_os_gate_abort` | RISKY | — | — | — | — | — | — |
| `node_os_gate_prepare` | RISKY | — | — | — | — | — | — |
| `node_os_gate_recover` | RISKY | — | — | ✓ | — | — | — |
| `pg_repair_force` | DESTRUCTIVE | — | ✓ | — | — | — | 1 |
| `rbd_backup_run` | SAFE | — | — | — | — | — | — |
| `rbd_clone_volume` | RISKY | — | — | — | — | — | — |
| `rbd_copy_volume` | RISKY | — | — | — | — | — | — |
| `rbd_create_volume` | RISKY | — | — | — | — | — | — |
| `rbd_flatten_volume` | DESTRUCTIVE | — | — | — | — | — | — |
| `rbd_move_cleanup_partial` | DESTRUCTIVE | — | — | — | — | — | — |
| `rbd_move_volume` | DESTRUCTIVE | — | — | — | — | — | — |
| `rbd_qos_set` | RISKY | — | — | — | — | — | — |
| `rbd_rename_volume` | RISKY | — | — | — | — | — | — |
| `rbd_resize_volume` | RISKY | — | — | — | — | — | — |
| `rbd_template_mark` | RISKY | — | — | — | — | — | — |
| `rbd_trash_move_volume` | RISKY | — | — | — | — | — | — |
| `rbd_trash_purge_all` | DESTRUCTIVE | — | — | — | — | — | — |
| `rbd_trash_remove` | DESTRUCTIVE | — | — | — | — | — | — |
| `rbd_trash_restore_volume` | RISKY | — | — | — | — | — | — |
| `remove_cluster_nodes` | DESTRUCTIVE | ✓ | — | ✓ | ✓ | — | — |
| `remove_invalid_rgw_default_key` | RISKY | — | ✓ | ✓ | ✓ | — | 1 |
| `replace_failed_osd` | DESTRUCTIVE | ✓ | — | ✓ | ✓ | — | — |
| `reshard_rgw_bucket` | RISKY | — | ✓ | ✓ | ✓ | — | 1 |
| `restart_osd_daemon` | RISKY | — | ✓ | ✓ | ✓ | — | — |
| `restore_cluster_from_backup` | DESTRUCTIVE | ✓ | — | ✓ | ✓ | — | — |
| `restore_drill_execute` | SAFE | — | — | — | — | — | — |
| `restore_rbd_image_as_new` | RISKY | — | — | — | — | — | — |
| `restore_rbd_image_to_production` | DESTRUCTIVE | — | — | — | — | — | — |
| `resync_ntp` | SAFE | — | ✓ | ✓ | ✓ | — | 2 |
| `retention_sweep_delete` | SAFE | — | — | — | — | — | — |
| `rgw_apply_service` | RISKY | — | — | — | — | — | — |
| `scrub_pool` | SAFE | — | — | — | — | — | — |
| `set_pool_pg_num` | SAFE | — | ✓ | ✓ | ✓ | — | 1 |
| `set_pool_protection` | SAFE | — | — | — | — | — | — |
| `set_pool_size` | SAFE | — | — | — | — | — | — |
| `tune_bluestore_slow_ops_warn` | RISKY | — | — | — | — | — | — |
| `upgrade_ceph_cluster` | RISKY | — | — | — | — | — | — |
| `upgrade_ceph_cluster_package_download` | RISKY | — | — | — | — | — | — |
| `upgrade_ceph_cluster_package_local` | RISKY | — | — | — | — | — | — |
| `vm_perf_benchmark` | RISKY | — | — | — | — | — | — |
| `volume_perf_sweep` | RISKY | — | — | — | — | — | — |
