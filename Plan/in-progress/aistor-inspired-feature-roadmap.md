# AIStor-inspired Feature Roadmap cho Ceph-AI

## 0. Mục tiêu và ranh giới

Tài liệu này mô tả các tính năng Ceph-AI có thể học từ hệ sinh thái MinIO
AIStor mà không sao chép mã nguồn proprietary hoặc phụ thuộc vào AIStor Server.

### Kết luận nghiên cứu nguồn

- minio/minio là mã nguồn MinIO Community AGPLv3 để tham khảo S3 behavior và
  một số ý tưởng storage; repository đã được archive và README ghi rõ không còn
  được duy trì.
- AIStor Server được MinIO phân phối qua binary, RPM/DEB, container và Helm,
  kèm enterprise license. Không coi binary, container hoặc package là source
  dependency của Ceph-AI.
- madmin-go cho thấy một admin API có phạm vi rộng: health, metrics, audit và
  error log, capacity forecast, bucket/user/policy, replication, batch job,
  healing, KMS và drive diagnostics.
- mcp-server-aistor cho thấy pattern AI tool an toàn: read-only mặc định,
  write/delete/admin bật riêng, giới hạn số item gửi vào model và dùng endpoint
  S3 qua credential được cấu hình. Repository này đã archive, vì vậy chỉ dùng
  làm reference.
- ansible-aistor và minio-aistor-gcp chỉ là deployment/packaging reference,
  không chứa server implementation.
- warp là reference cho benchmark S3 có latency, throughput, concurrency và
  kết quả có thể lưu lại để so sánh.

### Nguyên tắc bắt buộc

- Không reverse-engineer, decompile, vendor hoặc copy code AIStor proprietary.
- Ceph-AI tiếp tục dùng ceph, radosgw-admin, S3 API, Prometheus/Loki và
  Worker hiện tại làm backend.
- Read-only là mặc định. Mọi write/delete/admin phải đi qua capability,
  RBAC, preview, approval, Worker execution, post-check và audit.
- Mọi dữ liệu phải gắn cluster_id; không fallback âm thầm về cluster mặc định.
- Secret không được vào HTML, log, prompt, audit payload hoặc response không
  được bảo vệ.
- Dữ liệu stale được dùng cho hiển thị/diagnosis có gắn nhãn, nhưng không được
  dùng để quyết định thao tác phá hủy.
- Feature mới phải có flag riêng và fail-closed khi capability hoặc evidence
  chưa đủ.

## 1. Mapping nguồn tham khảo với Ceph-AI hiện tại

| Pattern học được | Nguồn tham khảo | Điểm nối trong Ceph-AI | Quyết định |
|---|---|---|---|
| Admin health/metrics/evidence | madmin-go health, metrics, logs, audit | watcher/ceph_client.py, dashboard/routes, shared/models.py | Tạo provider-neutral evidence contract |
| AI tool permission boundary | mcp-server-aistor read/write/delete/admin flags | dashboard/ceph_tools.py, dashboard/chat_client.py, policy gate | Bỏ raw command mặc định; tool registry đóng |
| Bounded result/context | mcp-server-aistor giới hạn object/bucket gửi model | Chat AI và log/RGW evidence collector | Pagination, max rows, max bytes, truncation metadata |
| Async operation/status | madmin-go batch job, heal, update, decommission | Action, Worker executor, audit | Chuẩn hóa StorageJob trên Action hiện có nếu đủ |
| Bucket governance | quota, policy, lifecycle, object lock, tags | dashboard/routes/object_storage.py | Mở rộng capability matrix |
| Replication/remote target | replication API, bucket targets | Object Storage roadmap, backup roadmap | Tách replication state khỏi backup policy |
| Deployment/release | ansible-aistor, GCP chart/deployer | deploy scripts, release gate | Học manifest/checksum/health verification |
| Performance proof | warp | volume/object benchmark và AI performance pages | Lưu benchmark run + reproducible profile |

