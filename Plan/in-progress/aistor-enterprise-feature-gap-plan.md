# AIStor Enterprise Feature Gap Plan — Ceph-AI

- Ngày rà soát: 2026-09-24
- Phạm vi: dashboard, worker, watcher, shared, config và toàn bộ thư mục Plan
- Mục tiêu: đối chiếu các năng lực nên học từ AIStor với những gì Ceph-AI thực sự đã có; các mục chưa hoàn thiện được đưa vào kế hoạch triển khai có thứ tự ưu tiên.
- Nguyên tắc: không đánh dấu hoàn thành chỉ vì đã có UI, policy action name hoặc diagnosis; một tính năng chỉ hoàn thành khi có backend action, trạng thái bất đồng bộ, audit/evidence, validation, test và rollback an toàn.

## 1. Kết quả audit

| Hạng mục | Ưu tiên | Trạng thái hiện tại | Kết luận |
|---|---:|---|---|
| Federated IAM cho RGW | P0 | Đang triển khai vertical slice | Provider/role mapping registry và Worker adapter đã có; live verification, STS và policy simulator còn thiếu |
| RGW Multisite Operations | P0 | Một phần rất nhỏ | Mới có diagnosis/roadmap và placement; chưa có thao tác vận hành |
| KMS/SSE Management | P0 | Chưa có | Policy action có tên liên quan nhưng chưa có quản trị SSE/KMS/Vault |
| Hoàn thiện Object Lock | P1 | Một phần | Có đọc trạng thái và default retention; thiếu mutation theo object/version và audit WORM |
| S3 Event Notification | P1 | Chưa có | Chưa có quản trị notification tới Kafka/RabbitMQ/NATS/MQTT/webhook |
| Bucket QoS, compression và inventory job | P1 | Một phần | Có inventory tức thời, stats, filter và lifecycle; thiếu job lịch sử, QoS và phân tích nâng cao |
| Kubernetes/Helm deployment | P1 | Chưa có chart sản phẩm | Có execution mode và systemd/container runtime, nhưng chưa có Helm deployment cho Ceph-AI |
| AI Data Catalog | P2 | Chưa có | Chưa có catalog dataset/model/checkpoint/lineage |
| Iceberg integration | P2 | Chưa có | Chưa có Polaris/Nessie/REST Catalog integration |

## 2. Những gì code hiện tại đã có

### 2.1 Object Storage đã có nhưng không đủ cho các yêu cầu mới

- Có inventory/listing và phân trang object/bucket theo request hoặc cache.
- Có bucket statistics, lifecycle editor và một số thao tác user/access key.
- Có tạo bucket với Object Lock và cấu hình default retention ở mức bucket.
- Object Browser đọc được retention và legal hold hiện tại.
- Có các policy action liên quan đến Object Lock như GetObjectRetention, PutObjectRetention, GetObjectLegalHold và PutObjectLegalHold, nhưng việc có tên action không đồng nghĩa với việc UI/API/worker đã triển khai.
- Có bucket logging và pipeline cảnh báo nội bộ; đây không phải S3 Event Notification tới các broker bên ngoài.
- Chưa có backend operation cho RGW realm/zonegroup/zone, peer, resync, failover/failback hoặc fencing.
- Đã có provider registry OIDC/LDAP/AD và role/policy mapping registry; STS, live directory bind và RGW verification vẫn đang triển khai.
- Chưa có KMS/Vault/SSE configuration, key accessibility probe, rotation workflow hoặc encryption evidence.
- Chưa có Helm chart sản phẩm với probes, PDB, NetworkPolicy, external PostgreSQL/RabbitMQ và secrets integration.
- Chưa có catalog cho dataset/model/checkpoint hoặc Iceberg REST Catalog.

### 2.2 Các Plan hiện tại không được coi là đã hoàn thành

