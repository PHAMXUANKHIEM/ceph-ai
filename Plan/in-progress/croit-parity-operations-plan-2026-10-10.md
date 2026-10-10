# Kế hoạch: thu hẹp khoảng cách vận hành với croit

**Ngày lập:** 10/10/2026
**Trạng thái:** `IN-PROGRESS`. Đã rà soát; làm lần lượt từ C1.
**Cụm:** CS-LAB là production (`autonomy_environment = production`, Autopilot bật); CS-STAGING
(10.3.54.178, 10.3.52.166, 10.3.54.251, cephadm, fsid `1beef4a8-c2bb-11f1-baa4-fa163ea57b22`) là cụm thử.
Mọi tính năng mới chạy trên CS-STAGING trước, rồi mới được dùng cho CS-LAB.

## 1. So sánh (theo code ở `origin/main` 5aefc8e0, 70 hành động thực thi)

| Tiêu chí | croit | Ceph AI | Khoảng cách |
|---|---|---|---|
| Triển khai | PXE diskless, image OS, GUI cho RBD/CephFS/RGW/NFS/iSCSI/NVMe-oF/SMB, multi-site | Deploy cephadm / ceph-deploy / RPM nội bộ, convert sang cephadm, xoá cụm, đăng ký giám sát | Lớn: chưa mở rộng cụm đang chạy (thêm host/OSD chỉ có lúc deploy), chưa có CephFS/MDS, NFS; RGW chỉ tạo lúc deploy |
| Quản trị ngày-2 | Thay ổ, bảo trì, CRUSH, user/caps, chứng chỉ qua GUI | Mạnh về RBD (tạo, clone, move, QoS, trash, khôi phục), pool, scrub, reshard RGW, OSD in/out, sơ tán OSD sắp hỏng, gỡ node (drain) | Trung bình: chưa có quy trình thay ổ/OSD hỏng trọn vẹn, chưa có bảo trì host + reboot lần lượt, chưa sửa CRUSH rule, chưa có CephFS |
| Cập nhật | Kênh phát hành đã kiểm thử, cập nhật OS theo image, rollback | `upgrade_ceph_cluster` (orch upgrade + cờ noout…), tải gói / gói cục bộ, cổng OS node (prepare/recover/abort) | Nhỏ–trung bình: thiếu nâng cấp thử trên staging trước production |
| Hỗ trợ production, kinh nghiệm | Kỹ sư 24/7, SLA, kinh nghiệm từ nhiều cụm | AI chẩn đoán, runbook, duyệt Telegram, Failure Lab, dự báo | Lớn nhất, không bù hết bằng phần mềm. Số liệu 10/10: 13 case có nhãn operator, học từ log bị chặn (đã sửa ở `cand/log-intel-node-aliases`), dự báo chưa có đánh giá operator |

Ceph AI hơn croit ở: chẩn đoán/đề xuất bằng AI, dự báo tài nguyên, duyệt hành động qua Telegram, nhiều cụm
một giao diện, tự sửa mã qua pipeline, không license.

## 2. Nguyên tắc chung

- Mỗi gói một nhánh `cand/*` + PR; hành động mới vào `worker/policy/action_policy.yaml` ở mức **RISKY**
  (chờ duyệt), không bao giờ SAFE ở lần đầu.
- Hành động nhiều bước theo mẫu `worker/executor/node_removal.py`: các pha có preflight, trạng thái từng
  host, dừng an toàn khi cụm không về trạng thái mong đợi.
- Chỉ cephadm ở bản đầu (CS-LAB và CS-STAGING đều cephadm); ceph-deploy theo
  `ceph-deploy-parity-plan-2026-10-09.md`.
- Kiểm thử thật trên CS-STAGING (có người theo dõi) trước khi bật cho CS-LAB.

## 3. Gói việc