## 2. Pha 0 — Research baseline, legal và contract freeze [x] nghiên cứu

- [x] Xác định AIStor Server core không phải public source dependency.
- [x] Liệt kê repo public theo ba nhóm: source/reference, integration và
  deployment/tooling.
- [x] Đối chiếu với route, Worker, capability matrix, audit và cache hiện có.
- [ ] Lưu snapshot nghiên cứu gồm repository URL, commit/tag, thời điểm đọc,
  license/status và feature được rút ra.
- [ ] Tạo docs/aistor-compatibility-boundary.md ghi rõ API nào là S3 chuẩn,
  API nào là Ceph RGW riêng và API nào chỉ là AIStor optional.

DoD: reviewer phân biệt được phần học về kiến trúc với phần không được sao chép;
không có AIStor binary/source trong repository Ceph-AI.

## 3. Pha 1 — Storage provider và capability contract [P0]

### 3.1 Provider-neutral contract

- [ ] Tạo contract cho health, capacity, bucket_inventory, object_metadata,
  policy, lifecycle, replication, audit, metrics và async_operation.
- [ ] Chuẩn hóa response gồm cluster_id, provider, backend, source,
  collected_at, fresh_until, partial, warnings và evidence_id.
- [ ] Phân biệt SUPPORTED, UNSUPPORTED, UNKNOWN, STALE và ERROR.
- [ ] Field không có bằng chứng trả unknown/null, không đoán từ UI.

### 3.2 Capability inventory

- [ ] Mở rộng shared/capability_matrix.py từ command-level sang feature,
  operation, backend, Ceph major, minimum permission và documentation source.
- [ ] Cache capability theo cluster với TTL; invalidate sau upgrade/config change.
- [ ] UI hiển thị lý do disabled và phiên bản/capability source.
- [ ] Contract test cho Ceph 14–20, mixed version và unknown version.

### 3.3 Data freshness

- [ ] Tách fresh read, stale read và snapshot read thành ba mode rõ ràng.
- [ ] Dashboard dùng bounded cache; preview/mutation bắt buộc fresh recheck.
- [ ] Gắn age và source lên mọi card/diagnostic result.

File dự kiến: shared/capability_matrix.py, module provider mới dưới
shared/storage/, shared/object_storage_cache.py và dashboard/routes/.

Acceptance: cùng một API trả kết quả cho RGW; thiếu capability thì UI khóa đúng
thao tác và Worker cũng từ chối khi gọi trực tiếp.

## 4. Pha 2 — Object Storage evidence plane [P0]

Học từ các nhóm health, metrics, audit/error log và capacity trong madmin-go,
nhưng collector lấy dữ liệu từ Ceph/RGW.

### 4.1 Health và capacity

- [ ] RGW service health: daemon, endpoint, TLS, latency, error rate,
  zone/zonegroup.
- [ ] Cluster/object capacity: logical bytes, physical bytes nếu có, free,
  near-full, quota, object count và growth rate.
- [ ] Per-bucket/per-user top consumers và stale/partial indicator.
- [ ] Evidence snapshot bounded theo cluster, host, daemon, bucket và user.

### 4.2 Metrics và logs

- [ ] Chuẩn hóa request_total, bytes_in/out, 4xx, 5xx, latency p50/p95/p99,
  access denied và throttling.
- [ ] Correlate RGW access log, Ceph health, Loki evidence và dashboard timeline
  theo timestamp window; không gửi toàn bộ log vào LLM.
- [ ] Lưu fingerprint/summary thay vì raw secret-bearing request.

### 4.3 Audit và export

- [ ] Audit read-sensitive và mutation: actor, cluster, target, preview hash,
  approval, request ID, result, post-check.
- [ ] Export CSV/JSON bounded, redacted và có filter thời gian/cluster.