Các mục multisite trong Plan/object-storage-roadmap.md vẫn còn là roadmap/checkbox chưa hoàn thiện. Các mục Object Lock hiện mới tập trung vào create bucket, default retention và read-only inspection. File mới này là gap plan bổ sung, không thay thế và không tự động đánh dấu hoàn thành cho các Plan cũ.

## 3. P0 — Federated IAM cho RGW

### Mục tiêu

Quản lý danh tính bên ngoài và quyền truy cập RGW theo mô hình enterprise, không buộc admin phải tạo toàn bộ user local trong Ceph-AI.

### Phạm vi triển khai

- [~] Provider registry cho OIDC, LDAP và Active Directory.
- [x] Secret reference an toàn, không lưu client secret/password dạng plaintext trong config hoặc audit log (`env:`, allowlisted `file:` và Vault KV `vault:path#field`; Vault dùng HTTPS và fail-closed).
- [ ] OIDC discovery/JWKS validation, issuer/audience/clock-skew validation và certificate trust configuration.
- [~] LDAP/AD connection test, bind test, group lookup và mapping user/group (adapter đã có; live directory/Vault resolver còn thiếu).
- [~] Role mapping từ claim/group sang RGW policy hoặc policy bundle (đã có preview/registry và Worker adapter; cần live verification, LDAP/AD adapter và approval hoàn chỉnh).
- [~] STS temporary credentials: assume role, TTL, session tags, revoke/expire và audit (đã có adapter/API/UI/registry; live RGW exchange và hard revoke còn chờ hạ tầng).
- [ ] Policy simulator: kiểm tra allow/deny theo principal, action, resource và condition.
- [ ] Phát hiện quyền dư thừa: policy không được dùng, action không cần thiết, wildcard nguy hiểm và role không có owner.
- [ ] Preview trước khi apply mapping/policy.
- [ ] Approval, idempotent worker action, audit event và rollback.
- [~] UI: Identity Providers, Role Mapping registry và Temporary Sessions đã có; Policy Simulator và Access Review còn thiếu.

### Tiến độ triển khai

- [x] 2026-09-24 — Thêm registry provider OIDC/LDAP/AD với trạng thái DRAFT, VALID, APPLIED và DISABLED.
- [x] 2026-09-24 — Lưu `secret_ref` thay vì plaintext secret; API/list/audit không trả secret hoặc client secret.
- [x] 2026-09-24 — Có preview, create, validate, apply, disable API; apply yêu cầu xác nhận chính xác tên provider.
- [x] 2026-09-24 — OIDC discovery kiểm tra issuer, audience/JWKS metadata và có audit evidence.
- [~] 2026-09-24 — LDAP/AD mới kiểm tra transport endpoint; bind/group lookup và mapping claim/group chưa hoàn thành.
- [~] 2026-09-25 — Thêm `ldap3` adapter: bind LDAP/AD, StartTLS/LDAPS certificate verification, user/group lookup, secret resolver `env:`/allowlisted `file:`/Vault KV `vault:path#field` và redaction; có negative tests. Live Vault/LDAP integration vẫn cần kiểm tra với hạ tầng thật.
- [~] 2026-09-24 — Thêm Role Mapping preview với policy SHA-256, cảnh báo wildcard, tạo DRAFT và register có confirmation; register đưa mapping vào hàng đợi Worker.
- [~] 2026-09-25 — Worker poller reconcile mapping `REGISTERED` qua `radosgw-admin` trên RGW node, tạo/update OIDC provider, role và role policy; có post-check policy, retry state và audit. Chưa chạy live vì server không có Ceph CLI/được cấu hình RGW node để thực thi thật.
- [~] 2026-09-25 — Thêm STS adapter/API/UI: preview, AssumeRoleWithWebIdentity, TTL 15 phút–12 giờ, session tags, trạng thái ACTIVE/EXPIRED/REVOKED/FAILED, audit và session registry không lưu secret. Live RGW STS exchange và hard revoke vẫn cần hạ tầng thật.
- [x] 2026-09-25 — Migration `m20260925federatediamsts` đã áp dụng trên database, tạo registry cho STS session và các index theo status/expiry/provider/mapping.
- [ ] Live LDAP/AD bind/group lookup với directory thật, Vault resolver, STS temporary credentials, policy simulator và access review.
- [ ] Approval workflow, worker async, idempotency đầy đủ và rollback cho toàn bộ IAM actions.
- [x] 2026-09-24 — Migration `m20260924federatediam` đã được áp dụng trên database của server; test vertical slice 3/3 đạt.

