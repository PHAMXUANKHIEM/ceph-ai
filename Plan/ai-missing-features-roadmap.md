# Roadmap các tính năng AI chưa triển khai

## 1. Mục tiêu và phạm vi

Roadmap này chỉ theo dõi các năng lực AI **chưa có** hoặc chưa đạt mức có thể
nghiệm thu trong `ceph-ai`. Các tính năng đã hoạt động như Ceph incident
diagnosis, Chat with AI, backup analysis, volume performance sweep analysis và
Vitastor diagnosis không được liệt kê lại.

Phạm vi bao gồm Ceph Block Storage, Object Storage/RGW, Vitastor và lớp an toàn
dùng chung. Thứ tự triển khai ưu tiên khả năng chỉ đọc, evidence và dự báo trước;
mọi chức năng thay đổi cụm chỉ được mở sau khi hoàn thành policy, preview,
phê duyệt, post-check và audit.

## 2. Quy ước trạng thái

- `[ ]` Chưa triển khai.
- `[~]` Đang triển khai hoặc mới hoàn thành một phần.
- `[x]` Hoàn thành, có kiểm thử và bằng chứng nghiệm thu.
- Mỗi mục hoàn thành phải cập nhật ngày, commit và kết quả kiểm thử trong nhật ký.
- Không đánh dấu hoàn thành chỉ vì đã có giao diện, prompt hoặc dữ liệu giả.

## 3. Nguyên tắc bắt buộc

### 3.1 Evidence và giới hạn AI

- Mọi kết luận phải dẫn về evidence thật, có cluster, thời điểm thu thập và độ mới.
- Phân biệt rõ dữ liệu đo được, suy luận của AI và đề xuất của hệ thống.
- Không gửi SSH key, access key, secret key, token, mật khẩu hoặc cấu hình nhạy cảm
  tới model.
- Khi evidence thiếu hoặc quá cũ, kết quả phải là `INSUFFICIENT_EVIDENCE`, không
  được đoán target, command, version hoặc nguyên nhân.
- Output AI dùng schema đóng và được server kiểm tra lại; không parse lệnh từ
  nội dung văn bản tự do.

### 3.2 Tương thích phiên bản Ceph

- Trước khi tạo hướng dẫn hoặc hành động, phải xác định phiên bản thực tế của cụm
  và phương thức triển khai: cephadm, package, ceph-deploy hoặc loại khác.
- Xây capability matrix theo major release, command, option, module và backend.
- Với mỗi capability, lưu nguồn tài liệu Ceph chính thức, release áp dụng và ngày
  kiểm chứng. Không dùng blog hoặc câu trả lời cộng đồng làm nguồn quyết định.
- Nếu phiên bản không hỗ trợ, UI/API phải trả `UNSUPPORTED_VERSION`, nêu phiên bản
  tối thiểu và không hiển thị nút thực thi.
- Việc tra cứu web là bước cập nhật/duyệt capability matrix có cache và audit;
  không để model tự lấy nội dung web chưa kiểm chứng rồi chạy thẳng trên cụm.

### 3.3 An toàn thực thi

- Action phải có `action_id` đóng, typed parameters, RBAC và target allowlist.
- Luôn có command preview, mức rủi ro, phạm vi ảnh hưởng và điều kiện tiên quyết.
- SAFE chỉ dành cho thao tác có thể chứng minh ít rủi ro và có post-check rõ ràng.
- RISKY/DESTRUCTIVE phải phê duyệt riêng; không được tự chạy từ Chat hoặc nội dung AI.
- Executor phải chống chạy lặp, có timeout, distributed lock, audit trước/sau và
  lưu stdout/stderr đã redaction.
- Không tuyên bố thành công nếu không có execution evidence và post-check đạt.

## 4. Lộ trình triển khai

### Pha 0 — Nền tảng version-aware và safety gate — P0

- [ ] **0.1 Cluster capability inventory**
  - Thu thập version theo daemon và cảnh báo mixed-version.
  - Xác định deployment mode và command runner phù hợp.
  - Chuẩn hóa capability response: `SUPPORTED`, `UNSUPPORTED_VERSION`,
    `UNAVAILABLE`, `UNKNOWN`.
