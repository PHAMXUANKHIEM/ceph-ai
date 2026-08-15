# Vitastor AI — Roadmap và nhật ký bàn giao

Tài liệu này là nguồn theo dõi chung cho các tính năng AI của Vitastor trong `ceph-ai`.
Mỗi pull request/commit phải cập nhật checkbox, mục **Nhật ký triển khai**, kiểm thử và
commit gần nhất. Không đánh dấu hoàn thành nếu tiêu chí nghiệm thu chưa đạt.

## Quy ước trạng thái

- `[ ]` Chưa làm
- `[~]` Đang làm hoặc mới hoàn thành một phần
- `[x]` Hoàn thành và đã có kiểm thử
- Mọi thao tác thay đổi cluster phải có preview, phân loại rủi ro và phê duyệt.
- AI không được tuyên bố đã chạy lệnh nếu hệ thống không có bằng chứng thực thi.
- Evidence gửi tới AI không chứa SSH key, API key, token hoặc mật khẩu.

## Danh sách tính năng

- [x] **1. AI chẩn đoán nguyên nhân sự cố — bản lõi hoàn thành**
  - [x] Thu thập evidence read-only: status, pool, OSD, image và lỗi từng nguồn.
  - [x] Lưu diagnostic run bền vững với trạng thái `RUNNING/COMPLETED/FAILED` và evidence.
  - [x] Sinh chẩn đoán có nguyên nhân, ảnh hưởng, độ tin cậy, bằng chứng và bước xử lý.
  - [x] Hiển thị chẩn đoán trên Dashboard, không tự thực thi lệnh.
  - [x] Dùng chung output contract cho Router API, Codex và Claude.
  - [x] Kiểm thử persistence, lỗi AI, JSON xấu và không rò rỉ credential.
  - [ ] Mở rộng sau: PG chi tiết, correlation metric và lifecycle incident tự động.
- [x] **2. Baseline động và phát hiện bất thường** theo cluster/pool/OSD/volume/khung giờ.
  - [x] Chuẩn hoá metric cho bốn loại entity, không đưa credential vào sample.
  - [x] Median/MAD robust, ngưỡng tương đối và ngưỡng sàn chống nhiễu/outlier.
  - [x] Ưu tiên baseline cùng khung giờ khi đủ mẫu, fallback cửa sổ gần nhất.
  - [x] Không coi lần tải đầu tiên sau baseline idle là IOPS/bandwidth anomaly.
  - [x] Lifecycle `OPEN/RESOLVED`, persistence, Telegram transition và Dashboard API/UI.
  - [x] Retention dùng chung chính sách metric Vitastor.
- [ ] **3. Dự báo đầy dung lượng** và thời gian tới các mốc 80/90/95%.
- [~] **4. Vòng lặp khắc phục đóng (closed-loop remediation)** — nền tảng cho bảo trì OSD.
  - [x] Model `VitastorRemediationAction` + `VitastorAuditEntry`, migration riêng, cô lập khỏi Ceph.
  - [x] Policy SAFE/RISKY bảo thủ mặc định (AD-5) + `action_id` đóng, không có shell tự do.
  - [x] Command builder đóng (start/restart OSD·mon·etcd, resync_time) + allowlist host khi thực thi.
  - [x] Proposer tất định từ telemetry: OSD `up:false` → đề xuất `start_osd_service` (chờ duyệt).
  - [x] Watcher tự sinh đề xuất (dedup), auto-run SAFE, cảnh báo Telegram khi có RISKY chờ duyệt.
  - [x] Dashboard: thẻ Khắc phục (Duyệt/Từ chối) + Nhật ký hành động; API approve/reject/audit gated theo Vitastor admin.
  - [ ] Mở rộng sau: dry-run/reweight, theo dõi rebalance, phê duyệt qua nút Telegram, tự huỷ đề xuất khi tín hiệu đã hết.
  - Xem thiết kế chi tiết: `docs/vitastor-remediation.md`.
- [ ] **5. Trợ lý tối ưu pool và PG** với mô phỏng tác động trước thay đổi.
- [ ] **6. Quản lý scrub thông minh** theo tải và phát hiện inconsistent/corrupted object.
- [ ] **7. Phân tích object lỗi** bằng `describe`; `fix` luôn là thao tác rủi ro cao.
- [ ] **8. AI quản lý volume/snapshot**: stale volume, retention, flatten/merge dependency.
- [ ] **9. Prometheus và biểu đồ lịch sử nâng cao**: percentile, correlation và export.
- [ ] **10. Incident timeline và AI postmortem** có audit đầy đủ.
- [ ] **11. AI capacity planner** từ mục tiêu VM/workload/failure domain.
- [ ] **12. Safe Autopilot** với ba cấp tự động, một lần duyệt và hai bước xác nhận.

## Mục 1 — Thiết kế triển khai

### Dữ liệu đầu vào

Chỉ dùng kết quả lệnh đọc từ `vitastor-cli --json`: `status`, `ls-pools --stats`,
`osd-tree -l`, `ls -l`, sau đó bổ sung `pg-list` ở pha kế tiếp. Credential kết nối
không được đưa vào prompt hoặc response.

### Output bắt buộc

Chẩn đoán phải có: health, nguyên nhân có khả năng nhất, mức ảnh hưởng, confidence,
bằng chứng, các bước kiểm tra/xử lý theo thứ tự, command preview và cảnh báo an toàn.
Command preview chỉ là văn bản; endpoint chẩn đoán không có khả năng thực thi.

### Tiêu chí hoàn thành

1. Có bản ghi chẩn đoán bền vững gắn với Vitastor cluster.
2. Dashboard cho phép chạy và xem lại chẩn đoán.
3. Cả Router API, Codex và Claude dùng chung contract output.
4. AI lỗi không làm mất evidence và có trạng thái `FAILED` rõ ràng.
5. Test tự động chứng minh phân quyền sản phẩm, persistence và read-only boundary.

## Nhật ký triển khai

| Ngày | Mục | Trạng thái | Thay đổi | Kiểm thử | Commit |
|---|---:|---|---|---|---|
| 2026-08-13 | 1 | Hoàn thành bản lõi | DiagnosticRun, migration, evidence read-only, provider-neutral JSON contract, API và Dashboard UI | `25 passed` (Vitastor diagnosis/dashboard/client/monitor), JS syntax, compileall, Alembic 1 head | Chưa commit |
| 2026-08-13 | 2 | Hoàn thành | Baseline median/MAD theo entity và khung giờ, anomaly lifecycle, Telegram, API và Dashboard table | `45 passed` tập trung; JS syntax, compileall, Alembic 1 head | Chưa commit |
| 2026-08-15 | 4 | Nền tảng hoàn thành | Closed-loop remediation: model + audit + migration (head `d9a1c7b3e204`), policy SAFE/RISKY đóng, command builder + allowlist, proposer down-OSD, wiring watcher, route approve/reject/audit, thẻ Dashboard | `16 passed` (test_vitastor_remediation) + `45 passed` (monitor/dashboard/auth/client hồi quy); `py_compile` sạch; Alembic 1 head | Chưa commit |

## Ghi chú bàn giao

- Dashboard Vitastor và Watcher đang được phát triển song song; luôn kiểm tra `git status`
  và không ghi đè thay đổi chưa commit của người khác.
- Migration hiện tại có một Alembic head; migration mới phải nối từ head đang có tại
  thời điểm tạo và chạy `alembic heads` trước khi commit.
- Test đầy đủ của repository có một số lỗi hạ tầng/collection cũ; luôn ghi riêng kết quả
  test tập trung của Vitastor và lỗi ngoài phạm vi.
