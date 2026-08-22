# Ceph AI — trạng thái bàn giao

Cập nhật: **2026-08-21**. Tạm dừng phát triển AI sau commit `cddc03e` để
chuyển sang hạng mục khác. Tài liệu này là điểm bắt đầu khi quay lại làm tiếp.

## Phạm vi đã hoàn thành

- Watcher đọc `ceph health detail`, tạo Incident theo từng `ceph_code` và
  tự đóng đúng sự cố khi mã health biến mất.
- Worker hỗ trợ ba đường gọi AI: Claude CLI, Codex app-server và Router API.
  Câu trả lời phải qua schema/action allowlist; model không được sinh shell
  tự do để chạy trực tiếp.
- Action được phân loại SAFE/RISKY/DESTRUCTIVE. SAFE chỉ tự chạy khi có
  command builder đóng; RISKY chờ duyệt; DESTRUCTIVE bị chặn.
- `BLUESTORE_SLOW_OP_ALERT` xác định OSD từ health detail, tìm node chứa OSD,
  nhận diện cephadm hoặc package/ceph-deploy và restart đúng daemon OSD.
- Sau khi lệnh chạy thành công, Incident vào `VERIFYING`; chỉ báo Telegram
  **ĐÃ KHẮC PHỤC** khi health poll xác nhận mã lỗi đã biến mất.
- Log Intelligence thu thập qua SSH hoặc Loki, triage pattern, gọi AI RCA,
  lưu evidence và hiển thị lệnh kiểm tra read-only tất định.
- LogFinding cũ, không còn evidence hoặc trùng do bộ evidence thay đổi giữa
  các cửa sổ được đóng/gộp; không tạo thêm Telegram/action trùng.
- Telegram tách đúng channel từng cluster, gửi health định kỳ 10 phút và
  không nhắc Incident của cluster đã vô hiệu hóa.
- Incident đã kết thúc hoặc thuộc cluster không hoạt động tự hủy Action
  `PENDING/PENDING_APPROVAL`; không đụng action đã duyệt/đã chạy.

## Trạng thái production lúc bàn giao

Server: `10.3.55.213`, checkout `/root/ceph-ai`.

- Commit đang chạy: `cddc03e`.
- Claude bật; Codex và Router API tắt.
- Log Intelligence và phân tích AI bật, nguồn log là Loki.
- Cluster `CS-LAB` hoạt động; `Ceph-Backup-CS-LAB` đã vô hiệu hóa.
- Sau reconciliation ngày 2026-08-21: không còn Action chờ duyệt và không
  còn LogFinding OPEN.

Đây là snapshot bàn giao, không phải invariant. Khi tiếp tục phải truy vấn
DB và kiểm tra health/log production lại, không mặc định các con số vẫn bằng 0.

## Các guardrail quan trọng

1. Không coi SSH exit code 0 là đã sửa xong; phải qua `VERIFYING`.
2. Không tạo action thực thi từ log AI nếu evidence không xác định được
   target. Chỉ đưa lệnh kiểm tra read-only từ catalogue phía server.
3. Không gửi finding nếu evidence đã cũ so với cửa sổ scan hiện tại.
4. Không nhắc hoặc giữ action mở cho cluster `is_active=false`.
5. Không hủy action `APPROVED/EXECUTED/FAILED` trong reconciliation.
6. Claude/Codex đang bật thì thiếu `ROUTER_API_KEY` không phải lỗi cấu hình.

## Phần còn lại khi quay lại làm AI

Roadmap Ceph theo thứ tự ưu tiên mới được theo dõi tại
[`docs/CEPH_AI_ROADMAP.md`](CEPH_AI_ROADMAP.md). Ceph được triển khai trước;
roadmap Vitastor giữ riêng và tiếp tục sau.

### Ceph

- Semantic grouping LogFinding đã có lõi `fault_family` + entity ổn định do
  server sinh; correlation với health Incident đã hoàn thành (2026-08-22).
  Bước kế tiếp là correlation chéo metric và disk signal.
- Hoàn thiện preflight enforcement và rollout production sau khi có đủ
  telemetry về tỷ lệ false-block.
- Incident timeline thống nhất và AI postmortem dựa trên audit/evidence thật.
- Mở rộng command catalogue cho các mã health còn phải
  `investigate_manually`, nhưng chỉ khi có target và post-check tất định.
- Sửa các test fixture Log Intelligence dùng timestamp cũ để toàn bộ nhóm
  test alerting phản ánh đúng freshness guard hiện tại.

### Vitastor

Theo dõi chi tiết tại `docs/VITASTOR_AI_ROADMAP.md`. Ưu tiên khi làm lại:

1. 4.1 — xác minh telemetry sau remediation.
2. 4.2 — tự hủy đề xuất khi tín hiệu tự hết.
3. 4.3 — duyệt action bằng Telegram.
4. 13 — Log Intelligence/RCA dùng Loki.
5. 3 — dự báo dung lượng 80/90/95%.

## Kiểm tra trước khi phát triển tiếp

```bash
git status --short
git pull --rebase origin main
PYTHONPATH=. .venv/bin/pytest -q tests/test_router_client.py \
  tests/test_watcher_incident_flow.py tests/test_log_analysis.py
```

Production không có `npm` tại thời điểm bàn giao, vì vậy
`scripts/deploy/restart_services.sh` dừng ở bước build frontend. Với thay đổi
backend-only, lần deploy gần nhất đã restart Watcher/Worker thủ công. Trước
khi thay đổi frontend, cần cài Node/npm đúng phiên bản hoặc sửa pipeline build
theo artifact; không được xem `git pull` đơn thuần là deploy hoàn tất.

## Các commit mốc

- `5455d1e` — restart đúng OSD cephadm cho BlueStore slow-op.
- `bb8992e` — chỉ báo thành công sau health verification.
- `d5bc768` — chặn finding từ recovery log đã cũ.
- `a48c1ee` — dọn action mồ côi và sửa cảnh báo provider.
- `938b777` — dedupe LogFinding có evidence overlap.
- `cddc03e` — đóng Incident/Action của cluster không hoạt động.
