# Kế hoạch tối ưu chi phí AI

Phạm vi này tách khỏi roadmap tính năng Ceph. Mục tiêu là giảm token/provider
usage mà không làm mất evidence quan trọng hoặc tự ý đổi provider production.

## Nguyên tắc

- Đo usage thực tế trước khi tối ưu; không suy ra chi phí chỉ từ số request.
- Giới hạn cứng input context, output và số vòng tool ở từng đường gọi AI.
- Dữ liệu hiện tại, evidence chính và kết luận an toàn luôn được ưu tiên hơn
  lịch sử dư thừa.
- Đổi model/provider chỉ là đề xuất hoặc canary có rollback; không tự đổi.
- Budget guard phải fail-closed ở chế độ hard limit và không lưu prompt/response.

## Thứ tự triển khai

- [x] **1. Context ceiling cho incident diagnosis** — thêm
  `ai_incident_max_context_chars` (mặc định 12.000) và
  `ai_incident_max_context_tokens` (mặc định 6.000, ước lượng bảo thủ 2
  ký tự/token), giữ mã lỗi/thời điểm/node/snapshot/log chính rồi mới cắt
  historical evidence. Đã deploy 2026-09-07.
- [x] **2. Context packing cho Chat** — giới hạn theo ký tự/token thay vì chỉ
  giới hạn số message; giữ lượt hiện tại và evidence gần nhất, loại lịch sử dư.
  Đã thêm hard ceiling 12.000 ký tự/6.000 token ước lượng, marker khi cắt và
  regression test cho transcript dài.
- [ ] **3. Giảm số provider round-trip** — cache read-only tool trong cùng lượt,
  dừng sớm khi đủ evidence, giới hạn model output theo từng feature.
- [ ] **4. Routing tiết kiệm có kiểm soát** — phân loại tác vụ, đề xuất model rẻ
  hơn, canary và so sánh chất lượng trước khi đổi mặc định.
- [x] **5. Observability và budget guard** — telemetry content-free, giá token,
  daily/monthly budget và hard limit đã có.
- [x] **6. Delegated task ceilings** — giới hạn sub-agent, provider calls,
  timeout, cancellation và admission đã có.

## Tiêu chí nghiệm thu

- Mỗi mục có test regression và số liệu input/output trước-sau.
- Không có request vượt hard ceiling đã cấu hình.
- Không mất evidence bắt buộc hoặc tạo kết luận từ dữ liệu đã bị cắt mà không
  ghi rõ trạng thái giới hạn.
- Dashboard AI Cost hiển thị được usage, lỗi, model và projection trước khi
  bật routing mới.