- [ ] **0.2 Capability matrix có nguồn kiểm chứng**
  - Khai báo major/minor version, command, flags, module, backend và tài liệu Ceph
    chính thức tương ứng.
  - Có cache, thời hạn cập nhật, người duyệt và lịch sử thay đổi.
  - Fail closed khi version hoặc capability chưa xác định.
- [ ] **0.3 AI preflight validator**
  - Kiểm tra action, tham số, target, version, cluster health và dependency trước
    khi cho phép tạo proposal.
  - Chặn command/flag không hỗ trợ và trả thông báo có thể hiểu trên Dashboard.
- [ ] **0.4 Safety policy hardening**
  - Bổ sung mức `READ_ONLY/SAFE/RISKY/DESTRUCTIVE`.
  - Bảo đảm thao tác mất dữ liệu như xóa pool, purge hoặc ghi đè production không
    thể nằm trong luồng auto-run.
  - Chuẩn hóa approval, expiry, stale-evidence check và idempotency key.
- [ ] **0.5 Kiểm thử**
  - Matrix test tối thiểu cho các phiên bản Ceph còn hỗ trợ trong sản phẩm.
  - Test mixed-version, version không biết, flag bị loại bỏ, prompt injection,
    hallucinated action và stale evidence.

**Hoàn thành khi:** không proposal nào có thể đi tới executor nếu chưa chứng minh
được action tương thích với phiên bản và policy của đúng cụm.

### Pha 1 — AI Capacity Forecasting — P0

- [ ] **1.1 Time-series pipeline**
  - Thu thập lịch sử raw/used/available, pool usage, thin provisioning, growth của
    volume/snapshot, replication/EC overhead và failure-domain reserve.
  - Chuẩn hóa timezone, khoảng trống dữ liệu, reset counter và retention.
- [ ] **1.2 Forecast engine**
  - Dự báo ngày chạm 80%, 90%, 95% và ngày đầy với confidence interval.
  - Hỗ trợ xu hướng tuyến tính, seasonality và spike; không dự báo khi không đủ mẫu.
  - Backtest theo rolling window và lưu sai số dự báo.
- [ ] **1.3 Risk explanation**
  - Giải thích nguồn tăng trưởng chính theo pool, volume, snapshot hoặc workload.
  - Tách physical usage khỏi provisioned capacity và chỉ rõ ảnh hưởng replica/EC.
- [ ] **1.4 Alert và Dashboard**
  - Cảnh báo theo time-to-threshold, chống spam và có lifecycle OPEN/RESOLVED.
  - Biểu đồ actual/forecast/confidence cùng evidence timestamp.
- [ ] **1.5 Kiểm thử**
  - Dữ liệu tăng đều, seasonality, spike, thiếu mẫu, counter reset và pool mới.
  - Đặt ngưỡng sai số chấp nhận được trước khi bật cảnh báo production.

**Hoàn thành khi:** Dashboard dự báo được mốc dung lượng có confidence, backtest
và nguyên nhân tăng trưởng; không biến cảnh báo ngưỡng hiện tại thành “AI forecast”.

### Pha 2 — AI Block Storage Inventory Insight — P1

- [ ] **2.1 Stale và unattached volume**
  - Kết hợp metadata, attachment, I/O, tuổi volume, owner/project và backup gần nhất.
  - Đưa ra lý do, confidence và mức dung lượng có thể thu hồi.
- [ ] **2.2 Snapshot/clone intelligence**
  - Phát hiện snapshot quá hạn, snapshot không có policy, clone chain sâu và quan hệ
    parent-child cản trở flatten/xóa.
  - Vẽ dependency trước khi đề xuất thay đổi.
- [ ] **2.3 Backup và protection gap**
  - Phát hiện backup trễ, retention bất hợp lý, volume quan trọng chưa được bảo vệ
    và restore drill quá hạn.
- [ ] **2.4 Recommendation only**
  - Đề xuất retain, snapshot, backup, flatten, move-to-trash hoặc resize nhưng chưa
    thực thi trong pha này.
  - Mỗi đề xuất có evidence, expected saving, tác động và TTL.
- [ ] **2.5 Kiểm thử**
  - Volume đang attach, metadata giả mạo, clone dependency, snapshot được bảo vệ,
    stale cache và tenant isolation.

