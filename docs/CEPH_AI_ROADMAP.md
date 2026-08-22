# Ceph AI — Roadmap phát triển

Cập nhật: **2026-08-22**. Ceph là phạm vi ưu tiên; các hạng mục Vitastor tiếp tục
được theo dõi riêng tại `docs/VITASTOR_AI_ROADMAP.md` và chỉ triển khai sau.

## Nguyên tắc

- AI chỉ kết luận từ evidence và audit có thật; không được tự tạo timeline.
- Mọi thay đổi cluster phải qua policy, preview, phê duyệt và post-check tất định.
- Chạy lệnh thành công không đồng nghĩa đã khắc phục; phải xác minh bằng telemetry.
- Rule phía server quyết định identity, policy và mức an toàn; model chỉ phân tích.

## Thứ tự ưu tiên Ceph

- [~] **1. Semantic correlation cho Log Intelligence**
  - Sinh `fault_family` ổn định từ catalogue phía server.
  - Sinh entity ổn định từ host/daemon/evidence đã xác thực.
  - Dedupe theo semantic identity, vẫn giữ overlap evidence làm fallback bảo thủ.
  - [x] Correlation tất định giữa health Incident và LogFinding theo cluster,
    fault-family, entity OSD và cửa sổ thời gian; lưu provenance để audit/UI.
  - [x] Chia evidence đã triage thành batch 10 pattern/lượt AI; circuit breaker
    tổng chỉ chặn cửa sổ cực đoan trên 100 pattern.
  - [x] Lưu structured evidence từ OSD latency/device-health vào Incident và
    đóng băng snapshot đó trong LogFinding khi correlation; không parse văn bản/đoán lại.
  - [ ] Mở rộng thêm các nguồn metric khác (volume, node resource, capacity).
- [ ] **2. Incident timeline thống nhất và AI postmortem**
  - Dòng thời gian: phát hiện → evidence → chẩn đoán → đề xuất → duyệt → thực thi → xác minh.
  - Postmortem chỉ được viết từ timeline/audit đã lưu, gồm root cause, ảnh hưởng,
    hành động, evidence trước/sau và biện pháp phòng ngừa.
- [ ] **3. Dự báo dung lượng**
  - Dự báo mốc 80/90/95% theo cluster, pool và OSD từ tối thiểu 30 ngày dữ liệu.
  - Hiển thị ngày dự kiến đầy, tốc độ tăng, confidence và nhu cầu thêm disk/node.
  - Mô phỏng sức chứa khi mất một OSD hoặc một failure domain.
- [ ] **4. AI Operations Copilot có dẫn nguồn**
  - Hỏi đáp theo thời gian, so sánh trước/sau upgrade và lập kế hoạch thao tác.
  - Mọi câu trả lời vận hành phải kèm evidence, thời điểm thu thập và confidence.
  - Kế hoạch không có quyền thực thi trực tiếp.
- [ ] **5. Dự báo lỗi ổ đĩa**
  - Risk score tất định từ SMART, latency, I/O error, BlueStore slow-op và lịch sử restart.
  - AI giải thích tín hiệu và đề xuất; không tự purge/zap/destroy OSD.
- [ ] **6. Change-risk analyzer**
  - Preflight upgrade/CRUSH/pool/PG, ước lượng rebalance và ảnh hưởng client I/O.
  - Sinh rollback plan và post-check trước khi operator phê duyệt.
- [ ] **7. Feedback loop và đo chất lượng AI**
  - Operator đánh dấu chẩn đoán đúng/sai, false positive và hiệu quả action.
  - Báo cáo precision/false-positive theo health code, fault family và AI provider.
  - Trước mắt dùng phản hồi để chỉnh rule/prompt; chưa tự fine-tune từ dữ liệu production.

## CPU/RAM forecast từ Loki

- Watcher ghi mỗi mẫu node health vào Loki với `job="ceph-ai-node-metrics"`,
  labels `cluster`, `host`, `metric_type="node_resource"`.
- Dự báo không đọc cache/DB cục bộ: luôn query lại lịch sử Loki, mặc định 30
  ngày, tối thiểu 24 mẫu và dự báo cửa sổ 168 giờ.
- Chỉ ghi cảnh báo xu hướng khi mốc 90% nằm trong cửa sổ và hệ số phù hợp
  (`R²`) đạt ngưỡng. Dự báo chỉ là evidence/cảnh báo, không tự reboot node.
- Bật bằng `NODE_RESOURCE_FORECAST_ENABLED=true`; dùng chung URL/tenant Loki
  của Log Intelligence.

## Tiêu chí hoàn thành mục 1

1. Hai cửa sổ có evidence ID khác nhau nhưng cùng fault family và cùng entity không tạo
   hai cảnh báo mở.
2. Hai lỗi chỉ chung log tổng quát nhưng khác family/entity không bị nhập nhầm.
3. `fault_family` và entity do server sinh, không tin trực tiếp output của model.
4. Bản ghi cũ không có semantic identity vẫn hoạt động bằng cơ chế overlap hiện tại.
5. Có migration, kiểm thử dedupe lúc ghi mới và reconciliation dữ liệu đang mở.