Acceptance: bucket detail trả metadata, health, metric trend, log evidence và
audit link mà không gọi vô hạn tới RGW hoặc làm lộ credential.

## 5. Pha 3 — AI Storage Tool Gateway [P0]

Đây là phần có giá trị nhất để học từ mcp-server-aistor, nhưng tích hợp vào
Chat AI hiện có thay vì chạy MCP server ngoài thiếu cluster scope.

### 5.1 Tool registry đóng

- [ ] Định nghĩa tool IDs: list_buckets, get_bucket_metadata, list_objects,
  get_object_metadata, get_rgw_health, get_storage_metrics, get_audit_events,
  get_replication_status và get_capacity_forecast.
- [ ] Mỗi tool khai báo input schema, read/write class, required capability,
  maximum rows/bytes, timeout, evidence type và allowed cluster scope.
- [ ] Không cho model tự tạo radosgw-admin, ceph hoặc shell command tùy ý.
- [ ] Giữ fixed Ceph tools; chuyển raw run_ceph_command sang explicit
  operator-only diagnostic mode với allowlist parser chặt hơn.

### 5.2 Permission modes

- [ ] READ_ONLY mặc định.
- [ ] PROPOSE_WRITE chỉ tạo preview/action, chưa mutation.
- [ ] APPROVED_WRITE chỉ Worker chạy action đã approve.
- [ ] DELETE_ADMIN tách riêng, yêu cầu role, confirmation và policy gate.
- [ ] Tool response trả truncated, next_cursor, max_items và evidence_id.

### 5.3 Context budget và injection safety

- [ ] Giới hạn theo rows, bytes, characters và token estimate.
- [ ] Summarize trước khi đưa vào model; giữ raw evidence ngoài prompt.
- [ ] Coi object metadata, bucket name, log và tags là untrusted data.
- [ ] Cache read-only tool theo cluster + tool + args hash.

### 5.4 Tool observability

- [ ] Log tool ID, scope, latency, row count, truncation và outcome; không log
  secret hoặc full object content.
- [ ] Rate limit theo user/cluster/tool và circuit breaker khi RGW chậm.

Acceptance: Chat AI hỏi được RGW thật ở read-only mode; ý định ghi/xóa luôn tạo
preview/action và không thể chạy bằng prompt đơn độc.

## 6. Pha 4 — Async Storage Jobs và operation lifecycle [P0]

Học pattern batch job/status/progress/heal/update từ madmin-go và áp dụng vào
RGW/Ceph operations.

- [ ] Chuẩn hóa state: PLANNED, PENDING_APPROVAL, QUEUED, RUNNING, PAUSING,
  PAUSED, SUCCEEDED, FAILED, CANCELLED và INCONCLUSIVE.
- [ ] Mỗi job có job_id, cluster_id, operation, target, actor, idempotency key,
  input hash, preview hash, progress, current step, retry count và timestamps.
- [ ] Endpoint status/poll và event stream bounded; UI không reload toàn trang.
- [ ] Retry chỉ khi operation idempotent hoặc có reconciliation evidence.
- [ ] Cancel chỉ dừng ở checkpoint an toàn; không báo cancelled khi chưa xác nhận.
- [ ] Reconciliation sau timeout/restart: success, failed hoặc inconclusive.
- [ ] Dùng lại Action/audit nếu đủ; chỉ tạo StorageJob riêng khi cần progress
  hoặc event chain riêng.

Ưu tiên operation: object delete/version purge, lifecycle update, bucket policy,
replication sync, benchmark và bulk inventory export.

Acceptance: restart Worker không tạo mutation trùng; timeout không retry mù; UI
hiển thị state cuối cùng có evidence.

## 7. Pha 5 — Governance, IAM và security insight [P1]

- [ ] Policy analyzer: public principal, wildcard action/resource, policy nguy
  hiểm, unused permission và conflict user/group/policy.
