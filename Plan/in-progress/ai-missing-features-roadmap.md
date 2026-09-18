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

- [x] **0.1 Cluster capability inventory**
  - Thu thập version theo daemon và cảnh báo mixed-version.
  - Xác định deployment mode và command runner phù hợp.
  - Chuẩn hóa capability response: `SUPPORTED`, `UNSUPPORTED_VERSION`,
    `UNAVAILABLE`, `UNKNOWN`.
- [~] **0.2 Capability matrix có nguồn kiểm chứng** — hạ tầng + trang admin + báo cáo
  độ phủ đã xong; còn lại DUY NHẤT việc operator nhập entry đã tự kiểm chứng
  (7 action_id, xem `/capability-matrix`).
  - Khai báo major/minor version, command, flags, module, backend và tài liệu Ceph
    chính thức tương ứng.
  - Có cache, thời hạn cập nhật, người duyệt và lịch sử thay đổi.
  - Fail closed khi version hoặc capability chưa xác định.
- [~] **0.3 AI preflight validator**
  - Kiểm tra action, tham số, target, version, cluster health và dependency trước
    khi cho phép tạo proposal.
  - Chặn command/flag không hỗ trợ và trả thông báo có thể hiểu trên Dashboard.
- [x] **0.4 Safety policy hardening**
  - Bổ sung mức `READ_ONLY/SAFE/RISKY/DESTRUCTIVE`.
  - Bảo đảm thao tác mất dữ liệu như xóa pool, purge hoặc ghi đè production không
    thể nằm trong luồng auto-run.
  - Chuẩn hóa approval, expiry, stale-evidence check và idempotency key.
- [x] **0.5 Kiểm thử**
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