### C1 — Thay ổ / OSD hỏng trọn vẹn
Hiện có: phát hiện OSD sắp hỏng (device health), `evacuate_predicted_failing_osd` (= `osd out`),
`mark_osd_*`, `finalize_osd_release`.
- [ ] Hành động `replace_failed_osd` (RISKY), các pha:
  1. preflight: OSD tồn tại, `ceph osd ok-to-stop`/`safe-to-destroy` sau khi data đã chuyển, không có PG
     inactive, đủ dung lượng còn lại (dùng lại kiểm tra của node_removal);
  2. `ceph orch osd rm <id> --replace` (giữ ID, đánh dấu `destroyed`), chờ `orch osd rm status` xong;
  3. operator thay ổ vật lý rồi xác nhận (bước chờ duyệt thứ hai, có thể bỏ qua nếu ổ vẫn dùng lại được);
  4. `ceph orch device zap <host> <path> --force` + tạo lại OSD cùng ID (`orch daemon add osd` hoặc để
     spec `osd` tự nhận ổ trống);
  5. chờ OSD up/in và PG về `active+clean`.
- [ ] Nối với cảnh báo "OSD sắp hỏng": đề xuất `replace_failed_osd` thay vì chỉ `out`.
- [ ] Thử trên CS-STAGING (ổ `vdb` 20 GB mỗi node).

### C2 — Bảo trì host và reboot lần lượt
Hiện có: `node_os_gate_prepare/recover/abort` (cờ noout/noscrub/nodeep-scrub/nosnaptrim, gỡ MON khỏi
quorum), `hard_reboot_node`.
- [ ] `ceph orch host maintenance enter/exit` cho cephadm (dừng daemon có trật tự, tự đặt noout cho host).
- [ ] Hành động `rolling_node_maintenance` (RISKY): lần lượt từng host — enter → (reboot / cập nhật OS) →
  chờ SSH → exit → chờ `HEALTH_OK` hoặc chỉ còn cảnh báo có từ trước → host tiếp theo; dừng và báo khi một
  host không về.
- [ ] Không bao giờ bảo trì cùng lúc hai host giữ MON nếu làm mất quorum.

### C3 — Mở rộng cụm đang chạy
Hiện có: thêm host/OSD trong `cluster_deploy.py` (chỉ lúc deploy), `node_removal.py` (gỡ).
- [ ] Hành động `add_cluster_nodes` (RISKY): preflight SSH + phiên bản/thời gian, `ceph orch host add`
  (kèm nhãn), thêm OSD trên ổ trống được chọn, cập nhật cấu hình node của Ceph AI (`configured_nodes`).

### C4 — Nâng cấp thử trên staging trước
Hiện có: `upgrade_ceph_cluster*`, cổng OS node.
- [ ] Nâng cấp CS-LAB chỉ được đề xuất khi cùng phiên bản đã chạy trên CS-STAGING ≥ N giờ mà không có
  cảnh báo mới; Failure Lab chạy lại các kịch bản sau nâng cấp staging.

### C5 — Dịch vụ còn thiếu
- [ ] CephFS: `ceph fs volume create`, MDS (`orch apply mds`), xem trạng thái, quota/subvolume cơ bản.
- [ ] NFS (`orch apply nfs`, export) và quản lý RGW sau deploy (`orch apply rgw`, realm/zone cơ bản).

### C6 — Kinh nghiệm vận hành đo được
- [ ] Failure Lab theo lịch hằng đêm trên CS-STAGING (code ở `cand/failure-lab-schedule`).
- [ ] Báo cáo MTTR, tỉ lệ chẩn đoán đúng, tỉ lệ sửa thành công theo họ lỗi (mở rộng FL6.5).
- [ ] Đường chuyển lên người: sự cố ngoài họ lỗi đã học → thẻ Telegram "cần kỹ sư", kèm gói bằng chứng.

## 4. Nhật ký

| Ngày | Việc | Kết quả |
|---|---|---|
| 10/10/2026 | Rà soát và lập kế hoạch | Bảng so sánh ở mục 1; bắt đầu C1 |
