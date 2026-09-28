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

- [~] **1. Context ceiling cho incident diagnosis** — một số đường gọi đã có
  giới hạn evidence/tool-result, nhưng cần đối chiếu lại toàn bộ runtime vì
  chưa có một context packer dùng chung và giới hạn cấu hình thực tế chưa
  đồng nhất giữa Incident và Chat. Không đánh dấu hoàn tất chỉ dựa trên
  setting/documentation. Các giới hạn hiện có gồm
  `ai_incident_max_context_chars` (mặc định 12.000),
  `ai_incident_max_context_tokens` (mặc định 6.000), giới hạn tool-result và
  giới hạn lịch sử Chat; cần hợp nhất chúng thành một packer có test đầu-cuối.
- [x] **2. Context packing cho Chat** — `pack_chat_history` giới hạn theo ký
  tự/token thay vì chỉ giới hạn số message; giữ lượt gần nhất, cắt phần cũ
  nhất với marker rõ ràng. Đã dùng chung cho Router/Codex/Claude và có test.
- [~] **3. Giảm số provider round-trip** — đã cache kết quả tool read-only
  trong cùng lượt và giữ giới hạn model output/tool iterations. Còn cần
  dừng sớm theo evidence sufficiency và đo số round-trip p95 trước/sau.
- [ ] **4. Routing tiết kiệm có kiểm soát** — phân loại tác vụ, đề xuất model rẻ
  hơn, canary và so sánh chất lượng trước khi đổi mặc định.
- [~] **5. Observability và budget guard** — ledger content-free, giá token,
  daily/monthly budget, hard limit, aggregate summary API, projection và panel
  Dashboard AI Cost đã có. Còn phải áp migration, cấu hình bảng giá theo
  provider/model, và xử lý reservation nguyên tử giữa nhiều process để hard
  limit không có race condition.
- [~] **5.1. Instrument toàn bộ provider path** — các luồng Chat, Incident,
  Log, Backup, Volume, Vitastor, Postmortem, Upgrade và Code Repair đã ghi
  usage; cần tiếp tục rà soát các subprocess/adapter AI còn lại và bổ sung
  contract test để không phát sinh đường gọi ngoài ledger.
- [x] **6. Delegated task ceilings** — giới hạn sub-agent, provider calls,
  timeout, cancellation và admission đã có.

## Tiêu chí nghiệm thu

- Mỗi mục có test regression và số liệu input/output trước-sau.
- Không có request vượt hard ceiling đã cấu hình.
- Không mất evidence bắt buộc hoặc tạo kết luận từ dữ liệu đã bị cắt mà không
  ghi rõ trạng thái giới hạn.
- API admin `/api/settings/ai-cost` và panel Settings → Hệ thống → AI Cost đã
  hiển thị usage, lỗi, model, chi phí ước tính và projection content-free trước
  khi bật routing mới.