**Hoàn thành khi:** AI tìm được tài nguyên lãng phí mà không đánh dấu nhầm volume
đang hoạt động và không vượt qua ranh giới tenant/RBAC.

### Pha 3 — AI Performance Diagnosis và Recommendation Simulation — P1

- [ ] **3.1 Correlation engine**
  - Tương quan volume → pool → PG → OSD → disk → host/network trong cùng cửa sổ thời gian.
  - Phân biệt consumer bottleneck, contention, recovery/backfill, capacity pressure,
    slow disk và network latency.
- [ ] **3.2 Hot resource detection**
  - Phát hiện hot volume, hot pool, hot PG/OSD và phân bố lệch theo baseline động.
  - Có confidence, top contributing signals và so sánh trước/sau sự kiện.
- [ ] **3.3 Recommendation simulation**
  - Mô phỏng resize, QoS, flatten, retention, placement, replica/EC và PG change.
  - Tính expected benefit, rebalance volume, thời gian ước lượng và failure-domain risk.
- [ ] **3.4 Read-only report**
  - Sinh báo cáo nguyên nhân, bằng chứng, bước kiểm tra và phương án xếp hạng.
  - Không tự đổi PG/CRUSH/replica/EC trong pha này.
- [ ] **3.5 Kiểm thử**
  - Workload tổng hợp có bottleneck đã biết, dữ liệu metric lệch thời gian, mất node,
    recovery đang chạy và false-positive benchmark.

**Hoàn thành khi:** kết luận chỉ ra đúng tầng nghẽn với evidence và mọi đề xuất thay
đổi topology đều có mô phỏng tác động.

### Pha 4 — AI Object Storage/RGW Diagnosis — P1

- [ ] **4.1 RGW evidence collector**
  - Thu thập daemon, endpoint, frontend, realm/zonegroup/zone, sync status, metadata
    và capacity dependency bằng lệnh read-only tương thích phiên bản.
  - Cache theo cụm và trả dữ liệu cũ có nhãn rõ khi node tạm thời không truy cập được.
- [ ] **4.2 Bucket access diagnosis**
  - Chẩn đoán lỗi list/create/delete/access bucket từ RGW, auth, endpoint, DNS/TLS,
    quota, policy và backend pool.
  - Phân biệt CephX admin lỗi với S3 credential hoặc bucket policy lỗi.
- [ ] **4.3 Multi-site diagnosis**
  - Phân tích replication lag, shard error, master state, period/epoch mismatch và
    conflict; chỉ đưa hướng dẫn read-only ở pha đầu.
- [ ] **4.4 Security insight**
  - Phát hiện bucket public ngoài ý muốn, policy/ACL quá rộng, key lâu không dùng,
    key không xoay vòng, quota bất thường và logging/audit gap.
  - Không gửi access/secret key vào prompt hoặc log.
- [ ] **4.5 Audit-log intelligence**
  - Tổng hợp GET/PUT/DELETE bất thường theo requester, IP, User-Agent, thời gian,
    status code, kích thước và latency.
  - Có baseline, lọc false positive và retention policy.
- [ ] **4.6 Kiểm thử**
  - RGW không có keyring, endpoint chết, permission denied, multisite thiếu cấu hình,
    unsupported release và malicious bucket metadata.

**Hoàn thành khi:** AI giải thích được lỗi RGW/bucket bằng evidence thật, có kiểm tra
phiên bản và hoàn toàn read-only.

### Pha 5 — AI Pool/PG, CRUSH và Scrub Intelligence — P2

- [ ] **5.1 Pool/PG advisor**
  - Đề xuất PG, autoscaler mode, replica hoặc EC profile dựa trên workload, số OSD,
    device class và failure domain.
  - So sánh trạng thái hiện tại với phương án đề xuất.
- [ ] **5.2 CRUSH placement advisor**
  - Phân tích skew, host/rack concentration, rule mismatch và failure-domain risk.
  - Mô phỏng data movement và khả năng chịu lỗi trước thay đổi.
- [ ] **5.3 Smart scrub scheduler**
  - Chọn cửa sổ scrub theo tải, tuổi lần scrub, risk signal và maintenance window.
  - Không trì hoãn quá giới hạn an toàn đã cấu hình.