- [ ] S3 user/access-key inventory: tuổi key, last-used nếu có, rotation
  reminder, disabled/revoked state; không hiển thị secret.
- [ ] Access denied spike, anonymous access, unusual source IP và object
  download/upload anomaly dạng recommendation-only.
- [ ] KMS/encryption posture: bucket encryption, TLS endpoint, key age/status.
- [ ] Policy diff trước/sau; public access hoặc bulk permission change cần
  confirmation mạnh và approval hai bước.
- [ ] Security finding có severity, evidence, first_seen, last_seen, suppress
  expiry và owner; không tự biến finding thành remediation.

Acceptance: security overview có evidence và audit link; không tự disable/delete/
rotate key.

## 8. Pha 6 — Replication, multi-site và data protection [P1]

Map remote target/replication của AIStor sang RGW multisite và tách khỏi RBD
backup policy.

- [ ] Mô hình realm → zonegroup → zone → endpoint theo cluster scope.
- [ ] Inventory peer/remote target với endpoint redaction, TLS state và auth status.
- [ ] Replication lag: last sync, pending/failed objects, bytes behind, RPO và
  confidence.
- [ ] Copy compliance: expected replicas vs observed replicas theo bucket/prefix.
- [ ] Digest theo thời gian: object count, bytes, checksum/sample verification,
  missing/extra/conflict.
- [ ] Failover/failback chỉ tạo plan và approval.
- [ ] Fencing/preflight: peer health, clock skew, DNS/TLS, writes in flight,
  split-brain indicators và rollback plan.
- [ ] Restore drill read-only hoặc scratch target; lưu RTO/RPO evidence.

Acceptance: dashboard phân biệt healthy, lagging, degraded, unknown và
split-brain-risk; ping endpoint không đủ để kết luận replication healthy.

## 9. Pha 7 — AI insight và forecasting cho Object Storage [P1]

- [ ] Forecast dung lượng theo cluster + zone + bucket + user và horizon
  1h/6h/24h riêng; không suy ra horizon dài từ model ngắn.
- [ ] Forecast request/bytes/error/latency; hiển thị interval, data quality,
  missing/gap rate, model version và verified outcome.
- [ ] Detect anomaly: 5xx spike, access denied spike, latency regression,
  replication lag, bucket growth burst và small-object explosion.
- [ ] RCA evidence chain: metric → RGW daemon → host → bucket/user → log window.
- [ ] Recommendation-only: cache policy, lifecycle, quota, prefix, endpoint
  scaling và replication repair.
- [ ] Feedback loop tách operator verdict khỏi telemetry truth; chỉ sample có
  verified outcome mới được học.

Acceptance: insight có evidence fingerprint, confidence, freshness, model/version
và recommendation; replay/shadow không tạo Incident, Telegram hoặc remediation.

## 10. Pha 8 — Benchmark và performance regression [P1]

Học từ warp, không gắn benchmark vào production mutation path.

- [ ] Benchmark profile: GET/PUT/HEAD/LIST/MULTIPART/DELETE, object size,
  concurrency, duration, prefix và TLS.
- [ ] Chạy trên scratch bucket hoặc target được xác nhận; cleanup có inventory.
- [ ] Lưu p50/p95/p99 latency, throughput, error rate, object rate, CPU/RSS,
  network và Ceph health trong cùng run.
- [ ] So sánh baseline/candidate/endpoint; không so sánh khác profile.
- [ ] Redact credential, bucket name nhạy cảm và endpoint trong report.
- [ ] Regression gate cho latency/error/throughput với ngưỡng operator-set.

Acceptance: run tái lập được và truy nguyên theo commit, cluster, endpoint, config
hash và tool version.

## 11. Pha 9 — Deployment, upgrade và release evidence [P1]

Học từ ansible-aistor, GCP deployer và package conventions cho Ceph-AI services.

- [ ] Release manifest: commit SHA, migration head, lockfile, image digest,
  config checksum và feature flags.
