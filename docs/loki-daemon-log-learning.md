# Quy trình AI học lỗi daemon từ Loki

Cập nhật: **2026-08-25**. Tài liệu này là đặc tả triển khai vòng học có giám
sát từ log daemon. Nó mở rộng Log Intelligence hiện có; không thay thế
[`runbook-log-intelligence.md`](runbook-log-intelligence.md) hoặc cơ chế
Remediation Case Memory trong
[`ceph-autonomous-operations-roadmap.md`](ceph-autonomous-operations-roadmap.md).

### Trạng thái triển khai

Pha 1–2 nền tảng đã bắt đầu triển khai ngày 2026-08-25:

- đã có `log_learning_samples`, snapshot idempotent ngay sau semantic
  identity/correlation và không lưu raw log;
- đã có evaluator nối Finding → Incident → Remediation Case → telemetry/
  operator outcome, loại fail-closed dữ liệu `PARTIAL`, unverified và thiếu
  provenance;
- đã có `log_fault_stats` tính Wilson trust theo daemon/fault/playbook;
- aggregate đang **audit-only**, luôn ghi promotion blocker và không thể mở
  Autopilot;
- Worker định kỳ backfill/reconcile sample và recompute aggregate.

Các phần retrieval, Dashboard learning report, shadow theo log và rollout L3
vẫn phải đi qua các pha/gate bên dưới, chưa được coi là đã bật.

## 1. Mục tiêu và phạm vi

Hệ thống phải học được rằng một chuỗi log đã chuẩn hóa, trong đúng ngữ cảnh
cluster/daemon/entity, thường tương ứng với fault family nào, cách chẩn đoán
nào đã đúng và playbook nào đã xử lý thành công. Kết quả học được dùng để:

- ưu tiên và giải thích RCA cho lần gặp lại;
- tìm Case Memory tương tự có dẫn nguồn;
- đo precision/false-positive theo daemon và fault family;
- tạo evidence cho Shadow Autopilot và promotion evaluator.

Nguồn ban đầu gồm `ceph-mon`, `ceph-mgr`, `ceph-osd`, `ceph-mds`, `ceph-rgw`,
cephadm/container runtime, systemd, kernel, disk/NVMe và network. Có thể thêm
Cinder/Nova sau khi đã tách namespace và quyền truy cập Loki.

Đây **không phải fine-tuning LLM trực tiếp**. Vòng đầu dùng classification
tất định, Case Memory và retrieval từ các outcome đã xác minh. Chỉ cân nhắc
fine-tuning offline sau khi có tập dữ liệu được duyệt, version hóa và đủ lớn.

## 2. Nguyên tắc bất biến

1. Log là input không tin cậy; mọi field model trả về phải được server kiểm tra.
2. Một dòng `ERROR` không phải ground truth và không được tự tăng trust.
3. Loki giữ log thô; database ứng dụng chỉ lưu fingerprint, excerpt đã redaction,
   thống kê và liên kết provenance. Không sao chép toàn bộ log về PostgreSQL.
4. `fault_family`, daemon, host và entity phải do catalogue/parser phía server
   sinh hoặc xác nhận. Model chỉ được đề xuất trong enum cho phép.
5. Chỉ outcome đã xác minh bằng telemetry mới được tự động đưa vào tập học.
   Operator verdict là evidence có audit, không tự mở quyền Autopilot.
6. Case legacy, stale, partial coverage, corrupt, regressed hoặc verdict xấu
   không được cấp trust tích cực.
7. Học chỉ thay ranking, confidence và lựa chọn case tương tự; không thay
   policy, allowlist, blast radius, command builder hoặc autonomy ceiling.
8. Mọi remediation từ log giữ `PENDING_APPROVAL` cho tới khi playbook vượt đủ
   gate riêng của Autonomous Operations.

## 3. Luồng dữ liệu chuẩn

