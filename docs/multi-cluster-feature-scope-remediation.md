# Multi-cluster feature scope remediation

Mục tiêu: sau khi người dùng chọn một cluster, mọi màn hình phải hoặc thao tác đúng cluster đó, hoặc khóa rõ ràng nếu tính năng chỉ hỗ trợ cluster mặc định. Không route nào được âm thầm rơi về cluster mặc định.

Quy ước trạng thái:

- `[ ]` Chưa làm.
- `[~]` Đang làm.
- `[x]` Đã code và test đạt.

## 1. Nền tảng cluster scope và guard

- [x] 1.1 Tạo capability/guard dùng chung cho route chỉ hỗ trợ cluster mặc định.
- [x] 1.2 Guard trả HTTP 409 với tên tính năng và cluster đang chọn, thay vì âm thầm fallback.
- [ ] 1.3 Mọi POST/action phải resolve cluster tại thời điểm submit; không âm thầm dùng `.env` khi session đang chọn cluster phụ.
- [x] 1.4 Approve/reject resolve Incident và redirect về đúng `?cluster=`; các form scoped giữ cluster id/query.

## 2. Các trang chỉ hỗ trợ cluster mặc định

Các trang này chưa có data model/executor đủ để chạy multi-cluster. Trong giai đoạn này phải hiển thị cluster thực tế, cảnh báo và khóa hành động khi đang chọn cluster phụ.

- [x] 2.1 Deploy Cluster là luồng tạo hạ tầng mới, không tác động cluster đang chọn; giữ độc lập và không giả vờ scoped.
- [x] 2.2 Delete Cluster vật lý: fail-closed khi session đang chọn cluster phụ.
- [x] 2.3 Convert to Cephadm: fail-closed khi session đang chọn cluster phụ.
- [x] 2.4 Patch Ceph: fail-closed khi session đang chọn cluster phụ.
- [x] 2.5 Restore Cluster: fail-closed khi session đang chọn cluster phụ.
- [x] 2.6 Settings ghi rõ kết nối Ceph là cụm mặc định; OpenStack có selector và lưu theo cluster.
- [x] 2.7 Telegram ghi rõ phạm vi global/default và dẫn sang cấu hình riêng của cluster phụ.

## 3. Nodes, RGW logs và Bucket Access Log

- [x] 3.1 Trang Nodes dùng node của cluster đang chọn (default vẫn đọc Settings live).
- [x] 3.2 API metrics/RGW log resolve session cluster và whitelist host theo đúng cluster.
- [x] 3.3 Collector node metrics/RGW log dùng SSH credential, exec mode và container của cluster đã chọn.
- [x] 3.4 Bucket Access Log đọc/lưu đúng RGW nodes/config của cluster; chỉ cluster mặc định ghi `.env`.

## 4. CRUSH Map

- [ ] 4.1 Thêm `cluster_id` vào snapshot CRUSH và migration database.
- [ ] 4.2 Watcher ghi snapshot theo cluster.
- [ ] 4.3 API tree/history lọc theo cluster đang chọn.
- [ ] 4.4 UI có cluster selector và trạng thái empty riêng từng cluster.

## 5. Backup và restore volume

- [x] 5.1 Danh sách queue/history/anomaly lọc theo cluster.
- [x] 5.2 Tracked images lấy từ policy mặc định hoặc cấu hình `Cluster.backup_*` tương ứng.
- [x] 5.3 Run-now, metadata backup và restore tạo Incident/Action có `cluster_id` đúng.
- [x] 5.4 Resolve MON theo cluster; Worker đã resolve SSH/backend từ Incident cluster scope.
- [x] 5.5 Progress API chỉ trả action của cluster đang xem.

## 6. OpenStack và Volumes

- [x] 6.1 Cho phép lưu/test OpenStack Controller/Compute theo từng cluster.
- [x] 6.2 Copy Ceph config/keyring dùng OpenStack config của cluster đang chọn.
- [x] 6.3 Volumes ghi rõ action benchmark VM chưa hỗ trợ cluster phụ và khóa trước submit.
- [x] 6.4 VM SSH/performance được khóa rõ ràng trên cluster phụ; default dùng Controller đã chọn.

## 7. Chat/AI và Action approval

- [ ] 7.1 Chat session/message gắn cluster scope.
- [ ] 7.2 Danh sách node và command validation dùng cluster của chat/action.
- [x] 7.3 Approve/reject giữ cluster context và quay về đúng dashboard cluster.
- [ ] 7.4 Worker luôn resolve cluster từ Incident/Action; thêm regression test chống fallback nhầm cluster mặc định.

## 8. Toggle, lifecycle và restart service

- [x] 8.1 Bật/tắt cluster restart cả Watcher và Worker.
- [x] 8.2 Kiểm tra kết quả restart ở create/toggle/delete/backup config; không báo thành công giả.
- [x] 8.3 Resolver/scheduler loại cluster inactive; approval chặn Action của cluster inactive/missing.
- [x] 8.4 Cluster phụ có form sửa; test health thành công trước khi lưu và restart Watcher/Worker.

## 9. UI nhất quán và kiểm thử

- [ ] 9.1 Header các trang lớn hiển thị cluster backend thực sự đang dùng.
- [x] 9.2 Navigation giữ cluster bằng session; các form scoped quan trọng mang cluster id/query rõ ràng.
- [x] 9.3 Sửa link Clusters bị thiếu và import test non-admin không ổn định.
- [ ] 9.4 Thêm ma trận test: cluster mặc định, cluster phụ hoạt động, cluster phụ bị vô hiệu hóa.
- [ ] 9.5 Chạy toàn bộ test liên quan và ghi kết quả cuối cùng tại đây.

## Nhật ký hoàn thành

- 2026-08-14: Tạo checklist sau audit ban đầu. Hai lỗi test hiện hữu: Settings thiếu link Clusters; test non-admin import module không ổn định.
- 2026-08-14: Tách resolver khỏi route `incidents` để loại vòng import; thêm fail-closed guard cho Delete/Convert/Patch/Restore. Regression tests đạt.
- 2026-08-14: Nodes/Metrics/RGW log đã dùng cluster scope thật; `tests/test_dashboard_nodes.py`: 10 passed.
- 2026-08-14: Bucket Access Log scoped cả đọc, stats, SSH và lưu RGW config; 40 regression tests đạt.
- 2026-08-14: Backup UI/action/progress scoped theo cluster; test cũ 20 passed. OpenStack settings hỗ trợ từng cluster; OpenStack + Volumes suite không có failure.
- 2026-08-14: Lifecycle restart Watcher/Worker được kiểm tra kết quả; navigation Clusters suite 21 passed. CRUSH Map và Chat/AI tạm fail-closed trên cluster phụ để loại nguy cơ fallback trong lúc chờ data-model scope.
- 2026-08-14: Thêm edit/test connection cho cluster phụ, chặn approve khi cluster inactive, redirect approve/reject về đúng cluster; Clusters + Actions tests đạt.
- 2026-08-14: Regression bổ sung cho redirect Action theo cluster và chặn approval cluster inactive; `tests/test_dashboard_actions.py` + `tests/test_dashboard_clusters.py`: 54 passed.