### Contract/backend đề xuất

- Capability: federated IAM.
- Action IDs: provider_validate, provider_apply, role_mapping_preview, role_mapping_apply, policy_simulate, access_review, sts_session_revoke.
- Mọi action phải có request id, idempotency key, approval state, evidence reference và failure reason có cấu trúc.

### Tiêu chí hoàn thành

- Có thể cấu hình tối thiểu một OIDC provider và một LDAP/AD provider qua UI/API.
- Có test connection và test group/claim mapping, không để lộ secret.
- Người dùng liên kết có thể nhận temporary credentials với TTL và bị thu hồi.
- Policy simulator trả kết quả allow/deny có lý do.
- Có audit, approval và rollback cho thay đổi provider/role/policy.
- Có test integration với provider giả lập và negative tests cho token/key hết hạn.

## 4. P0 — RGW Multisite Operations

### Mục tiêu

Biến phần diagnosis multisite thành vận hành có kiểm soát, có preview, approval, fencing và post-check.

### Phạm vi triển khai

- [ ] Inventory topology: realm, zonegroup, zone, endpoints, period, sync status và peer health.
- [ ] Tạo/sửa/xóa realm, zonegroup và zone qua worker; chặn thao tác phá vỡ topology đang hoạt động.
- [ ] Add/remove peer với credential validation và secret rotation.
- [ ] Preview thay đổi period và replication topology trước khi apply.
- [ ] Resync với dry-run, estimate, approval, progress, pause/resume/cancel và reconciliation.
- [ ] Planned failover/failback với pre-check, approval, fencing và post-check.
- [ ] Split-brain detection, peer divergence, clock skew, stale period và replication lag alert.
- [ ] RPO/RTO estimate theo zone/peer.
- [ ] Audit bằng chứng trước/sau action, snapshot config và rollback plan.
- [ ] Digest/restore drill liên kết với backup roadmap.

### Safety gates

- Không cho failover nếu pre-check chưa đạt.
- Fencing là bước bắt buộc trước failover/failback có nguy cơ ghi đồng thời.
- Mọi resync/failover/failback phải có operator approval.
- Không cho xóa zone/peer nếu còn bucket hoặc replication dependency chưa được xử lý.

### Tiêu chí hoàn thành

- Dashboard hiển thị topology và trạng thái sync thật từ cluster.
- Có dry-run/preview trước thao tác thay đổi.
- Resync và failover chạy qua worker async, có progress/retry/cancel/reconciliation.
- Có evidence post-check chứng minh replication đã ổn định.
- Có test split-brain, stale peer, partial failure, retry và rollback.

## 5. P0 — KMS/SSE Management

### Mục tiêu

Quản lý mã hóa server-side và chứng minh object thực sự được mã hóa, không chỉ hiển thị policy.

### Phạm vi triển khai

- [ ] Cấu hình SSE-S3, SSE-KMS và Vault/KMS provider.
- [ ] KMS endpoint/credential/certificate validation và secret reference.
- [ ] Key accessibility probe: list/describe/encrypt/decrypt theo quyền tối thiểu.
- [ ] Key-to-bucket mapping, default encryption policy và exception review.
- [ ] Key rotation plan, certificate rotation và expiration warning.
- [ ] Preview/apply/rollback config cho bucket encryption.
- [ ] Encryption evidence: HEAD/stat object, encryption headers, key ID, version và timestamp.
- [ ] Cảnh báo bucket/object không đạt encryption policy.
- [ ] Audit log cho key access, policy change và failed probe.
- [ ] Không hiển thị plaintext secret trong UI, log, evidence hoặc notification.