```text
Alloy/Promtail
  -> Loki stream có label chuẩn
  -> Log window có watermark và coverage
  -> Redaction + normalize + fingerprint
  -> Gom event nhiều dòng và triage anomaly
  -> Server xác định daemon/entity/fault family
  -> AI RCA trên evidence đóng băng
  -> LogFinding
  -> Correlate Incident/metric/disk/network
  -> Diagnosis/Action/RemediationCase
  -> Post-check và outcome 1h/24h/7d
  -> Learning sample đủ điều kiện
  -> Statistics + retrieval index + shadow report
```

Mọi bước phải lưu `source`, timestamp, version thuật toán và ID của evidence
đầu vào để có thể tái lập kết quả.

## 4. Chuẩn hóa dữ liệu Loki

### 4.1 Label bắt buộc

Mỗi stream phục vụ học phải có tối thiểu:

| Label | Ví dụ | Quy tắc |
|---|---|---|
| `cluster` | `CS-LAB` | map tất định sang `clusters.id` |
| `host` | `ceph-osd-01` | hostname canonical, không dùng alias tự do |
| `service` | `ceph-osd` | enum catalogue |
| `daemon_type` | `osd` | `mon/mgr/osd/mds/rgw/...` |
| `daemon_id` | `12` | nullable nếu nguồn không xác định được |
| `source` | `journald` | `journald/file/container/kernel` |
| `environment` | `lab` | lấy từ inventory, không tin label bên ngoài |

Không đưa label có cardinality cao như request ID, client ID, image hoặc raw
message vào Loki labels. Chúng nằm trong structured payload và chỉ được trích
ra sau redaction.

### 4.2 Watermark và coverage

- Mỗi lần quét dùng `[window_start, window_end)` và lưu watermark thành công.
- Có overlap nhỏ để chống hụt log; dedupe bằng stream + timestamp + fingerprint.
- Ghi coverage theo node/daemon. Thiếu một nguồn thì run là `PARTIAL`, confidence
  tối đa `MEDIUM` và không tạo positive learning sample.
- Log đến trễ được nhận trong lateness window, nhưng không được sửa âm thầm
  evidence snapshot đã dùng cho một quyết định; phải tạo revision có provenance.

### 4.3 Redaction, normalize và event assembly

Thứ tự xử lý cố định:

1. Che cephx key, token, S3 signature, password, IP/tenant nhạy cảm theo policy.
2. Parse timestamp, severity, process, daemon và structured fields.
3. Thay UUID, PID, counter, offset, address và request ID bằng placeholder.
4. Giữ các định danh vận hành cần thiết như OSD ID, PG, pool/image dưới dạng
   entity đã kiểm tra quyền và redaction.
5. Tính `pattern_fingerprint` trên normalized template + parser version.
6. Ghép stack trace/multiline và các dòng liên quan trong một event window ngắn.

Fingerprint phải version hóa. Đổi normalizer không được ghi đè fingerprint cũ;
chạy reconciliation để liên kết version cũ/mới và kiểm tra collision.

## 5. Semantic identity và correlation

Identity tối thiểu của một finding:

```text
cluster_id + fault_family + entity_key + daemon_type + semantic_version
```

`entity_key` ưu tiên định danh hẹp nhất đã xác minh: `osd:12`, `pg:1.a3`,
`host:ceph-osd-01`, `pool:rbd`, `rgw:zone/name`. Không xác định được entity thì
giữ `unknown`; không được đoán target để sinh action.

Catalogue phía server ánh xạ pattern/signal sang fault family. AI chỉ hỗ trợ
phân tích khi nhiều family còn khả dĩ và phải trả về candidate enum + evidence
ID. Correlation dùng các điều kiện:

- cùng cluster và cửa sổ thời gian;
- entity khớp khi cả hai phía có entity;
- fault family tương thích theo catalogue;
- metric/health/disk signal không mâu thuẫn;
- correlation score và các thành phần score được lưu để audit.

Không được dùng “gần nhau về thời gian” làm bằng chứng duy nhất của root cause.

## 6. State machine của mẫu học

