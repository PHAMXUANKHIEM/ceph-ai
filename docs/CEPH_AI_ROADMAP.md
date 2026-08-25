# Ceph AI — Roadmap phát triển

Cập nhật: **2026-08-22**. Ceph là phạm vi ưu tiên; các hạng mục Vitastor tiếp tục
được theo dõi riêng tại `docs/VITASTOR_AI_ROADMAP.md` và chỉ triển khai sau.

## Nguyên tắc

- AI chỉ kết luận từ evidence và audit có thật; không được tự tạo timeline.
- Mọi thay đổi cluster phải qua policy, preview, phê duyệt và post-check tất định.
- Chạy lệnh thành công không đồng nghĩa đã khắc phục; phải xác minh bằng telemetry.
- Rule phía server quyết định identity, policy và mức an toàn; model chỉ phân tích.

Lộ trình closed-loop đầy đủ từ học có giám sát đến Autopilot L5, bao gồm case
memory, trust engine, shadow mode, promotion/demotion và rollout production,
được mô tả tại
[`ceph-autonomous-operations-roadmap.md`](ceph-autonomous-operations-roadmap.md).

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
  - [~] Mở rộng thêm các nguồn metric khác:
    - [x] Capacity cluster/pool/OSD từ `ceph df detail` và `ceph osd df`, đóng
      băng trong Incident rồi truyền nguyên snapshot sang LogFinding tương quan.
    - [x] Volume RBD: snapshot pool/image, IOPS, read/write latency, baseline,
      observed peak và streak; correlation bắt buộc khớp `volume:pool/image` khi có entity.
    - [x] Node resource: snapshot CPU/RAM, streak và threshold; correlation
      bắt buộc khớp `host:` khi LogFinding và Incident đều có định danh node.
- [~] **2. Incident timeline thống nhất và AI postmortem**
  - Dòng thời gian: phát hiện → evidence → chẩn đoán → đề xuất → duyệt → thực thi → xác minh.
  - Postmortem chỉ được viết từ timeline/audit đã lưu, gồm root cause, ảnh hưởng,
    hành động, evidence trước/sau và biện pháp phòng ngừa.
  - [x] Timeline hợp nhất các mốc có timestamp thật từ Incident, Action và Audit;
    diagnosis thiếu timestamp chỉ là context, không giả làm event.
  - [x] AI postmortem dùng payload đóng, bắt buộc citation event ID hợp lệ và
    lưu prompt version; khóa nhạy cảm trong structured evidence được che trước khi gửi.
  - [x] Event ledger append-only ghi timestamp thật cho AI diagnosis và tự
    mirror toàn bộ audit lifecycle trong cùng transaction; dữ liệu cũ tiếp tục dùng fallback.
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
  - Quy trình triển khai vòng học lỗi daemon từ Loki được đặc tả tại
    [`loki-daemon-log-learning.md`](loki-daemon-log-learning.md).
- [ ] **8. Autonomous Operations theo playbook maturity**
  - Lưu Remediation Case và outcome đã verify để tái sử dụng cho lỗi quen thuộc.
  - Chạy Shadow Autopilot trước khi mở quyền tự thực thi trên lab/production.
  - Nâng/hạ autonomy riêng từng playbook dựa trên trust và hard safety gate.
  - SAFE đủ maturity được tự chạy; RISKY/DESTRUCTIVE giữ trần quyền rõ ràng.
  - Có rate limit, cooldown, distributed lock, rollback, circuit breaker và kill switch.

## CPU/RAM forecast từ Loki

- Alloy ghi mẫu node health vào Loki với `job="ceph-ai-node-metrics"`, labels
  `cluster`, `host`, `metric_type="node_resource"`. Watcher đọc CPU/RAM hiện
  tại và lịch sử từ stream này, không SSH vào node để lấy `/proc`.
- Dự báo không đọc cache/DB cục bộ: luôn query lại lịch sử Loki, mặc định 30
  ngày, tối thiểu 24 mẫu và dự báo cửa sổ 168 giờ.
- Chỉ ghi cảnh báo xu hướng khi mốc 90% nằm trong cửa sổ và hệ số phù hợp
  (`R²`) đạt ngưỡng. Dự báo chỉ là evidence/cảnh báo, không tự reboot node.
- Bật bằng `NODE_RESOURCE_FORECAST_ENABLED=true`; dùng chung URL/tenant Loki
  của Log Intelligence.
- Vòng tự học lưu mỗi dự báo candidate vào `node_resource_forecast_runs`.
  Sau `NODE_RESOURCE_LEARNING_EVALUATION_HOURS`, mẫu thật mới nhất từ Loki
  được dùng làm outcome và tính absolute error.
- `node_resource_model_states` giữ running MAE theo cluster/host/metric và
  cửa sổ 24/72/168/720 giờ. Sau tối thiểu 3 outcome, cửa sổ có MAE thấp nhất
  tự được chọn; trước đó dùng cửa sổ dài nhất có đủ dữ liệu.
- Việc học chỉ đổi lựa chọn mô hình forecast. Nó không được thay policy,
  allowlist, ngưỡng hành động hoặc tự thực thi remediation.

## Block Storage/RBD — hiện trạng và lộ trình AI

### Đã có

- Watcher thu thập lịch sử theo từng `pool/image`: IOPS, read/write latency,
  observed peak, baseline và streak bão hòa; dữ liệu được lưu trong
  `volume_metrics` để Dashboard, Incident và correlation cùng dùng evidence.