### Tiêu chí hoàn thành

- Có thể test accessibility cho từng provider/key.
- Có thể cấu hình encryption theo bucket với approval và rollback.
- Có bằng chứng object-level xác nhận encryption mode/key ID.
- Có rotation workflow và cảnh báo certificate/key sắp hết hạn.
- Có negative tests: KMS unreachable, permission denied, invalid certificate và stale key.

## 6. P1 — Hoàn thiện Object Lock và WORM audit

### Phạm vi triển khai

- [ ] Set/release Legal Hold theo object version.
- [ ] Set/extend/shorten retention theo object version với kiểm tra Governance/Compliance.
- [ ] Bypass Governance yêu cầu quyền riêng, lý do và approval.
- [ ] Policy riêng cho Governance và Compliance.
- [ ] Danh sách object sắp hết retention, đã hết retention và bị giữ vô thời hạn.
- [ ] Bulk operation an toàn, giới hạn scope, preview và rate limit.
- [ ] Evidence WORM: version ID, retention mode, retain-until date, legal hold, actor, request ID và checksum.
- [ ] Manual delete chỉ cho phép khi policy/object state hợp lệ; hiển thị rõ lý do bị chặn.
- [ ] Test restore/replication giữ nguyên retention/legal hold.

### Tiêu chí hoàn thành

- Thao tác per-object/version hoạt động qua UI/API/worker.
- Governance và Compliance có enforcement khác nhau đúng kỳ vọng.
- Mọi thay đổi có audit/evidence và không thể giả mạo bằng log client.
- Có test hết hạn retention, legal hold, delete blocked, bypass denied và replication.

## 7. P1 — S3 Event Notification

### Phạm vi triển khai

- [ ] Notification configuration theo bucket/prefix/suffix/event type.
- [ ] Target Kafka, RabbitMQ, NATS, MQTT và webhook.
- [ ] Credential/CA/endpoint validation và secret rotation.
- [ ] Preview event routing và test delivery.
- [ ] Retry/backoff, dead-letter, replay, deduplication và delivery status.
- [ ] Schema versioning, correlation ID, object version và source zone.
- [ ] Rate limit, backpressure và payload redaction.
- [ ] Audit config và evidence delivery.
- [ ] Feed cho realtime anomaly detection/AI, tách rõ raw event và derived alert.

### Tiêu chí hoàn thành

- Tạo được một route event từ bucket tới từng target được hỗ trợ.
- Có test delivery end-to-end và hiển thị delivery health.
- Có retry/resume/replay/cancel/reconciliation.
- Webhook có signature verification; broker có TLS và credential rotation.
- Không tạo Incident/Action/remediation tự động nếu chưa qua feature flag và approval.

## 8. P1 — Bucket QoS, compression và inventory jobs

### Phạm vi triển khai

- [ ] Bucket QoS policy: request rate, bandwidth, burst, priority và tenant fairness.
- [ ] Preview/apply/reset QoS qua worker, có conflict detection.
- [ ] Phân biệt compression policy với object metadata; không giả định mọi object đã được nén.
- [ ] Scheduled inventory job độc lập với trang listing.
- [ ] Lịch sử inventory theo bucket/prefix/time window.
- [ ] Top prefix, growth rate, cold object, orphan/duplicate candidate và object age distribution.
- [ ] Báo cáo lifecycle/tiering recommendation có confidence và evidence.
- [ ] Export CSV/JSON và retention của inventory report.
- [ ] Job retry/resume/cancel/reconciliation và health per target.
- [ ] Quota/cost guard để inventory lớn không làm nghẽn RGW.

### Tiêu chí hoàn thành