- [ ] **5.4 Inconsistent-object analysis**
  - Thu thập PG/object evidence và phân loại inconsistency/corruption.
  - `repair`, object `fix` hoặc thao tác có nguy cơ mất bản sao luôn DESTRUCTIVE/RISKY.
- [ ] **5.5 Kiểm thử**
  - Nearfull, degraded/recovery, undersized pool, mixed device class, CRUSH hierarchy
    lỗi và simulation sai/thiếu dữ liệu.

**Hoàn thành khi:** advisor chứng minh được tác động placement/capacity và không tự
thay đổi PG, CRUSH hoặc dữ liệu.

### Pha 6 — Incident Timeline và AI Postmortem — P2

- [ ] **6.1 Unified event timeline**
  - Hợp nhất health transition, metric anomaly, alert, proposal, approval, command,
    post-check và operator event theo cluster/time.
- [ ] **6.2 Correlation và root-cause chain**
  - Nhóm các alert liên quan, phân biệt nguyên nhân với hệ quả và lưu confidence.
- [ ] **6.3 AI postmortem**
  - Sinh impact, timeline, root cause, contributing factors, response assessment,
    recovery evidence và follow-up action.
  - Mọi câu khẳng định phải liên kết tới event/evidence nguồn.
- [ ] **6.4 Export và review workflow**
  - Cho phép operator chỉnh sửa/phê duyệt trước khi xuất Markdown/PDF hoặc gửi đi.
- [ ] **6.5 Kiểm thử**
  - Event tới trễ, clock skew, duplicate incident, thiếu log, nhiều sự cố đồng thời
    và prompt injection trong log/message.

**Hoàn thành khi:** postmortem có thể kiểm chứng từng kết luận và không bịa timeline.

### Pha 7 — AI Capacity Planner — P2

- [ ] **7.1 Workload model**
  - Nhập số VM/volume, dung lượng, IOPS, throughput, latency target, growth, RPO/RTO
    và failure domain.
- [ ] **7.2 Topology planner**
  - Đề xuất số node/OSD, media class, replica/EC, headroom và network requirement.
  - Hỗ trợ Ceph và Vitastor bằng model tách biệt.
- [ ] **7.3 Scenario comparison**
  - So sánh cost/capacity/performance/durability và mô phỏng mất host/rack.
- [ ] **7.4 Explainability và export**
  - Hiển thị công thức, giả định, confidence và dữ liệu đầu vào; không chỉ trả văn bản AI.
- [ ] **7.5 Kiểm thử**
  - Golden scenarios, boundary values, impossible SLA, thiếu failure domain và mixed disk.

**Hoàn thành khi:** cùng một input cho kết quả tính toán tái lập được, AI chỉ giải
thích và xếp hạng trên dữ liệu từ deterministic planner.

### Pha 8 — Closed-loop Remediation và Rollback dùng chung — P3

- [ ] **8.1 Remediation state machine**
  - `PROPOSED → APPROVED → EXECUTING → VERIFYING → SUCCEEDED/FAILED/ROLLED_BACK`.
  - Hỗ trợ expiry, cancellation, distributed lock và recovery sau worker restart.
- [ ] **8.2 Universal post-check contract**
  - Mỗi action khai báo success criteria, thời gian chờ, health guard và evidence
    trước/sau.
- [ ] **8.3 Rollback planner**
  - Chỉ đánh dấu rollback-supported khi có inverse action đã kiểm thử.
  - Không giả rollback cho delete/purge/data repair hoặc thay đổi không đảo ngược.
- [ ] **8.4 Controlled RGW/Block/Vitastor actions**
  - Mở từng action sau shadow mode và canary; RISKY/DESTRUCTIVE luôn cần phê duyệt.
- [ ] **8.5 Kiểm thử failure injection**
  - Timeout, SSH disconnect, partial success, stale approval, concurrent action,
    failed post-check, failed rollback và worker crash.

**Hoàn thành khi:** mọi thay đổi do AI đề xuất đi qua cùng RBAC/policy/executor như
thao tác thủ công, có audit và không tuyên bố thành công trước post-check.

### Pha 9 — Safe Autopilot nhiều cấp — P3