- Rule hiện tại chỉ kết luận volume bão hòa khi cửa sổ đã warm-up, IOPS gần
  đỉnh quan sát, latency tăng rõ so với median và tình trạng kéo dài nhiều lần
  quét. Khi đủ điều kiện, hệ thống tạo Incident `VOLUME_SATURATED:*` thay vì
  tự thay đổi volume.
- Load sweep chủ động chạy `fio` trên scratch RBD image, không chạy trên dữ
  liệu production. Kết quả lưu trong `volume_perf_sweeps`, gồm toàn bộ đường
  cong iodepth/IOPS/latency, điểm knee, QoS notes và bottleneck evidence.
- Nút **Phân tích bằng AI** đọc kết quả sweep đã hoàn tất để giải thích trần
  IOPS khả dụng, confidence và caveat bằng tiếng Việt. Đây là phân tích
  read-only; hiện chưa tự động gọi AI sau mỗi sweep.
- Luồng quản trị đã có preflight, policy, phê duyệt và post-check cho tạo,
  resize, rename, trash/restore/purge RBD; attach/detach và snapshot Cinder;
  backup/restore RBD. Hành động RISKY/DESTRUCTIVE không được AI tự chạy.
- Xác minh production ngày 2026-08-22: lịch sử `volume_metrics` đã có dữ liệu
  thực và đã có các lần performance sweep. Backup RBD vẫn chưa hoạt động vì
  chưa cấu hình storage target và tracked image.

### Giới hạn hiện tại

- Phát hiện bão hòa vẫn là rule cố định: rolling window 12 mẫu, IOPS ít nhất
  90% peak, latency ít nhất 2 lần median và 3 poll liên tiếp. Đây chưa phải
  mô hình tự học theo đặc tính riêng của từng volume.
- Rolling state ngắn hạn nằm trong tiến trình Watcher nên phải warm-up lại sau
  restart; lịch sử dài hạn trong PostgreSQL không mất.
- IOPS/latency hiện lấy qua lệnh `rbd perf image iostat` trên đường quản trị
  Ceph/SSH, chưa lấy từ Loki. AI chỉ phân tích dữ liệu đã thu thập; chưa tự
  chỉnh QoS, tự resize, tự migrate hoặc tự restore.

### Thứ tự triển khai tiếp theo

- [x] **1. Baseline tự học theo volume**
  - Học IOPS và read/write latency bình thường theo `cluster/pool/image`, giờ
    trong ngày và ngày trong tuần từ lịch sử `volume_metrics`.
  - Chạy nhiều cửa sổ candidate, lưu forecast/outcome, tính MAE và tự chọn cửa
    sổ tốt nhất giống cơ chế CPU/RAM; restart Watcher không làm mất trạng thái học.
  - Giữ hard safety gate: học chỉ thay mô hình/baseline, không thay policy hoặc
    tự cấp quyền thực thi.
  - Triển khai 2026-08-25: seasonal median ưu tiên hour-of-week/hour-of-day,
    outcome 1 giờ, MAE/MAPE bền vững và tự chọn cửa sổ 24/72/168/720 giờ;
    trạng thái hiển thị read-only tại trang AI Learning.
- [ ] **2. Dự báo và cảnh báo sớm**
  - Dự báo IOPS/latency cho 1h, 6h và 24h; cảnh báo khi có khả năng chạm knee
    hoặc latency SLO trước khi workload thực sự suy giảm.
  - Mỗi cảnh báo phải kèm timestamp, số mẫu, training window, confidence và
    forecast/model version; dữ liệu stale hoặc thiếu phải fail closed.
- [ ] **3. Correlation nguyên nhân**
  - Ghép anomaly volume với log Ceph từ Loki, pool/PG/OSD latency, CPU/RAM node,
    network và QoS để phân biệt bottleneck tại image, pool, OSD hay host.
  - Không kết luận nguyên nhân khi chỉ có tương quan thời gian; phải chỉ rõ
    evidence ủng hộ và evidence còn thiếu.
- [ ] **4. Khuyến nghị có kiểm soát**
  - Gợi ý QoS, resize, đổi pool/tier, lịch backup hoặc chạy lại load sweep dựa
    trên evidence và lịch sử outcome.
  - Chỉ tạo proposal có preview/preflight. Resize, migration, restore, purge và
    mọi thao tác có nguy cơ ảnh hưởng dữ liệu vẫn cần operator phê duyệt.
- [ ] **5. Chuẩn hóa nguồn telemetry**
  - Đưa IOPS/latency RBD vào observability pipeline có lịch sử tập trung;
    ưu tiên metric backend phù hợp, hoặc structured Loki stream nếu hạ tầng chỉ
    dùng Loki. Sau khi đối chiếu đủ coverage mới bỏ đường poll SSH hiện tại.
- [ ] **6. Bảo vệ dữ liệu**
  - Cấu hình storage target, tracked images, retention và RestoreDrill; chỉ coi
    backup sẵn sàng khi có bản full/incremental verified và drill thành công.

## Tiêu chí hoàn thành mục 1

1. Hai cửa sổ có evidence ID khác nhau nhưng cùng fault family và cùng entity không tạo
   hai cảnh báo mở.
2. Hai lỗi chỉ chung log tổng quát nhưng khác family/entity không bị nhập nhầm.
3. `fault_family` và entity do server sinh, không tin trực tiếp output của model.
4. Bản ghi cũ không có semantic identity vẫn hoạt động bằng cơ chế overlap hiện tại.
5. Có migration, kiểm thử dedupe lúc ghi mới và reconciliation dữ liệu đang mở.