- Có thể lập lịch inventory theo bucket và xem các phiên bản report.
- Có progress, cancel, retry và failed-item reconciliation.
- Có tăng trưởng theo thời gian và top prefix dựa trên snapshot thật.
- Recommendation chỉ là advisory, không tự thay đổi lifecycle nếu chưa được duyệt.
- Có test bucket lớn, timeout, partial listing và duplicate candidate false positive.

## 9. P1 — Kubernetes/Helm deployment

### Phạm vi triển khai

- [ ] Helm chart cho các thành phần dashboard, worker, watcher, scheduler và realtime gateway.
- [ ] Values schema, image digest pinning và environment separation.
- [ ] Readiness/liveness/startup probes.
- [ ] PodDisruptionBudget, topology spread/anti-affinity và resource requests/limits.
- [ ] NetworkPolicy tối thiểu cho PostgreSQL, RabbitMQ, Ceph/RGW, Loki và external KMS.
- [ ] External PostgreSQL/RabbitMQ configuration.
- [ ] Secrets integration qua Kubernetes Secret và external secret provider.
- [ ] Migration Job có lock/idempotency/rollback guidance.
- [ ] ServiceAccount/RBAC tối thiểu, securityContext, read-only filesystem khi khả thi.
- [ ] Backup/restore của config, registry, evidence và job state.
- [ ] Upgrade/rollback runbook và compatibility matrix.

### Tiêu chí hoàn thành

- Cài mới được bằng Helm trên cluster sạch.
- Upgrade/rollback không mất snapshot, registry, evaluation hoặc queued job.
- Probe phản ánh đúng dependency health, không chỉ process alive.
- NetworkPolicy và secret integration có test.
- Có chart lint, template test, smoke test và release artifact checksum.

## 10. P2 — AI Data Catalog

### Mục tiêu

Xây catalog trên RGW cho artifact AI mà không trộn với model registry dự báo vận hành hiện có.

### Phạm vi triển khai

- [ ] Entity model: model, version, dataset, checkpoint, feature set, evaluation và artifact.
- [ ] Metadata schema, owner, project, environment, license và retention.
- [ ] SHA-256/checksum, object version, media type, size và storage location.
- [ ] Lineage model-to-dataset-to-checkpoint-to-evaluation.
- [ ] Promotion state: draft, validated, staged, production, deprecated, revoked.
- [ ] Immutability/retention policy theo artifact.
- [ ] Search/filter/tag và dependency graph.
- [ ] Import/sync từ Hugging Face API ở chế độ read-only trước.
- [ ] Evidence cho download, promotion, checksum mismatch và access decision.
- [ ] Approval trước promote/retire/delete.

### Tiêu chí hoàn thành

- Catalog không phụ thuộc vào tên file tự do.
- Có checksum verification và phát hiện object thay đổi.
- Truy ngược lineage được từ model tới dataset/checkpoint.
- Có retention/promotion audit và rollback state.
- Hugging Face sync có rate limit, pagination, provenance và không tự tải artifact nếu chưa được duyệt.

## 11. P2 — Iceberg integration

### Nguyên tắc

Không tự viết Iceberg engine. Tích hợp Apache Polaris, Project Nessie hoặc REST Catalog; Ceph-AI chỉ quản lý observability, governance và operations xung quanh catalog/table.

### Phạm vi triển khai

- [ ] Catalog connection profile cho REST Catalog/Polaris/Nessie.
- [ ] Credential, TLS, endpoint test và capability discovery.
- [ ] Read-only catalog/table/namespace inventory.
- [ ] Table health: snapshot age, metadata size, orphan file candidate và failed commit.
- [ ] Snapshot expiration preview và approval.
- [ ] Orphan cleanup preview, retention guard và evidence.
- [ ] Object store path validation, checksum/size drift và encryption posture.
- [ ] Table-to-bucket/object lineage liên kết với AI Data Catalog.
- [ ] Không sửa hoặc xóa table/object nếu chưa có explicit approval.
- [ ] Contract test cho từng catalog implementation.

### Tiêu chí hoàn thành