> **Kế hoạch chi tiết:** `Plan/log-intelligence-rca-plan.md` (Log Intelligence &
> AI RCA, lập 2026-08-18) — thiết kế tầng thu thập/fingerprint/triage/phân tích
> log mon/mgr/osd/rgw làm nguồn evidence cho 6.1–6.3, theo yêu cầu đưa đội RCA
> vào. Kho log đã chốt: **Loki** (mục 11.1 của plan).
>
> - [x] **L0 — Thu thập + fingerprint** · [x] **L1 — Triage tất định** · [x] **L2 — Phân tích AI** (2026-08-18) · [x] **L3 — Cảnh báo + vòng đời** · [x] **L4 — Dashboard + đề xuất** (2026-08-19). Xem nhật ký mục 8.
> - [x] **L6 — Kiểm thử đầu-cuối + runbook** (2026-08-19, `docs/runbook-log-intelligence.md`).
> - [ ] L5 adapter Loki chạy thật (chờ hạ tầng đội RCA)

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
| 2026-08-17 | 0.1 Cluster capability inventory | Hoàn thành | Thêm bảng `cluster_capability_inventory` (migration `6b5e22967d5f`) + enum `CapabilityStatus`; collector `watcher/capability_inventory.py::scan_and_store` chạy theo cadence riêng (`capability_inventory_scan_interval_seconds`, mặc định 300s) trong cả 2 vòng lặp Watcher (cụm mặc định + cụm quan sát thêm), tái dùng `ceph_client.summarize_cluster_versions`/`summarize_versions_payload` đã có sẵn cho phần mixed-version; deployment mode lấy từ `cluster.ceph_exec_mode` (chưa tự dò `ceph orch`, để dành Pha 0.2+ nếu cần). Dashboard `/clusters` hiển thị version/trạng thái mới nhất mỗi cụm. | `pytest tests/test_capability_inventory.py` (9/9 pass) + toàn bộ suite `pytest -q` (2170 passed, 3 fail KHÔNG liên quan — `test_mq.py`/`test_dashboard_pools.py`, tái hiện y hệt trên `main` chưa sửa, do thiếu RabbitMQ broker thật trong môi trường) + `alembic upgrade heads` áp thành công vào Postgres dev thật | Chờ commit |
| 2026-08-17 | 0.2 Capability matrix có nguồn kiểm chứng (hạ tầng) | Một phần | Thêm bảng `capability_matrix_entries` + `capability_matrix_changes` (migration `18f374b79a75`, lịch sử append-only, không upsert); `shared/capability_matrix.py::check_capability(command_id, ceph_major)` fail-closed đúng đặc tả (không có entry -> `UNKNOWN`, có entry nhưng không phủ version -> `UNSUPPORTED_VERSION`, có entry phủ version -> `SUPPORTED` kèm cờ `is_stale` theo `capability_matrix_max_age_days`, mặc định 180 ngày); trang admin `/capability-matrix` (`dashboard/routes/capability_matrix.py`) cho thêm/deprecate entry, `verified_by` luôn lấy từ user admin đang đăng nhập (không thể giả qua form), bắt buộc Doc URL dạng http(s), lưu lịch sử thay đổi. **Cố ý CHƯA seed dữ liệu thật** — bảng khởi tạo rỗng nên mọi capability check hiện tại trả `UNKNOWN` (đúng theo "fail closed" của roadmap), vì AI không tự xác minh tài liệu Ceph chính thức rồi tự nhận là "người duyệt" thay cho operator; cần operator tự kiểm tra docs.ceph.com/download.ceph.com và nhập entry qua trang admin. Đây là lý do đánh dấu `[~]` chứ không phải `[x]`. | `pytest tests/test_capability_matrix.py` (10/10 pass) + `pytest tests/test_dashboard_capability_matrix.py` (5/5 pass) + `alembic upgrade heads` áp thành công vào Postgres dev thật | Chờ commit |
| 2026-08-17 | 0.3 AI preflight validator | Một phần | Thêm `worker/preflight.py::run_preflight(session, cluster_id, action_id)` — gọi ngay trong `worker/llm/router_client.py::diagnose_incident`'s (nhánh tạo Action MỚI, trước `gate.classify_action`), fail-closed 3 bước theo đúng thứ tự: (1) `Cluster.is_active` — cụm đã vô hiệu hoá thì chặn; (2) Pha 0.1's `ClusterCapabilityInventory` — chưa quét lần nào hoặc lần quét gần nhất không phải `SUPPORTED` (kể cả `UNAVAILABLE`/`UNKNOWN`/mixed-version) thì chặn, INSUFFICIENT_EVIDENCE; (3) Pha 0.2's `capability_matrix.check_capability(action_id, ceph_major)` — `UNKNOWN`/`UNSUPPORTED_VERSION` đều chặn. Không tự dựng dependency graph (roadmap dùng từ "dependency" nhưng codebase chưa có khái niệm này ở đâu khác — diễn giải hẹp thành "cluster còn active" để tránh dựng trừu tượng speculative); "cluster health" tách biệt với is_active chưa làm ở bước này (Incident luôn xuất phát từ 1 lần cluster KHÔNG khoẻ nên tự nó không phải tín hiệu hữu ích tại đây; một guard "cluster health" tổng quát hơn cho MỌI action family còn lại thuộc phạm vi rộng hơn 0.3, để dành ý kiến operator). **Enforcement mặc định TẮT** (`settings.ai_preflight_enforcement_enabled = False`) — validator vẫn chạy và log/verdict mỗi lần, nhưng KHÔNG chặn Action thật cho tới khi operator tự bật, vì bảng Capability Matrix (Pha 0.2) đang rỗng trên mọi deployment hiện có (kể cả `ceph-aiops-prod` đang tự động thực thi SAFE action) — bật mặc định sẽ âm thầm tắt toàn bộ auto-remediation đang chạy thật. Khi bị chặn (lúc enforcement bật): `Incident.diagnosis_text` được nối thêm lý do, `Incident.status = FAILED`, ghi `AuditEntry` (`EVENT_PROPOSAL_BLOCKED_BY_PREFLIGHT`), không tạo `Action` — tái dùng đúng kênh Dashboard đã hiển thị `diagnosis_text` sẵn có, không tạo kênh mới. Đây là lý do đánh dấu `[~]`: cluster-health check tổng quát và việc bật enforcement thật (sau khi Pha 0.2 có dữ liệu) còn để ngỏ cho operator quyết định. | `pytest tests/test_preflight.py` (7/7 pass) + 3 test tích hợp mới trong `pytest tests/test_router_client.py` (enforcement tắt vẫn tạo Action / enforcement bật thì chặn đúng / enforcement bật nhưng preflight pass vẫn tạo Action — 83/83 cả file pass, không có test cũ nào bị ảnh hưởng vì mặc định tắt) | Chờ commit |
| 2026-08-18 | 0.4 Safety policy hardening | Một phần | 4 mức `ActionClassification` (`READ_ONLY`/`SAFE`/`RISKY`/`DESTRUCTIVE`, migration `5bdecca5014e` — cột `expires_at`/`idempotency_key` mới trên `actions` + mở rộng CheckConstraint qua `batch_alter_table` để tương thích SQLite). `worker/policy/gate.py::classify_action` giờ ưu tiên DESTRUCTIVE > RISKY > SAFE > READ_ONLY > mặc định RISKY (giữ nguyên tinh thần AD-5 cũ, chỉ mở rộng 2→4 mức). Chuyển 7 action_id từ `risky:` sang `destructive:` trong `action_policy.yaml` — **không đổi hành vi**, cả 7 đều đã luôn cần duyệt Dashboard, đây chỉ là nhãn chặt hơn + đảm bảo cấu trúc (không chỉ policy) không bao giờ auto-run: `pg_repair_force`, `delete_cluster_cephadm`, `delete_cluster_manual`, `restore_cluster_from_backup`, `rbd_trash_remove`, `rbd_trash_purge_all`, `restore_rbd_image_to_production`, `backup_delete_manual`. **`delete_pool` CỐ Ý giữ nguyên SAFE** theo quyết định operator tái xác nhận ngày 2026-08-18 (quyết định gốc 2026-07-23) — dù cũng là thao tác mất dữ liệu, Chat-with-AI's confirm-với-preview-lệnh-đã-resolve được operator coi là đủ an toàn cho luồng đó; roadmap 0.4 không được diễn giải để tự ý đảo ngược quyết định này. 2 lớp guard cứng chống DESTRUCTIVE lọt vào auto-run: `worker/llm/router_client.py::_maybe_execute_safe_action` tự gọi lại `classify_action` và refuse nếu DESTRUCTIVE (không chỉ tin caller); `dashboard/routes/chat.py`'s auto-approve kế thừa an toàn tự động vì `is_safe` không bao giờ True cho action_id destructive. Approval expiry (`settings.action_approval_expiry_hours`, mặc định 24h) + stale-evidence check trong `approve_action_core` (`ApprovalOutcome.EXPIRED` mới — Action ở lại PENDING_APPROVAL, không tự reject) — chỉ set `expires_at` ở nhánh Incident-diagnosis (router_client.py), mọi action family khác NULL = không áp dụng. Idempotency key = sha256(action_id + nodes đã sort + action_params), CỐ Ý KHÔNG gồm incident_id (mới bắt được trùng lặp CROSS-incident) + unique index CHỈ trong phạm vi status in-flight (PENDING/PENDING_APPROVAL/APPROVED) — không phải unique vĩnh viễn (nếu không sẽ chặn vĩnh viễn 1 command tái diễn hợp lệ sau khi lần trước đã xong). Trong lúc làm: gặp 2 lần va chạm migration concurrent (`2a98d04f2f06` xoá rồi có người tạo lại làm gãy chain của 1 migration Log Intelligence không liên quan — đã re-point về head đúng; `op.drop_constraint` không chạy được trên SQLite — đã sửa dùng `batch_alter_table` theo đúng tiền lệ `1bd5de967b1a`). | `pytest tests/test_policy_gate.py` (32/32, cập nhật 5 assertion theo phân loại mới) + `pytest tests/test_router_client.py tests/test_dashboard_actions.py tests/test_telegram_approval_bot.py` (186/186, gồm 7 test mới cho expiry/idempotency/hard-guard) + cập nhật 4 test khác nơi seed data giả định RISKY cho action_id đã chuyển sang DESTRUCTIVE (`test_dashboard_backups.py`, `test_dashboard_delete_cluster.py` x2, `test_dashboard_restore_cluster.py`, `test_dashboard_volumes.py`) + `pytest tests/test_migrations.py` (7/7, xác nhận migration chạy sạch trên SQLite fresh) + `alembic upgrade heads` áp thành công vào Postgres dev thật | Chờ commit |
| 2026-08-18 | 0.5 Kiểm thử | Hoàn thành | File riêng `tests/test_pha0_safety_matrix.py` (25 test), phủ đúng từng gạch đầu dòng của 0.5: **matrix tối thiểu** — parametrize theo TẤT CẢ major mà `shared/ceph_releases.RELEASES` còn công nhận (13–20, Mimic→Tentacle), xác nhận Pha 0.1 trả SUPPORTED + Pha 0.1→0.2→0.3 end-to-end cho từng major khi matrix phủ đúng; **version không biết** — major ngoài RELEASES (vd RELEASES lớn nhất + 50) → preflight chặn UNSUPPORTED_VERSION; **mixed-version** — 2 daemon 2 version khác nhau (mô phỏng nâng cấp dở dang) → chặn; **flag bị loại bỏ** — entry `max_major` giới hạn (vd hỗ trợ tới Pacific/16, bỏ ở Quincy/18) → SUPPORTED ở 16, UNSUPPORTED_VERSION ở 18, cùng 1 command_id; **prompt injection** — 2 test: (1) `log_excerpt`/response giả lập bị chèn chỉ thị độc hại cố ép action_id ngoài enum (`rm_rf_everything`) → `RouterDiagnosisError`, không tạo Action; (2) injection nằm trong `rationale` (cố chèn `rm -rf`) nhưng action_id vẫn hợp lệ → lưu rationale nguyên văn để operator xem lại (không parse/thực thi), lệnh thật vẫn do command builder có tham số kiểu dữ liệu build ra, không phải free text; **hallucinated action** — `investigate_manually` (action_id hợp lệ, cố ý không có Command) → `approve_action_core` đóng thành ACKNOWLEDGED, không crash; **stale evidence** — 3 test, gồm 1 LỖ HỔNG THẬT phát hiện khi viết test này: `worker/preflight.py` trước đó chỉ kiểm tra `status == SUPPORTED` của capability inventory snapshot gần nhất, KHÔNG kiểm tra snapshot đó cũ bao lâu — một cụm mà Watcher đã ngừng quét (mất kết nối, cụm bị thay đổi ngoài băng thông giám sát) vẫn được coi là "còn hỗ trợ" mãi mãi dựa trên snapshot SUPPORTED cũ. Đã vá: setting mới `capability_inventory_max_age_seconds` (mặc định 3600s, gấp ~12 lần cadence quét 300s để tránh false-positive) + `run_preflight` chặn nếu snapshot cũ hơn ngưỡng này (INSUFFICIENT_EVIDENCE); đồng thời test xác nhận `capability_matrix`'s `is_stale` (tài liệu cũ nhưng vẫn SUPPORTED) và `Action.expires_at` (Pha 0.4, hết hạn duyệt) là 2 khái niệm "cũ" ĐỘC LẬP, không lẫn vào nhau. | `pytest tests/test_pha0_safety_matrix.py` (25/25 pass, gồm test cho lỗ hổng staleness vừa vá) + `pytest tests/test_preflight.py tests/test_capability_inventory.py tests/test_capability_matrix.py tests/test_policy_gate.py tests/test_router_client.py tests/test_dashboard_actions.py tests/test_dashboard_capability_matrix.py` (210/211, 1 lỗi KHÔNG liên quan — `Could not refresh instance`, cùng loại flake toàn-suite đã xác nhận trước đó, pass khi chạy riêng) | Chờ commit |
| 2026-08-18 | Pha 6 / L0 Log Intelligence — thu thập + fingerprint | Hoàn thành | Kế hoạch đầy đủ tại `Plan/log-intelligence-rca-plan.md` (5 tầng T1–T5, 6 ràng buộc thiết kế, lộ trình L0–L6); **chốt Loki** thay ELK (mục 11.1: tầng T2 đã tự fingerprint nên inverted index toàn văn của ES là chi phí thừa; kèm ràng buộc bắt buộc KHÔNG lưu chunk Loki lên chính cụm Ceph đang giám sát — phụ thuộc vòng). Code L0: 3 bảng `log_ingest_runs`/`log_patterns`/`log_pattern_observations` (migration `6192ad592f06`) — cố ý KHÔNG bảng nào chứa log thô, chỉ template + số đếm + 1 dòng mẫu đã redact, vì DB của chính app là tài nguyên có cảnh báo (`watcher/database_capacity_monitor.py`); tầng adapter `watcher/log_source/` (Protocol + `ssh_tail` tái dùng `watcher/ceph_log.py` đã có + `loki` query_range) để tầng phân tích không phụ thuộc nguồn log — đội RCA đổi hạ tầng thì chỉ thêm 1 file; `watcher/log_intel.py` chuẩn hoá dòng log thành template tất định (KHÔNG gọi AI — đây là thứ chặn trần chi phí AI ở L2: hàng triệu dòng co còn vài trăm template) + redaction cephx key/Authorization/X-Amz-Signature/Credential/token + đếm theo (pattern, giờ, host) + retention 2 mức (observations 30 ngày vs patterns/runs 180 ngày). Cắm vào cả 2 vòng lặp Watcher theo cadence riêng `log_intel_scan_interval_seconds` (mặc định 900s — chậm nhất trong các scan vì là scan duy nhất mở SSH per-daemon-per-node). **Mặc định TẮT** (`log_intel_enabled=False`) — operator tự bật trước khi app bắt đầu đọc cả cửa sổ log của mọi node. `ceph_log._fetch` thêm tham số `tail_lines` tuỳ chọn (mọi call site cũ giữ nguyên hành vi). Chưa có: triage/AI/cảnh báo/Dashboard (L1–L4). | `pytest tests/test_log_intel.py` (27/27 pass, gồm nhóm test bảo mật bắt buộc của plan mục 9 — nhóm này bắt được 1 lỗi thật: regex `authorization:` ban đầu chỉ che token đầu tiên, để lọt nguyên `Credential=AKIA...`, đã sửa thành che tới hết dòng) + `pytest tests/test_ceph_log.py tests/test_collector.py` (24/24) + `pytest tests/test_watcher_main.py` (28/28) — không có test cũ nào hỏng; `alembic upgrade head` rồi `downgrade -1` chạy sạch trên DB sqlite mới tinh qua toàn bộ chuỗi migration | Chờ commit |
| 2026-08-18 | Pha 6 / L1 Log Intelligence — triage tất định | Hoàn thành | `watcher/log_triage.py::triage_window(cluster_id, window_start, window_end)` — tầng quyết định "mẫu log nào đáng nhìn", chạy hoàn toàn tất định (không AI, không token, chạy được cả khi router AI chết). Đây là chốt chặn chi phí cho L2: L0 co hàng triệu dòng thành vài trăm mẫu, L1 co tiếp xuống còn vài mẫu thật sự bất thường; chỉ mẫu gắn cờ ở đây mới bao giờ được đưa lên model. 4 lý do gắn cờ: `NOTABLE` (operator tự đánh dấu), `NOVEL` (mẫu chưa từng có trước cửa sổ + đạt `log_intel_novelty_min_count`), `SEVERE` (Ceph ghi mức lỗi `prio <= -1`, hoặc khớp 13 từ khoá hạt nhân đặc trưng Ceph — cố ý KHÔNG đưa "error"/"failed" chung chung vào vì sẽ làm mất tác dụng lọc), `BURST` (tần suất >= `log_intel_burst_ratio` lần baseline). Nhãn `BENIGN` loại bỏ mẫu trước mọi kiểm tra khác — cách tắt nhiễu không cần sửa code. **Baseline so theo CÙNG KHUNG GIỜ TRONG NGÀY** chứ không phải trung bình phẳng: cụm Ceph có nhịp ngày rõ (scrub đêm, backup rạng sáng), trung bình phẳng sẽ gắn cờ sai mỗi đêm và làm tầng triage mất uy tín; dưới `log_intel_burst_min_baseline_samples` mẫu lịch sử thì IM LẶNG, không đoán (giữ cho tuần đầu chạy — baseline còn rỗng — không thành mưa cảnh báo giả). `baseline_mean`/`burst_ratio` trả None chứ không phải 0 khi chưa đo được (evidence, roadmap 3.1 — ở L2 chính nó quyết định model được kết luận hay phải trả INSUFFICIENT_EVIDENCE). KHÔNG có bảng riêng: kết quả luôn tính lại được từ `log_patterns`+`log_pattern_observations`, thêm bảng chỉ tạo thứ có thể lệch với nguồn sự thật; chỉ SỐ ĐẾM được ghi vào cột mới `log_ingest_runs.patterns_flagged` (migration `5cca511ebd3c`, nullable để phân biệt 0 "đã triage, không có gì" với NULL "lần quét trước khi có L1"). Cắm vào `log_intel.scan_and_store` sau bước lưu, bọc try/except riêng (triage hỏng không được kéo theo kết quả thu thập đã xong) và log mức WARNING — trước khi có L3/L4 thì log Watcher LÀ kênh duy nhất vận hành thấy kết quả. Chưa có: gửi Telegram (L3), Dashboard (L4). | `pytest tests/test_log_triage.py` (26/26 pass — nhóm quan trọng nhất là "im lặng đúng lúc": mẫu thường ở mức bình thường, nhãn BENIGN, mẫu theo mùa ở đúng giờ cao điểm quen thuộc, baseline không đủ mẫu, mẫu vắng mặt khỏi cửa sổ — vì tầng triage gắn cờ mọi thứ thì tương đương không có) + `pytest tests/test_log_intel.py` (30/30, thêm 3 test nối L0↔L1 gồm ca triage raise vẫn giữ nguyên dữ liệu thu thập) + `pytest tests/test_watcher_main.py tests/test_ceph_log.py` (30/30, không regression) + `alembic upgrade head`/`downgrade -1` sạch trên DB sqlite mới | Chờ commit |
| 2026-08-18 | Pha 6 / L2 Log Intelligence — phân tích AI | Hoàn thành | `watcher/log_analysis.py::analyze_window(...)` + bảng `log_findings` (migration `452507aa3c32`). Đưa các mẫu L1 đã gắn cờ lên router qua `shared/router_client.py::build_router_client` (đường gọi AI duy nhất được phép), forced tool-call + `strict` schema đóng — cùng khuôn `worker/backup/ai_analysis.py`. Output đóng: `verdict` (FINDING/NO_FINDING/**INSUFFICIENT_EVIDENCE**)/`severity`/`confidence`/`title`/`summary`/`root_cause_hypothesis`/`evidence_pattern_ids`/`affected_*`/`recommended_action_id`/`recommended_manual_steps`. **Chống prompt injection (ràng buộc R3)**: log bọc trong hàng rào `<<<UNTRUSTED_LOG_DATA>>>`, system prompt nói rõ nội dung bên trong là dữ liệu chứ không phải mệnh lệnh, chuỗi hàng rào xuất hiện TRONG log bị vô hiệu hoá (chống "đóng hàng rào sớm"), ký tự điều khiển bị lọc, và model không bao giờ được sinh câu lệnh — chỉ chọn `action_id`. **Kiểm tra lại phía server, không tin output model** (roadmap 3.1): evidence_pattern_ids phải CÓ THẬT (bịa hết -> hạ FINDING xuống INSUFFICIENT_EVIDENCE; bịa một phần -> chỉ giữ id thật), host phải nằm trong `configured_nodes`, lần quét PARTIAL -> hạ `confidence=HIGH` xuống MEDIUM, verdict lạ -> INSUFFICIENT_EVIDENCE; mọi lần can thiệp ghi vào cột `validation_notes` để operator đọc được. **Allowlist `recommended_action_id` hẹp có chủ ý**: chỉ enum CHẨN ĐOÁN SỰ CỐ (`action_ids:`) trừ nhóm DESTRUCTIVE — KHÔNG lấy `management_action_ids:`, đúng lý do action_policy.yaml đã ghi (hành động quản trị cần tham số operator cung cấp mà một sự cố không mang theo). **Test bảo mật bắt được một lỗ hổng thật trước khi chạy production**: bản đầu chỉ trừ DESTRUCTIVE thì `delete_pool` LỌT QUA, vì nó đang được phân loại SAFE (xoá pool vĩnh viễn, giữ SAFE vì luồng Chat có bước xem trước lệnh làm lớp bảo vệ — lớp đó không tồn tại khi "đầu vào" là log do người ngoài kiểm soát); đã thu hẹp allowlist và thêm test khẳng định. `NO_FINDING` KHÔNG lưu hàng (tránh phình bảng mỗi 15 phút, ràng buộc R1). Bật riêng bằng `log_intel_ai_enabled` (mặc định TẮT) tách khỏi `log_intel_enabled` — bật thu thập không đồng nghĩa bật chi tiêu token. Chưa có: gửi Telegram (L3), Dashboard (L4). | `pytest tests/test_log_analysis.py` (30/30 pass — nhóm bảo mật gồm prompt injection qua tên bucket, đóng hàng rào sớm, ký tự điều khiển, model bịa pattern_id, model đề xuất action huỷ dữ liệu, host lạ) + `pytest tests/test_log_intel.py tests/test_log_triage.py tests/test_policy_gate.py` (88/88, không regression) + `alembic upgrade head`/`downgrade -1` sạch trên DB sqlite mới | Chờ commit |
| 2026-08-19 | Pha 6 / L3 Log Intelligence — cảnh báo + vòng đời | Hoàn thành | `shared/telegram_alerts.py::send_log_finding_alert` / `send_log_finding_resolved_alert` — dùng chung kênh "Cụm Ceph" (`telegram_incident_*`) với `send_ai_incident_alert`, **KHÔNG mở kênh thứ 4** (thiết kế 3 kênh giữ nguyên là 3, AD-31). **Chống spam** (`watcher/log_analysis.py`): trước khi ghi, tra `dedupe_key` — đã có bản ghi chưa RESOLVED thì không tạo hàng mới và không báo lại, trả về id bản ghi cũ; một vấn đề kéo dài được quét lại mỗi 15 phút nên thiếu bước này người trực sẽ nhận vài trăm tin nhắn cho cùng một chuyện rồi tắt kênh. Vấn đề quay lại SAU khi đã đóng thì báo lại bình thường (dedupe chỉ chặn trong lúc còn mở, không chặn vĩnh viễn). **Ngưỡng báo**: chỉ `severity` WARNING/CRITICAL mới gửi Telegram; INFO và INSUFFICIENT_EVIDENCE vẫn được LƯU cho Dashboard (L4) nhưng không làm rung điện thoại — báo mọi thứ sẽ dạy người trực bỏ qua kênh. **Nội dung cảnh báo mang EVIDENCE GỐC** (template log thật qua `resolve_pattern_templates`) chứ không chỉ kết luận AI, kèm `validation_notes` nếu server đã phải sửa/hạ cấp câu trả lời của model — người trực phải tự đánh giá được, không phải tin lời model. **Vòng đời OPEN→RESOLVED** (`resolve_stale_findings`): đóng khi MỌI mẫu trong `evidence_pattern_ids` đều có `last_seen_at` trước cửa sổ hiện tại (một mẫu còn chạy = hiện tượng mới giảm chứ chưa hết); đọc thẳng `LogPattern.last_seen_at` mà L0 vẫn cập nhật nên KHÔNG cần AI — vòng đời không kẹt khi `log_intel_ai_enabled` tắt hoặc router chết; phát hiện không trích dẫn mẫu nào (INSUFFICIENT_EVIDENCE) không bao giờ tự đóng (không có gì đối chiếu, để operator xử lý); ACKNOWLEDGED vẫn tự đóng được. Bỏ qua bước đóng khi lần quét FAILED — lúc đó không đọc được log node nào, "mẫu không còn xuất hiện" là kết luận sai và sẽ đóng nhầm hàng loạt. Gửi Telegram là best-effort, lỗi gửi không xoá sổ kết quả phân tích. Chưa có: Dashboard (L4). | `pytest tests/test_log_alerting.py` (17/17 pass — trọng tâm chống spam: 3 lần quét cùng vấn đề chỉ 1 tin nhắn và 1 hàng DB, vấn đề khác vẫn báo riêng, tái phát sau khi đóng thì báo lại; cùng nhóm vòng đời và ngưỡng báo) + `pytest tests/test_log_intel.py tests/test_log_triage.py tests/test_log_analysis.py tests/test_log_alerting.py` (103/103) + `pytest tests/test_shared_telegram_alerts.py tests/test_dashboard_telegram_alerts.py tests/test_telegram_client.py` (62/62, chạy 3 lần liên tiếp đều sạch — một lần lỗi `Could not refresh instance <Cluster>` quan sát được là flake nhiễm chéo giữa file test, tái hiện độc lập với thay đổi này) | Chờ commit |
| 2026-08-19 | Pha 6 / L4 Log Intelligence — Dashboard + đề xuất advisory | Hoàn thành | **Trang `/log-intelligence`** (`dashboard/routes/log_intelligence.py` + `dashboard/templates/log_intelligence.html`, nav thêm vào 21 template có dropdown Cluster): 3 khối theo đúng thứ tự người điều tra cần đọc — (1) **Trạng thái thu thập** đặt TRÊN CÙNG có chủ ý (PARTIAL/số node hụt/lý do), vì mọi kết luận bên dưới chỉ đáng tin bằng đúng độ đầy đủ của dữ liệu sinh ra nó; (2) **Phát hiện** luôn kèm bằng chứng gốc (mẫu log thật) + `validation_notes` + model/prompt version để truy vết, ghi rõ đây là "giả thuyết của AI" chứ không phải phép đo; (3) **Mẫu log** với nút gắn nhãn BENIGN/NOTABLE — cách "dạy" tầng triage im lặng mà không cần sửa code, nên phải nằm trên giao diện chứ không phải trong file cấu hình (giới hạn admin: gắn BENIGN sai sẽ làm hệ thống im lặng với đúng thứ lẽ ra phải báo). Nút "Đã ghi nhận" chỉ OPEN→ACKNOWLEDGED, KHÔNG cho bấm sang RESOLVED — một phát hiện chỉ hết khi mẫu log thật sự ngừng, đo bằng dữ liệu chứ không bằng cú bấm nút. **Đề xuất hành động** (`watcher/log_analysis.py::_maybe_propose_action`): phát hiện WARNING/CRITICAL sinh `Incident(ceph_code="LOG_ANOMALY:<dedupe_key[:12]>")` + `Action(PENDING_APPROVAL)` theo đúng khuôn `watcher/osd_latency_monitor.py`; ceph_code tất định nên bước đóng tìm lại đúng Incident mà KHÔNG cần thêm cột FK/migration nào. Ràng buộc R5 giữ bằng đúng một điều: Action luôn sinh ở PENDING_APPROVAL (không đường nào trong codebase tự phê duyệt — `_process_approved_actions_once` chỉ lấy APPROVED, và nhánh tự chạy SAFE nằm trong `diagnose_incident` mà Incident tạo thẳng vào DB không đi qua). Không có `recommended_action_id` thì fallback `investigate_manually` (vốn không có Command, "Duyệt" chỉ nghĩa là ghi nhận). Rationale luôn mở đầu "[Giả thuyết từ AI đọc log — độ tin cậy X]" kèm bằng chứng gốc: người duyệt phải biết mình đang duyệt dựa trên suy luận, khác hẳn OSD_LATENCY_HIGH vốn từ số liệu `ceph osd perf`. Finding hết → Incident/Action chờ duyệt tự đóng theo. **`LOG_ANOMALY:` bị loại khỏi `compute_cluster_status`**: badge hứa phản ánh "tình trạng CỦA CLUSTER (HEALTH_WARN/ERR thật)", để một suy luận của model bôi đỏ nó sẽ bào mòn niềm tin vào badge — phát hiện vẫn hiện đầy đủ trong danh sách Incident và hàng chờ duyệt, chỉ không tự đổi màu (khác OSD_LATENCY_HIGH/NODE_RESOURCE_HIGH vốn là phép đo và vẫn tính). Không cần migration cho bước này. Chưa có: adapter Loki chạy thật (L5), kiểm thử đầy đủ (L6). | `pytest tests/test_dashboard_log_intelligence.py` (19/19 pass — gồm ca badge không bị bôi đỏ bởi LOG_ANOMALY, Action luôn PENDING_APPROVAL, ceph_code không đẻ Incident trùng, finding hết thì Incident tự đóng) + `pytest tests/test_log_intel.py tests/test_log_triage.py tests/test_log_analysis.py tests/test_log_alerting.py tests/test_dashboard_log_intelligence.py` (122/122) + `pytest tests/test_dashboard_status.py tests/test_dashboard_feed.py tests/test_dashboard_deploy_cluster.py` (68/68, không regression ở các nơi dùng compute_cluster_status) | Chờ commit |
| 2026-08-19 | Pha 6 / L6 Log Intelligence — kiểm thử đầu-cuối + runbook | Hoàn thành | **Sửa một bug retention THẬT phát hiện trong bước này**: `log_findings.ingest_run_id` là FK NOT NULL vào `log_ingest_runs`, nhưng `prune_old_rows` xoá thẳng `log_ingest_runs` theo cutoff — trên Postgres (luôn cưỡng chế FK) nó ném IntegrityError, và vì lệnh xoá observations nằm CÙNG transaction nên bị rollback theo ⇒ **retention ngừng hoạt động HOÀN TOÀN, âm thầm**, đúng kiểu phình DB mà ràng buộc R1 sinh ra để tránh (sqlite mặc định TẮT cưỡng chế FK nên test sqlite không tự lộ; tái hiện riêng bằng `PRAGMA foreign_keys=ON`). Sửa thành thứ tự bắt buộc: (1) xoá finding quá hạn **chỉ khi đã RESOLVED** — finding còn OPEN/ACKNOWLEDGED không bao giờ bị xoá vì già, nó vẫn là việc chưa xong của người trực; (2) xoá observations; (3) chỉ xoá run **không còn finding nào trỏ vào** — giữ provenance cho mọi finding còn lưu. Thêm `log_intel_finding_retention_days` (90 ngày, đã có trong plan nhưng chưa hiện thực). `log_patterns` cố ý không xoá (danh mục, phình theo SỐ LOẠI chứ không theo khối lượng, và là nơi finding neo bằng chứng vào). **Test đầu-cuối mới** `tests/test_log_intelligence_e2e.py` (8 test): chạy nguyên chuỗi L0→L4 trên DÒNG LOG CEPH THÔ THẬT, chỉ giả lập đúng 3 biên giới ngoài (nguồn log / model / Telegram) — mọi thứ ở giữa (parse, redaction, fingerprint, đếm theo giờ, triage, dựng prompt, kiểm tra lại output, dedupe, tạo Incident, đóng vòng đời) là code thật; gồm ca prompt-injection đi xuyên toàn chuỗi với model NGHE THEO (server vẫn chặn, Action rơi về `investigate_manually`), ca PARTIAL hạ confidence đầu-cuối, ca router chết vẫn giữ dữ liệu, ca tắt AI vẫn thu thập+phân loại, ca quét lại không nhân đôi bất cứ thứ gì, ca log ngừng thì đóng cả finding lẫn Incident chờ duyệt. Timestamp trong log mẫu sinh theo `utcnow()` chứ không cắm cứng — log cắm cứng ngày giờ sẽ luôn rơi ngoài cửa sổ quét và làm test "xanh" vì lý do sai. **Runbook vận hành** `docs/runbook-log-intelligence.md`: quy trình bật 2 bước (thu thập trước 3–7 ngày rồi mới bật AI, kèm ngưỡng lành mạnh 0–5 mẫu gắn cờ/lần quét), cách đọc kết quả, 4 nhóm cách chỉnh khi nhiễu (ưu tiên gắn BENIGN), thủ tục tắt/rollback schema, bảng dung lượng+retention, bảng xử lý sự cố, và 4 ranh giới an toàn không được nới. | `pytest tests/test_log_intelligence_e2e.py` (8/8) + toàn bộ chuỗi `tests/test_log_intel.py tests/test_log_triage.py tests/test_log_analysis.py tests/test_log_alerting.py tests/test_dashboard_log_intelligence.py tests/test_log_intelligence_e2e.py` (**133/133**) + `pytest tests/test_watcher_main.py tests/test_dashboard_status.py` (43/43, không regression) + kiểm riêng bug retention dưới `PRAGMA foreign_keys=ON`: trước khi sửa ném IntegrityError, sau khi sửa chạy sạch và không để lại finding mồ côi | Chờ commit |
| 2026-08-19 | 0.4 Safety policy hardening — `delete_pool` sang `destructive:` | Hoàn thành | Quyết định của operator. Chuyển `delete_pool` từ `safe:` sang `destructive:` trong `worker/policy/action_policy.yaml` — **chỉ sửa YAML, không sửa code** (đúng posture "operational decision" của file này). **Đây là thay đổi HÀNH VI**: trước đó confirm trên Chat là thực thi ngay (Action tạo thẳng ở APPROVED cho `poll_approved_actions()` nhặt); giờ dừng ở PENDING_APPROVAL và hiện trên mục "Chờ duyệt" — operator vẫn xem lệnh đã resolve ở bước confirm rồi Duyệt lần hai trên Dashboard. Luồng Chat xử lý sẵn (`dashboard/routes/chat.py` chỉ phân nhánh `is_safe = classification == SAFE`), không cần đụng tới. Ba lý do đổi so với quyết định 2026-07-23 (giữ SAFE vì bước xem trước lệnh trong Chat được coi là đủ): (1) DoD của chính Pha 0.4 nêu đích danh "thao tác mất dữ liệu như **xóa pool**, purge hoặc ghi đè production không thể nằm trong luồng auto-run" — giữ SAFE là mâu thuẫn với đúng pha đã tạo ra tầng `destructive:`; (2) kill-switch — lớp chặn cuối cho mọi action tự chạy — đã bị gỡ 2026-08-11 (commit a3864dd), nên "chạy ngay" giờ có hậu quả nặng hơn hẳn lúc quyết định cũ được đưa ra, vì không còn cách nào dừng giữa chừng; (3) Pha 6 (Log Intelligence) mở thêm một đường mà AI đọc dữ liệu NGƯỜI NGOÀI TÁC ĐỘNG ĐƯỢC (tên bucket/client trong log RGW) rồi đề xuất action_id — đường đó có allowlist riêng rất hẹp nên chưa bao giờ chạm tới `delete_pool`, nhưng một hành động huỷ dữ liệu nằm trong `safe:` là mìn cho đường tiếp theo. `delete_pool` vẫn nằm trong `management_action_ids:` — Chat vẫn đề xuất được, chỉ khác là phải Duyệt thêm một bước. **Kiểm lại toàn bộ `safe:` sau thay đổi: không còn hành động huỷ dữ liệu nào sót lại**, và cả 3 trường hợp DoD nêu đích danh (xóa pool / purge / ghi đè production) đều đã DESTRUCTIVE. | `pytest tests/test_policy_gate.py` (34/34 — cập nhật `test_management_action_ids_are_classified_safe` bỏ delete_pool, thêm `test_delete_pool_is_classified_destructive` và `test_delete_pool_stays_proposable_from_chat`) + test hành vi mới `test_confirm_action_delete_pool_now_waits_for_a_second_approval` trong `tests/test_dashboard_chat.py` (48/48 cả file) + `pytest tests/test_policy_gate.py tests/test_dashboard_chat.py tests/test_chat_client.py tests/test_commands.py tests/test_dashboard_actions.py tests/test_log_analysis.py` (**322/322**, không regression) | Chờ commit |
| 2026-08-19 | 0.4 đánh dấu hoàn thành (kiểm chứng lại) | Hoàn thành | Không viết thêm code — rà lại và xác nhận cả 3 gạch đầu dòng của 0.4 đã đủ: (1) 4 mức READ_ONLY/SAFE/RISKY/DESTRUCTIVE có trong `ActionClassification` + `classify_action` với precedence "bảo thủ nhất thắng"; (2) thao tác mất dữ liệu không còn trong luồng auto-run — sau khi `delete_pool` chuyển sang `destructive:` (cùng ngày), soát lại toàn bộ `safe:` không còn hành động huỷ dữ liệu nào, và cả 3 ca DoD nêu đích danh (xóa pool / purge / ghi đè production) đều DESTRUCTIVE; (3) approval expiry + idempotency key đã nối dây THẬT hai đầu chứ không chỉ có cột: ghi ở `worker/llm/router_client.py:584-585` (`expires_at` theo `action_approval_expiry_hours`, `idempotency_key` từ `_compute_idempotency_key`) và ở `dashboard/routes/volumes.py` (8 call site), đọc/chặn ở `dashboard/routes/actions.py:146` với `EVENT_RISKY_ACTION_APPROVAL_EXPIRED` riêng, cùng unique constraint `uq_actions_idempotency_key_inflight` chỉ áp cho hàng đang in-flight. Trước đây để `[~]` là do chưa ai rà lại sau khi các mảnh landed rời rạc. | Không có thay đổi code cần test; xác minh bằng đọc mã + `pytest tests/test_pha0_safety_matrix.py tests/test_dashboard_actions.py tests/test_models.py tests/test_router_client.py` (đã xanh trong lần chạy 322/322 cùng ngày) | Chờ commit |
| 2026-08-19 | 0.2 Capability matrix — báo cáo độ phủ (gỡ thế bí seed) | Một phần (hạ tầng xong) | **Vấn đề thật đang chặn cả Pha 0**: bảng matrix khởi tạo rỗng có chủ đích, mà preflight thì fail-closed ⇒ bật enforcement bây giờ sẽ chặn SẠCH mọi đề xuất remediation tự động. Nên không ai bật được, và cả lớp an toàn nằm im vô thời hạn. Nguyên nhân sâu hơn không phải "lười seed" mà là **không ai nhìn thấy việc cần làm lớn cỡ nào**: trang admin chỉ liệt kê entry đã có (rỗng), không hề cho biết cần bao nhiêu entry hay thiếu cái gì — nghe như việc vô hạn. Thực tế preflight chỉ gác đúng enum chẩn đoán sự cố: **7 action_id**. Thêm `shared/capability_matrix.py::gated_command_ids()` (đọc thẳng `action_ids:` từ policy yaml, KHÔNG lấy họ management/Chat vì chúng không đi qua cổng này — gộp vào sẽ thổi phồng việc cần làm) và `coverage_report(ceph_major)` trả về từng action_id đang ở trạng thái nào + số sẽ bị chặn nếu bật enforcement. Trang `/capability-matrix` thêm mục "Mức sẵn sàng bật cổng an toàn": hiện trạng thái enforcement, và **độ phủ tính riêng cho TỪNG cụm theo đúng phiên bản Ceph Pha 0.1 dò được** — không gộp thành một chỉ số chung, vì một entry chỉ phủ một khoảng major version nên cùng bảng có thể đủ cho cụm Reef mà vẫn hổng cho cụm Nautilus bên cạnh. Operator giờ thấy trước hậu quả TRƯỚC khi bật công tắc thay vì bật rồi mới biết cái gì gãy. **Cố ý KHÔNG tự seed dữ liệu** — giữ nguyên nguyên tắc mục 3.2 của roadmap: mỗi entry phải có Doc URL tài liệu Ceph chính thức mà người thật đã đọc, và `verified_by` luôn là admin đang đăng nhập; AI dựng công cụ thì được, tự nhận là người kiểm chứng thì không. Vẫn `[~]` cho tới khi operator nhập entry thật. | `pytest tests/test_capability_matrix.py` (15/15, thêm 5 test: enum bị gác là enum chẩn đoán chứ không phải mọi action, bảng rỗng báo chặn hết, seed 1 entry chuyển đúng 1 dòng, entry ngoài khoảng version vẫn tính là chặn, cụm chưa quét version thì chặn hết) + `pytest tests/test_dashboard_capability_matrix.py` (7/7, thêm 2 test render — trong đó một test ban đầu SAI kỳ vọng của tôi: tưởng seed xong là SUPPORTED, thực ra còn cần Pha 0.1 đã quét ra version, tức fail-closed đang chạy đúng) | Chờ commit |
| 2026-08-19 | Pha 6 / Log Intelligence — form cấu hình trên Dashboard | Hoàn thành | Lỗ hổng do operator chỉ ra: mọi cấu hình khác (cụm Ceph, router AI, database, patch pipeline) đều sửa được trên trang Cài đặt, riêng `log_intel_*` chỉ sửa được bằng tay trong `.env` rồi tự restart — tức tính năng coi như chưa dùng được cho người không đụng vào server. Thêm mục **Cài đặt → Log Intelligence** (`dashboard/routes/settings.py::log_intel_settings_submit` + panel trong `settings.html`, theo đúng khuôn `patch_pipeline_settings_submit`/`PATCH_PIPELINE_ENV_NAMES` sẵn có): 2 công tắc tách riêng (Thu thập / Phân tích AI) kèm dòng nhắc thứ tự bật khuyến nghị NGAY TRÊN FORM chứ không bắt đọc runbook mới biết, chọn nguồn ssh|loki, Loki URL + tenant, chu kỳ quét, cửa sổ, số dòng tối đa. Thêm nút **Kiểm tra kết nối Loki** (`POST /settings/log-intel/test-loki`, gọi `/ready`, KHÔNG lưu gì) — cùng posture "test kết nối" mà form Database/OpenStack đã có. Lưu xong restart **Watcher** (không phải Worker — Watcher mới là tiến trình chạy vòng quét; có test khoá đúng điều này). **Ba lỗi bị chặn ngay tại form** thay vì để lưu rồi hỏng lúc chạy: (1) chọn `loki` mà bỏ trống URL — cấu hình thiếu mà im lặng trông y hệt cụm không phát sinh log nào; (2) URL không có http(s); (3) **cửa sổ ≤ chu kỳ quét** — mỗi tick chậm sẽ để lại lỗ hổng dữ liệu VĨNH VIỄN không lấy lại được, đây là cái đáng chặn nhất vì hậu quả không hồi phục. Ngưỡng triage (`burst_ratio`/`novelty_min_count`/`burst_min_baseline_samples`) cố ý KHÔNG đưa lên form: chúng chỉ cần chỉnh sau khi đã chạy thật và thấy nhiễu, đưa vào form cấu hình ban đầu là mời người dùng vặn ngưỡng khi chưa có dữ liệu để vặn theo (runbook mục 4 hướng dẫn ưu tiên gắn nhãn BENIGN trước). Runbook cập nhật theo: mục 2/5/9 giờ trỏ vào form thay vì bảo sửa `.env`. | `pytest tests/test_dashboard_log_intel_settings.py` (12/12 — gồm ca checkbox không tick phải hiểu là TẮT chứ không giữ giá trị cũ, ca restart đúng Watcher chứ không phải Worker, và 3 ca validation ở trên) + `pytest tests/test_dashboard_settings.py tests/test_env_config.py tests/test_dashboard_log_intel_settings.py` (154/154, không regression trên trang Settings sẵn có) | Chờ commit |