- [ ] **9.1 Ba chế độ vận hành**
  - `ADVISORY`: chỉ chẩn đoán/đề xuất.
  - `APPROVAL_REQUIRED`: operator duyệt từng action.
  - `LIMITED_AUTOPILOT`: tự chạy tập SAFE đã duyệt trước trong phạm vi/time window.
- [ ] **9.2 Guardrails**
  - Maintenance window, action budget, blast-radius limit, health floor, cooldown,
    kill switch và per-cluster allowlist.
- [ ] **9.3 Shadow mode và promotion**
  - Đo precision, false-positive, expected/actual outcome trước khi action được nâng cấp.
- [ ] **9.4 Operator controls**
  - Hiển thị rõ chế độ hiện tại, action sắp chạy, lịch sử, lý do dừng và cách vô hiệu hóa.
- [ ] **9.5 Kiểm thử**
  - Guardrail bypass, policy reload, split-brain worker, kill switch, budget exhaustion
    và model/provider unavailable.

**Hoàn thành khi:** Limited Autopilot chỉ chạy action SAFE đã được operator cấp quyền
trước, tự dừng khi health xấu đi và có thể truy vết toàn bộ quyết định.

## 5. Thứ tự phát hành đề xuất

1. **AI-F0:** Pha 0 — version-aware capability và safety gate.
2. **AI-F1:** Pha 1 — capacity forecasting read-only.
3. **AI-F2:** Pha 2 — Block Storage inventory insight.
4. **AI-F3:** Pha 3 — performance correlation và simulation.
5. **AI-F4:** Pha 4 — Object Storage/RGW diagnosis và security insight.
6. **AI-F5:** Pha 5 — Pool/PG/CRUSH/scrub intelligence.
7. **AI-F6:** Pha 6 và 7 — postmortem, capacity planner.
8. **AI-F7:** Pha 8 — closed-loop và rollback dùng chung.
9. **AI-F8:** Pha 9 — Safe Autopilot sau thời gian shadow/canary đạt yêu cầu.

Không triển khai Pha 8 hoặc Pha 9 trước Pha 0. Các pha read-only có thể phát triển
song song sau khi schema evidence và capability contract đã ổn định.

## 6. Definition of Done dùng chung

Một tính năng chỉ được coi là hoàn thành khi đáp ứng đủ:

1. Có model/schema dữ liệu, migration nếu cần và retention rõ ràng.
2. Có collector dùng dữ liệu thật, cache policy và stale-data indicator.
3. Có version/capability check, RBAC và tenant isolation.
4. Prompt/output schema đóng, redaction và giới hạn kích thước evidence.
5. Có UI/API thể hiện confidence, evidence, timestamp và trạng thái lỗi.
6. Có audit cho proposal, approval, execution và post-check nếu liên quan thay đổi.
7. Có unit test, integration test, security test và test lỗi provider/model.
8. Có kiểm thử trên các phiên bản Ceph/Vitastor được công bố hỗ trợ.
9. Có tài liệu vận hành, rollback/disable procedure và metric quan sát hệ thống AI.
10. Roadmap và nhật ký được cập nhật bằng commit đã phát hành.

## 7. Chỉ số đánh giá

- Tỷ lệ chẩn đoán có evidence hợp lệ và không quá hạn.
- Precision/recall của anomaly và recommendation trên bộ dữ liệu có nhãn.
- Forecast error theo horizon 7/30/90 ngày.
- False-positive rate và số đề xuất bị operator từ chối.
- Tỷ lệ post-check thành công, rollback và action bị guardrail chặn.
- Mean time to diagnose và mean time to recover trước/sau khi bật tính năng.
- Số lần AI sinh target/action/version không hợp lệ phải bằng 0 sau validation.
- Số credential/secret xuất hiện trong prompt, response hoặc audit phải bằng 0.

## 8. Nhật ký triển khai

| Ngày | Hạng mục | Trạng thái | Thay đổi | Kiểm thử | Commit |
|---|---|---|---|---|---|
| 2026-08-17 | Khởi tạo roadmap | Hoàn thành | Tổng hợp riêng các năng lực AI chưa triển khai và thứ tự phát hành | Review tài liệu | Chờ commit |