- Kết nối được tối thiểu một REST Catalog implementation.
- Inventory và health check có pagination, timeout, retry và evidence.
- Snapshot expiration/orphan cleanup có dry-run, approval, cancel và rollback guidance.
- Tách rõ lỗi catalog, lỗi RGW và lỗi object store.
- Có compatibility matrix theo version Iceberg/catalog.

## 12. Thứ tự triển khai đề xuất

### Wave 0 — Contract và safety foundation

- [ ] Capability/action registry chung cho RGW, IAM, multisite, KMS, jobs và catalog.
- [ ] Approval, idempotency, audit, evidence, retry/resume/cancel/reconciliation.
- [~] Secret reference và redaction framework (đã áp dụng cho provider registry IAM; cần dùng chung cho các capability còn lại).
- [ ] Feature flag riêng cho từng capability; mặc định read-only với tính năng mới.
- [ ] Contract test và failure-injection harness.
- [ ] UI pattern chung: preview, diff, confirmation, progress và post-check.

### Wave 1 — P0 production foundation

- [~] Federated IAM (đã có provider/role mapping registry, Worker reconcile adapter, LDAP/AD/Vault adapter và STS session slice; còn live verification, hard revoke, simulator và access review).
- [ ] Multisite topology và safe operations.
- [ ] KMS/SSE management.
- [ ] Post-check, evidence và rollback cho cả ba nhóm.

### Wave 2 — P1 data protection và operations

- [ ] Object Lock mutation/WORM audit.
- [ ] S3 Event Notification.
- [ ] Scheduled inventory, QoS và compression posture.
- [ ] Helm deployment và external dependency integration.

### Wave 3 — P2 AI/data platform

- [ ] AI Data Catalog.
- [ ] Iceberg REST Catalog integration.
- [ ] Lineage liên kết Object Storage, catalog và AI artifacts.

## 13. Release gates

- [ ] Không có destructive operation mặc định không cần approval.
- [ ] Không ghi secret/token vào log, audit, notification hoặc evidence.
- [ ] Mọi async action có trạng thái durable và phục hồi sau restart.
- [ ] Mọi action thay đổi cluster/object phải có pre-check, post-check và request ID.
- [ ] Không tự tạo Incident, Action, Telegram hoặc remediation từ event/inventory nếu feature flag chưa bật.
- [ ] Có rollback hoặc hướng dẫn khôi phục rõ ràng cho từng action.
- [ ] Có audit trail cho actor, scope, before/after, approval và kết quả.
- [ ] Có UI empty/loading/error state và health state riêng cho từng dependency.
- [ ] Có test unit, contract, integration, failure injection và smoke test trước khi đánh dấu hoàn thành.

## 14. Liên kết với các Plan hiện có

- Plan/aistor-inspired-feature-roadmap.md: các pattern AIStor-inspired về provider contract, evidence plane, async jobs, governance, replication và deployment.
- Plan/object-storage-roadmap.md: bucket/object browser, Object Lock, multisite và hardening hiện có; cần cập nhật checkbox theo từng release gate, không đánh dấu hàng loạt.
- Plan/backup-roadmap.md: digest, restore drill, retention và cross-site recovery; cần liên kết với multisite failover/failback và WORM evidence.
- File này là gap plan chính thức cho chín nhóm tính năng mới; khi triển khai xong từng checkbox phải bổ sung file evidence, test case và release note tương ứng.

## 15. Definition of Done chung

Một mục chỉ được chuyển sang completed khi đồng thời có:

1. Backend/API hoặc worker action thật.
2. UI/API contract và validation.
3. Durable state, retry/resume/cancel/reconciliation nếu là job.
4. Approval/rollback cho thao tác rủi ro.
5. Audit/evidence và redaction.
6. Test thành công, thất bại, restart và partial failure.
7. Documentation/runbook và migration/upgrade note.
8. Feature flag hoặc capability gate được cấu hình đúng.