```text
CANDIDATE
  -> INSUFFICIENT_EVIDENCE | CORRELATED
CORRELATED
  -> DIAGNOSED | FALSE_POSITIVE | EXPIRED
DIAGNOSED
  -> PROPOSED | OBSERVATION_ONLY
PROPOSED
  -> REJECTED | EXECUTED_PENDING_VERIFY
EXECUTED_PENDING_VERIFY
  -> VERIFIED_SUCCESS | VERIFIED_FAILED | INCONCLUSIVE
VERIFIED_SUCCESS
  -> REGRESSED (nếu lỗi tái diễn trong cửa sổ theo dõi)
```

Chỉ `VERIFIED_SUCCESS` chưa regression và không có verdict xấu mới là positive
sample. `VERIFIED_FAILED`, `FALSE_POSITIVE`, `UNSAFE`, `INEFFECTIVE` và
regression là negative sample. `PROPOSED`, `REJECTED`, `INCONCLUSIVE`, partial
coverage và legacy/unverified chỉ dùng cho audit, không huấn luyện trust.

## 7. Mô hình dữ liệu cần bổ sung

Ưu tiên mở rộng các bảng hiện có thay vì tạo một pipeline song song.

### 7.1 `log_learning_samples`

Một hàng là snapshot bất biến của một quyết định học:

- ID và FK: `cluster_id`, `log_finding_id`, `incident_id`,
  `remediation_case_id`, `action_id`;
- identity: `daemon_type`, `daemon_id`, `host`, `fault_family`, `entity_key`;
- evidence: danh sách pattern ID/fingerprint, Loki query hash, window,
  coverage, redacted excerpt hash;
- model provenance: parser/catalogue/prompt/model/retrieval version;
- diagnosis candidate, confidence, classification và playbook snapshot;
- label: state ở mục 6, outcome source, verifier version, timestamps;
- operator verdict/note reference, regression 1h/24h/7d;
- `eligible_for_learning`, `exclusion_reason` được evaluator phía server ghi.

Không lưu raw Loki payload hoặc secret. Row sau khi đóng băng không update nội
dung evidence; thay đổi kết luận tạo revision/event mới.

### 7.2 `log_fault_stats`

Aggregate idempotent theo:

```text
cluster/environment + daemon_type + fault_family + playbook/version
+ Ceph major + deployment mode
```

Lưu proposed/executed/verified/success/failure/inconclusive, false-positive,
regression, Wilson lower bound, precision, recall khi đo được, last failure,
sample window và lý do bị chặn. Aggregate không tự nâng maturity.

### 7.3 Provenance và audit

Mọi liên kết, operator verdict, thay label, exclusion, recompute và promotion
proposal phải có append-only event/audit. Migration cần index cho identity,
time window, eligibility và FK; có unique key chống tạo hai sample từ cùng
finding revision + remediation case.

## 8. Tạo nhãn và xác minh outcome

### 8.1 Ground truth ưu tiên

1. Post-check tất định của playbook và telemetry mới sau action.
2. Health/metric/disk signal tương ứng biến mất hoặc còn tồn tại.
3. Không tái diễn trong các cửa sổ 1h, 24h và 7d.
4. Operator verdict có tài khoản, thời gian, note và audit.
5. AI self-assessment không bao giờ là ground truth.

Exit code SSH bằng 0 chỉ đưa case vào `EXECUTED_PENDING_VERIFY`.

### 8.2 Xử lý mâu thuẫn

- Telemetry báo thất bại nhưng operator báo thành công: giữ failure và mở cờ
  review; không tự sửa label.
- Telemetry thành công nhưng operator báo unsafe/false-positive: tính negative.
- Loki mất coverage sau action: `INCONCLUSIVE`.
- Lỗi tái diễn đúng identity trong cửa sổ: ghi regression và tính negative.
- Nhiều action tác động cùng entity trong verification window: inconclusive
  trừ khi verifier chứng minh được attribution.

## 9. Cách dùng dữ liệu đã học

Khi có finding mới, retrieval chỉ lấy tối đa một số nhỏ case:

- cùng cluster hoặc cùng environment đã được phép chia sẻ;
- cùng fault family, daemon/entity compatible, Ceph major và deployment mode;
- chỉ `VERIFIED_SUCCESS`, không regression/verdict xấu, evidence còn hợp lệ;
- xếp hạng tất định theo identity match, recency và trust; similarity văn bản
  chỉ là tie-breaker;
- prompt nhận summary redacted cùng Case ID, không nhận raw history.

Output phải hiển thị “case tương tự”, vì sao match, outcome và độ cũ. Case match
không được vượt policy/preflight/autonomy gate.

## 10. Trust và điều kiện promotion

Tính riêng theo scope ở mục 7.2 bằng Wilson lower bound, không dùng confidence
do model tự khai báo. Ngưỡng ban đầu đề xuất:

- ít nhất 20 verified outcomes cho shadow decision;
- cửa sổ Shadow tối thiểu 14 ngày;
- precision tối thiểu 95%, không unsafe miss;
- ít nhất 10 verified case và 5 success liên tiếp cho promotion candidate;
- success rate 30 ngày tối thiểu 98%; không severe/unsafe verdict gần đây;
- target deterministic, playbook contract cho phép L3 và đủ pre/post-check.

Đạt ngưỡng chỉ tạo `PROMOTION_CANDIDATE`; admin vẫn phải review. Dữ liệu lab
không tự cấp quyền production. Threshold phải nằm trong cấu hình server có
audit, không để model thay đổi.

## 11. Dashboard và báo cáo vận hành

Trang Log Intelligence cần bổ sung:

- learning state, daemon/entity/fault family và provenance của mỗi finding;
- nút verdict: đúng, false positive, sai nguyên nhân, ineffective, unsafe;
- liên kết Incident → Action → Case → verifier outcome;
- lý do mẫu bị loại khỏi learning.

Trang Settings/Autonomy cần báo cáo theo 7/28 ngày:

- coverage Loki và số run `PARTIAL/FAILED`;
- candidate/correlated/verified/inconclusive;
- precision, false-positive, regression và unsafe miss;
- top fault family/daemon, trust score và sample count;
- shadow `HOLD/WOULD_EXECUTE` so với outcome;
- promotion blocker bằng câu giải thích tất định.

## 12. Kế hoạch triển khai theo pha

### Pha 0 — Baseline và data contract

- Chốt label contract, catalogue daemon/fault family và quyền Loki tenant.
- Đo 7 ngày: coverage, log volume, cardinality, pattern/finding rate.
- Gắn BENIGN cho nguồn nhiễu; xác nhận redaction bằng test fixture production đã
  khử bí mật.

**Gate:** coverage >= 99%, không secret trong sample, không cardinality runaway.

### Pha 1 — Learning sample audit-only

- Thêm schema/migration, writer idempotent và provenance.
- Backfill chỉ tạo `legacy/unverified`, tuyệt đối không cấp trust.
- Hiển thị state/exclusion reason; chưa đưa sample vào prompt.

**Gate:** mọi sample truy ngược được Loki window → Finding → Incident/Case.

### Pha 2 — Correlation và outcome evaluator

- Mở catalogue theo từng daemon, bắt đầu với OSD và MON.
- Correlate health/metric/disk signal; verifier 1h/24h/7d.
- Thêm operator verdict và conflict handling.

**Gate:** bộ test replay không merge sai entity; partial/stale luôn fail closed.

### Pha 3 — Retrieval có giám sát

- Dùng verified case để bổ sung context RCA; chạy A/B hoặc shadow.
- So sánh precision, citation validity, latency và token cost với baseline.
- Có kill switch để quay về RCA không retrieval.

**Gate:** precision không giảm, citation 100% trỏ tới evidence tồn tại, không
rò dữ liệu chéo cluster.

### Pha 4 — Shadow remediation

- Ghi `HOLD/WOULD_EXECUTE`, không gọi executor.
- Chạy tối thiểu 14–28 ngày và đủ sample cho từng scope.
- Review unsafe miss, missed opportunity, false-positive và regression hàng tuần.