- [ ] Preflight target: disk/RAM/port/TLS/secret permission/connectivity.
- [ ] Upgrade từng service với health check, migration backup, rollback checkpoint.
- [ ] Tách systemd/container mode; restart đúng runtime unit/container.
- [ ] Artifact deploy gồm stdout/stderr redact, status từng phase, target host,
  version thực tế sau deploy.
- [ ] Smoke test: bucket list, metadata, audit, AI read-only tool và mutation
  bị chặn khi chưa approval.

## 12. Pha 10 — Test strategy và release gates

### Test layers

- [ ] Unit: schema, capability, policy, truncation, redaction, freshness,
  idempotency và state transition.
- [ ] Contract: Ceph/RGW version, provider response, unsupported capability,
  S3 error mapping và partial evidence.
- [ ] Security: RBAC, CSRF, cluster isolation, prompt injection, command
  injection, secret leakage, public policy và bulk delete.
- [ ] Worker: retry/resume/cancel/reconcile, crash/restart, duplicate request,
  stale preview và in-flight mutation.
- [ ] E2E: dashboard → preview → approval → Worker → Ceph/RGW → post-check →
  audit → UI status.
- [ ] Live smoke trên cluster test; production chỉ read-only cho đến khi có
  operator approval và rollback evidence.

### Release gates

- [ ] Không còn P0 destructive-flow blocker.
- [ ] Read-only AI tools hoạt động với bounded result/context.
- [ ] Không có raw shell tool mặc định.
- [ ] Capability unknown/unsupported đều fail-closed.
- [ ] 100% mutation có target cluster, preview hash, approval và audit.
- [ ] Restart không làm mất job/evidence/model state.
- [ ] Benchmark và deploy artifact truy nguyên được theo commit SHA.
- [ ] Shadow/replay không tạo Incident, Action, Telegram hoặc remediation.

## 13. Thứ tự triển khai đề xuất

1. P0.1: capability/evidence contract và freshness metadata.
2. P0.2: bounded read-only Storage Tool Gateway cho Chat AI.
3. P0.3: async StorageJob/reconciliation dùng chung với Action.
4. P0.4: RGW health/metrics/audit/capacity overview.
5. P1.1: governance/security insight.
6. P1.2: replication/multi-site/RPO-RTO.
7. P1.3: Object Storage forecasting/RCA/recommendation.
8. P1.4: benchmark regression.
9. P1.5: release evidence và upgrade safety.

Không bắt đầu replication failover, public policy automation hoặc AI write tool
trước khi P0.1–P0.3 đạt đầy đủ test và operator approval boundary.

## 14. Tiêu chí tổng thể hoàn thành

- [ ] Có provider/capability contract được version hóa.
- [ ] Dashboard Object Storage hiển thị health, capacity, metrics, audit và
  replication evidence có freshness.
- [ ] Chat AI chỉ gọi tool registry đã allowlist và giới hạn context.
- [ ] Mọi operation dài có job ID, progress, retry/cancel/reconcile.
- [ ] Governance/replication/forecast có evidence và không suy đoán khi thiếu dữ liệu.
- [ ] Có benchmark, deploy manifest, smoke test và rollback evidence.
- [ ] Cập nhật các mục implementation vào Plan/object-storage-roadmap.md,
  Plan/backup-roadmap.md và Plan/production-readiness-plan.md; không đánh dấu
  [x] chỉ vì UI đã xuất hiện.

## 15. Tài liệu/repo tham khảo

- https://www.min.io/product/aistor
- https://www.min.io/download/aistor-server
- https://github.com/minio/minio
- https://github.com/minio/madmin-go
- https://github.com/minio/mcp-server-aistor
- https://github.com/minio/ansible-aistor
- https://github.com/minio/minio-aistor-gcp
- https://github.com/minio/pkger
- https://github.com/minio/warp
- https://github.com/minio/directpv