**Gate:** đạt mục 10 và không có unsafe miss.

### Pha 5 — L3 lab rồi canary production

- Chỉ SAFE playbook có target deterministic và blast radius thấp.
- Lab: grace period, cancel Telegram, rate limit, cooldown, lease, rollback và
  chaos test.
- Production: một cluster/playbook canary, budget rất nhỏ, auto-demotion và
  global/per-cluster kill switch.

**Gate:** admin phê duyệt riêng từng scope; production không kế thừa quyền lab.

## 13. Kiểm thử bắt buộc

- Unit: redaction, normalization, multiline, fingerprint version/collision,
  daemon/entity parser, eligibility và Wilson score.
- Integration: Loki pagination/watermark/late log, partial coverage, dedupe,
  correlation và transaction rollback.
- Replay: incident thật đã redaction, gồm cùng pattern khác entity và khác
  pattern cùng fault family.
- Security: prompt injection trong log, secret leakage, tenant/cluster isolation,
  forged labels và model trả action ngoài allowlist.
- Safety: stale evidence, service restart, duplicate delivery, concurrent action,
  post-check timeout, regression và kill switch.
- Migration: upgrade/downgrade trên SQLite test và PostgreSQL staging; backfill
  không làm tăng trust.

Các nhóm test hiện có phải tiếp tục xanh: `test_log_analysis`,
`test_log_intelligence_e2e`, `test_remediation_cases`, `test_trust_engine`,
`test_playbook_registry` và `test_router_client`.

## 14. Runbook rollout và rollback

Trước mỗi pha:

1. Backup database và ghi Alembic revision/commit đang chạy.
2. Deploy staging, chạy migration và replay test.
3. Bật feature flag ở audit/shadow mode cho một cluster.
4. Theo dõi coverage, error rate, DB growth, Loki latency và AI cost.
5. Mở rộng từng daemon; không bật đồng loạt.

Feature flags nên tách riêng: ingest, sample writer, correlation, retrieval,
shadow và execution. Rollback theo thứ tự ngược: tắt execution → shadow →
retrieval → correlation; giữ ingest và dữ liệu audit. Không downgrade schema
khi service cũ vẫn có thể ghi vào bảng mới.

## 15. Truy vấn nghiệm thu mẫu

```sql
-- Số mẫu đủ điều kiện và outcome theo daemon/fault family
SELECT daemon_type, fault_family, label, count(*)
FROM log_learning_samples
GROUP BY daemon_type, fault_family, label
ORDER BY daemon_type, fault_family, label;

-- Mẫu bị loại phải luôn có lý do
SELECT count(*)
FROM log_learning_samples
WHERE eligible_for_learning = false
  AND (exclusion_reason IS NULL OR exclusion_reason = '');

-- Scope có thể review promotion
SELECT daemon_type, fault_family, playbook_id, playbook_version,
       verified_count, trust_score, promotion_candidate_at,
       promotion_blocked_reason
FROM log_fault_stats
ORDER BY verified_count DESC;
```

Truy vấn thứ hai phải trả về `0`. Promotion candidate không đồng nghĩa đã được
nâng quyền; cần đối chiếu audit của admin và playbook maturity.

## 16. Definition of Done

Tính năng chỉ được coi là hoàn thành khi:

1. Mỗi kết luận học truy nguyên được tới Loki window và evidence bất biến.
2. Không positive sample nào thiếu telemetry verification.
3. Partial/stale/corrupt/legacy data không tăng trust.
4. Retrieval không làm giảm precision và không rò dữ liệu chéo cluster.
5. Shadow đủ thời gian/mẫu, không unsafe miss và có báo cáo tái lập được.
6. Không model output nào thay đổi policy hoặc tự cấp quyền.
7. Có kill switch, rollback, audit, retention và dashboard vận hành.
8. L3 chỉ được bật theo từng playbook/scope sau admin approval.
