# Independent Source Review Remediation Plan — 2026-09-22

**Repository:** `PHAMXUANKHIEM/ceph-ai`
**Baseline reviewed:** `e5a1df169ecab4b7c04ff93ce739bd4822c2dc83`
**Target:** `10.3.55.213:/root/ceph-ai`
**Status:** Open — release và autonomous remediation vẫn bị chặn
**Review source:** Independent source review ngày 22/09/2026
**Scope rule:** Báo cáo review chỉ là bằng chứng phát hiện/probe. Không coi
production-ready nếu chưa có test, runtime evidence và operator approval tương ứng.

**Related plans:** `production-reassessment-follow-up-plan.md`,
`production-readiness-02-test-and-ci.md`, `production-readiness-04-executor-isolation.md`,
`production-readiness-05-security-baseline.md` và
`production-readiness-06-reproducible-deployment.md`.

**Follow-up review:** `release-stability-and-ai-evidence-follow-up-2026-09-24.md`.
File này ghi riêng các lỗi CI của HEAD `7abd88ca`, bằng chứng self-learning còn
thiếu, 21 scope drift, và thứ tự nghiệm thu release sau review mới nhất.

## 0. Quyết định an toàn ngay lập tức

- [ ] Giữ hệ thống ở `ADVISORY` hoặc `APPROVAL_REQUIRED`; không bật
  `LIMITED_AUTOPILOT`/autonomous remediation trên production.
- [x] Giữ `Single Full` là chế độ operator toàn quyền riêng biệt. Không áp
  typed gateway vào đường này; vẫn bắt buộc Telegram authorization, executor
  token, signed cluster scope, audit và container boundary hiện hữu.
- [ ] Không chạy prompt-injection, destructive-command hoặc đổi firewall trên
  cluster thật. Các kiểm thử boundary chỉ chạy trên fixture/sandbox/staging cô lập.
- [ ] Ghi baseline release gồm source SHA, migration head, image digest, config
  fingerprint, runtime owner và trạng thái autopilot; không dùng số liệu review
  offline để đại diện cho production.

**Release rule:** Chỉ được chuyển từ advisory sang approval pilot sau khi toàn bộ
P0 pass. Không mở autonomy rộng hơn cho đến khi P1 release/recovery gates pass và
đã có operator sign-off.

## 1. Ma trận phát hiện và nơi thực hiện

| Finding | Mức | Kết luận hiện tại | Plan thực hiện chính | Trạng thái |
|---|---:|---|---|---|
| F01 Secret/config validator | P0 | Validator đã sửa và regression pass; chưa commit/release | Plan này + `production-readiness-05-security-baseline.md` | [~] |
| F02 Session revocation | P1 | HTTP/WebSocket revoke đã có code; migration đã áp dụng, rehearsal/restore/rollout evidence còn pending | Plan này + security baseline | [~] |
| F03 execution boundary | P0 | Typed gateway đã áp dụng cho Incident/Autopilot; Single Full giữ nguyên toàn quyền operator | Plan này + `production-readiness-04-executor-isolation.md` | [~] |
| F04 Incident MQ outbox | P1 | Outbox, retry, consumer idempotency, metrics và reconciler đã có; live chaos còn thiếu | Plan này + reassessment follow-up | [~] |
| F05 Release dependency gate | P1 | Clean runner install và integration blocking đã có; clean workflow evidence còn pending | Plan này + `production-readiness-02-test-and-ci.md` | [~] |
| F06 pip-audit parser | P1 | Nested/malformed report đã fail-closed; artifact metadata và CI evidence còn pending | Plan này + CI/release gate | [~] |
| F07 Immutable deployment | P1 | Deploy script nhận image đã scan và đối chiếu digest trước/sau restart; registry push/pull, base-image/dependency lock và staging rehearsal còn pending | Plan này + `production-readiness-06-reproducible-deployment.md` | [~] |
| F08 Documentation/release manifest | P2 | Quickstarts dev/prod, README clarification và CI release manifest generator đã có; LICENSE/COPYING và operator sign-off còn pending | Plan này + release manifest | [~] |
| Cross-cutting RBAC/scope | P1 | Cần nghiệm thu theo cluster/capability, không chỉ route guard | Security baseline + end-to-end plan | [ ] |
| Cross-cutting evidence/benchmark | P1 | Chưa có browser, PostgreSQL, chaos và live performance evidence | Reassessment follow-up + Plan này | [ ] |

Các Plan liên kết là nơi giữ chi tiết domain. File này là control plane của review:
không tạo lại cùng một task ở nhiều nơi; mọi task phải có một owner, một artifact
và một acceptance command.

## 2. P0 — đóng các đường có thể phá vỡ security boundary

### 2.1 F01 — Production configuration fail-closed

**Mục tiêu:** Không cho Dashboard khởi động production nếu session signing key,
admin credential hoặc environment profile không hợp lệ.

- [x] Dùng enum/allowlist cho `CEPH_AI_ENVIRONMENT`; reject giá trị lạ, khác chữ
  hoa/thường ngoài quy ước, chuỗi rỗng và whitespace.
- [x] Reject `SESSION_SECRET_KEY` khi rỗng, whitespace, giá trị mặc định/dev,
  quá ngắn hoặc entropy không đạt ngưỡng đã quy định.
- [x] Reject password hash rỗng, không đúng bcrypt format hoặc dùng credential
  dev mặc định; không log giá trị secret/hash trong exception.
- [x] CSRF production gate hoạt động độc lập với session secret validator; thiếu
  một biến không được làm cả nhóm bảo vệ bị bỏ qua.
- [x] Đồng nhất cách đọc environment giữa settings, cookie, CSRF, DB policy,
  compose và middleware; không có nhánh coi `development` là production.
- [x] Đổi `.env.example` thành placeholder rõ ràng, không để trường secret trống
  khiến người vận hành tưởng là hợp lệ; thêm hướng dẫn tạo secret ngẫu nhiên.

**Test bắt buộc:** empty, whitespace, default, secret ngắn, hash sai, environment
sai, CSRF thiếu, production hợp lệ và development hợp lệ.

**Acceptance:**

```text
.venv/bin/pytest -q tests/test_production_readiness.py tests/test_dashboard_auth.py
```

Expected: mọi cấu hình xấu fail closed; cấu hình production hợp lệ pass; không
regression cho development/test. Artifact cần lưu: JUnit, config case matrix,
commit SHA và output không chứa secret.

Evidence 2026-09-22: `config/settings.py` dùng `Literal` cho environment;
`dashboard/app.py` kiểm tra username, bcrypt structure/hash parsing, secret
không rỗng/default và tối thiểu 32 bytes. `.env.example` không còn để trống hai
trường bảo mật. `tests/test_production_readiness.py`: `27 passed`; regression
`tests/test_production_readiness.py tests/test_dashboard_auth.py
tests/test_dashboard_settings.py`: `204 passed, 3 deselected`. Ruff và
`git diff --check` pass trên server. Chưa commit, chưa restart production và
chưa có CI runner evidence, nên F01 vẫn `[~]`.

### 2.2 F03 — Khoá và tái thiết kế Single Full

**Mục tiêu:** Prompt/model không được tự quyết định boundary thực thi.

#### P0 containment trước

- [x] Single Full không bị typed gateway giới hạn; đây là operator escape hatch
  riêng, chỉ vào được sau Telegram authorization, executor token và signed
  cluster scope.
- [x] Typed gateway hard-block đường Incident/Autopilot khi action chưa có
  contract; không dựa vào prompt hoặc UI toggle.
- [ ] Tách credential read-only của Watcher/Dashboard khỏi credential mutation;
  không dùng chung SSH identity giữa read path và executor.
- [ ] Thu hẹp mount `/var/lib/ceph-ai`, workspace và network egress theo service;
  xác định service nào cần read/write và ghi vào release manifest.
- [ ] Code-repair/full-access không được chạy trong production runtime/repository
  workspace; nếu cần nghiên cứu, dùng sandbox/worktree/artifact riêng.

#### Typed executor thay cho shell tự do

- [x] Định nghĩa action contract gồm `action_id`, `cluster_id`, capability,
  target type/id, typed params, evidence fingerprint, expiry và actor.
- [ ] Server kiểm tra allowlist action, capability, target scope, parameter
  bounds, freshness và approval trước khi tạo execution lease.
- [ ] Preflight/post-check/rollback dùng cùng policy với action thủ công; không
  có đường Full Access bỏ qua các chốt này.
- [ ] Shell chỉ được gọi qua gateway allowlist có timeout, output redaction,
  target binding, rate/cooldown và audit; không cho model truyền command string
  tùy ý.
- [ ] Retry/disconnect không được đổi target hoặc làm mất approval fingerprint.

**Test bắt buộc trong sandbox:** prompt injection từ log/repository, target ngoài
scope, capability thiếu, params phá huỷ, đọc secret, approval hết hạn và retry sau
disconnect. Không chạy payload này trên cluster thật.

**Acceptance:** với Incident/Autopilot, server chặn mọi case xấu dù model trả
output bất kỳ; action hợp lệ chỉ chạy đúng target/capability; audit có
before/after/evidence/actor. `Single Full` là luồng riêng có chủ đích: operator
đã xác thực được giữ quyền provider toàn quyền, nhưng không được coi đó là
đường Autopilot hoặc quyền mặc định của model.

**Implementation evidence 2026-09-22 (containment phase):** Single Full giữ
nguyên đường provider toàn quyền theo quyết định operator; không có typed gate
chèn vào `run_single_full_access_chat`, `worker/full_executor.py` hoặc
`_ask(..., full_access=True)`. Các chốt hiện hữu vẫn giữ nguyên: Telegram
authorization, executor token, signed cluster scope, database reconciliation,
audit/recovery marker và container isolation. Policy mới chỉ chặn
`worker/code_repair.py` full-access automation trong production hoặc khi
`CODE_REPAIR_AUTO_ENABLED` tắt. Không chạy provider thật, không chạy SSH/Ceph
và không chạy payload phá huỷ.

**Focused evidence:** `tests/test_single_full_policy.py`,
`tests/test_full_executor.py`, `tests/test_dual_ai_chat.py`,
`tests/test_code_repair.py`, `tests/test_executor_isolation.py` đạt `49 passed`;
Ruff và `git diff --check` pass. Đây mới là containment, chưa đủ để đánh dấu
F03 hoàn thành: còn typed action contract, capability/target/parameter gate,
preflight/post-check/rollback, gateway allowlist, credential separation, mount/
egress review và sandbox boundary test.

**Typed contract evidence 2026-09-22:** thêm
`worker/executor/action_contract.py` với `TypedActionRequest`, `TargetScope`,
`TypedActionGateway` và fingerprint bất biến cho approval/idempotency. Contract
reject field thừa, command/shell tự do, params quá sâu/quá lớn, fingerprint sai,
expiry quá hạn, action/capability ngoài allowlist, cluster/target ngoài scope và
capability matrix trả false. Action cần approval phải có fingerprint khớp cả
contract lẫn giá trị server-side. `tests/test_action_contract.py`: `11 passed`.
Module này là boundary validator độc lập; router adapter đã nối nó vào đường
tạo execution lease hiện hữu cho Incident SAFE.

**Lease integration evidence 2026-09-22:** adapter trong
`worker/llm/router_client.py` gọi `TypedActionGateway` ngay trước
`acquire_lease`, sử dụng `Action.target_nodes/action_params/expires_at`,
`RemediationCase.evidence_fingerprint` và `Cluster` scope đã lưu trong DB.
Preflight capability luôn được đánh giá ở execution boundary; cờ tương thích
chỉ còn ảnh hưởng proposal, không được làm gateway tự giả định capability đúng.
Action thiếu playbook, thiếu case/expiry, target ngoài cluster, capability bị
từ chối hoặc dữ liệu persisted sai đều bị chuyển sang approval và không lấy
lease. Regression `tests/test_router_client.py`: `129 passed`; test riêng target
ngoài scope xác nhận `acquire_lease` không được gọi. Adapter này cố ý không áp
dụng cho Full Executor prompt path, vì đó là chế độ operator toàn quyền đã
được yêu cầu giữ nguyên; Full Executor tiếp tục dựa trên token, signed scope,
database reconciliation, audit/recovery marker và container isolation.

### 2.3 P0 gate sau khi sửa

- [x] F01 regression pass: focused production/auth batch passed; CI runner and
  production rollout evidence remain release-level blockers.
- [ ] F03 sandbox boundary pass.
- [ ] Không còn đường Incident/Autopilot gọi provider full-access; Single Full
  operator mode là ngoại lệ có chủ đích và phải giữ token/scope/audit boundary.
- [ ] Security owner xác nhận credential separation, mount và egress.
- [ ] Cập nhật `Plan/in-progress/production-readiness-05-security-baseline.md`
  và `04-executor-isolation.md` bằng command/result/artifact; không chỉ đổi checkbox.

## 3. P1 — khép kín tính toàn vẹn của luồng vận hành

### 3.1 F02 — Session revocation tập trung

**Mục tiêu:** Disable/delete/reset password/role change phải vô hiệu hoá session
cũ mà không logout nhầm user khác.

- [x] `require_login` lookup user theo immutable user id hoặc identity an toàn,
  kiểm tra user tồn tại và `is_active` ở dependency tập trung.
- [x] Thêm `session_version` hoặc `security_epoch` ở user/account; lưu version
  trong session và tăng version khi disable, delete, password reset hoặc quyền
  thay đổi.
- [x] Không dựa riêng vào cookie `max_age`; session cũ phải fail ngay sau revoke.
- [x] Áp dụng cùng check cho WebSocket handshake và các connection đang mở;
  đóng hoặc đánh dấu stale connection sau revoke.
- [x] Có cơ chế riêng cho bootstrap/root account nếu account không có row user
  bình thường; mọi bypass phải audit được.

**Acceptance:** HTTP session cũ và WebSocket cũ bị từ chối sau disable/delete/reset;
user khác vẫn hoạt động; audit ghi actor/reason/request id nhưng không ghi cookie.

Implementation evidence 2026-09-22: thêm `session_version` cho `users` và
`vitastor_users`, migration `m20260922authsessionversion`, session claims chứa
immutable `user_id` + version, còn tài khoản root dùng fingerprint của
`product/username/password_hash`. `require_login` và cả hai WebSocket handshake
đều đọc lại trạng thái active/version; disable, delete, password change và
privilege/scope change đều làm tăng version. Test auth/user/WebSocket đạt
`70 passed`; migration + production/auth regression đạt `71 passed`. Ruff và
`git diff --check` pass. Database runtime đã lên
`m20260923incidentmetrics` bằng backup-first migration wrapper; F02 vẫn chưa
đóng vì còn thiếu rehearsal/restore witness và rollout evidence trên staging.

### 3.2 F04 — Durable Incident Outbox cho RabbitMQ

**Mục tiêu:** Incident đã commit cuối cùng phải được publish hoặc được retry,
không mất job ở khoảng hở DB → broker.

**Implementation slice 2026-09-23:** đã thêm `IncidentOutbox`, migration
`m20260923incidentoutbox`, `shared/incident_outbox.py`, ghi envelope trong cùng
transaction với Incident cho cả health watcher, observed-cluster watcher và
verification retry, cùng Worker dispatcher có lease, exponential backoff,
`DEAD` terminal state, publish latency/queue metrics và redacted error.
Consumer đã có claim/done/release theo event id để chống redelivery. Smoke test
transaction, success, failure/retry và consumer idempotency đã pass. Chưa đánh
dấu hoàn thành vì còn RabbitMQ confirm/chaos evidence.

- [x] Tạo `incident_outbox` append/durable với incident id, event type/version,
  cluster scope, payload đã redacted, idempotency key, attempts, next retry,
  delivery state, last error và timestamps.
- [x] Insert Incident và outbox row trong cùng DB transaction.
- [~] Dispatcher claim row bằng lease/locking, retry exponential backoff có jitter,
  publisher confirm và cập nhật `SENT` chỉ sau broker confirm.
- [~] Có trạng thái `FAILED/DLQ` và metric tuổi hàng đợi, attempt count, oldest
  pending, publish latency; không retry vô hạn không quan sát được.
- [x] Consumer claim/idempotency theo incident/event id; redelivery không tạo
  duplicate diagnosis hoặc mutation.
- [x] Reconciler tìm `NEW` quá tuổi, outbox pending quá tuổi và Incident không có
  delivery; tạo cảnh báo/operator action.

**Chaos acceptance trên RabbitMQ/DB cô lập:** broker down trước commit, sau commit
trước publish, sau publish trước confirm, worker kill giữa claim/publish, duplicate
delivery và DB reconnect. Mỗi incident phải cuối cùng được xử lý đúng một lần về
side effect; delivery có thể at-least-once nhưng consumer phải idempotent.

### 3.3 F05 — Release gate chạy được trên runner sạch

- [~] Chọn một nguồn dependency duy nhất: lock/constraints được version-control,
  hoặc gate chạy trực tiếp trong image đã build.
- [x] Release gate cài dependency trước `python -m alembic`, scanner và tests;
  không phụ thuộc package đã cài từ job trước.
- [~] Tách `test`, `quality`, `release_gate`, `integration` và `deploy` bằng
  artifact/output rõ ràng; deploy bắt buộc tất cả job blocking pass.
- [x] Integration MQ/PostgreSQL không còn chỉ `workflow_dispatch` nếu release
  cần chúng; live/destructive vẫn manual approval nhưng negative result phải chặn.
- [ ] Runner sạch phải kiểm tra đúng SHA, migration head, dependency lock,
  scan artifact, JUnit và image digest.

**Acceptance:** chạy toàn workflow trên clean runner; xoá dependency cache vẫn pass;
thiếu dependency hoặc integration fail phải chặn deploy.

### 3.4 F06 — pip-audit parser fail-closed

- [x] Pin version/schema của pip-audit parser.
- [x] Parse đúng `dependencies[].vulns[]`; vuln nested phải tăng count và làm gate
  fail theo policy.
- [x] Reject `{}`, schema lạ, thiếu `dependencies`, dependency item sai kiểu hoặc
  vulns sai kiểu; không coi malformed report là sạch.
- [~] Artifact phải gắn SHA, scanner version, timestamp, environment và checksum.
- [x] Giữ exit code trực tiếp của scanner là tín hiệu chính; parser chỉ chuẩn hoá
  evidence, không được làm mất failure.

**Acceptance fixtures:** clean, one vulnerability, multiple vulnerabilities,
malformed, missing field, scanner error. Expected result phải được assert chính xác.

### 3.5 F07 — Immutable artifact và deployment identity

- [~] CI build/scan/SBOM và push cùng image lên GHCR bằng tag SHA, lưu registry
  manifest digest làm artifact; chưa chạy clean GitHub Actions với registry thật.
- [~] Deploy script pull `CEPH_AI_IMAGE=ghcr.io/...@sha256:...`, kiểm tra label SHA
  và image ID của container; chưa rehearsal staging/production.
- [~] Bỏ source checkout bind mount khỏi runtime Compose, bake frontend/backend,
  migration và runbook cần thiết vào image; Code Repair chuyển host supervisor
  riêng, tắt auto push/deploy/promotion chờ pipeline artifact cho candidate.
- [~] Có `requirements-prod.lock` với wheel hashes và pin base image theo digest;
  direct apt package versions đã pin. Transitive OS packages vẫn phụ thuộc apt
  repository hiện hành, cần snapshot repository và toolchain evidence.
- [~] Thứ tự deploy: approved digest pull/label check → backup → migration
  compatibility check chạy từ image → rollout → health/smoke; staging rehearsal
  và PostgreSQL witness còn thiếu.
- [~] Có rollback container-only về previous registry digest, bắt buộc operator
  xác nhận schema compatible. Chưa có tự động đối chiếu migration/config version
  và chưa có PostgreSQL restore/rollback rehearsal; tuyệt đối không auto-downgrade.

**Acceptance:** digest CI bằng digest đang chạy; thay đổi checkout host không đổi
runtime; rehearsal build failure/migration failure/rollback trên PostgreSQL staging
được lưu artifact và có operator witness.

## 4. P1 — quyền, scope và bằng chứng vận hành

### 4.1 RBAC/capability theo cluster

- [ ] Lập route/action inventory: read, preview, execute, admin, destructive;
  map từng action với `cluster_id`, capability và audit event.
- [ ] User không có capability không nhìn thấy hoặc không gọi được action; server
  không tin cluster/role do frontend hoặc model gửi lên.
- [ ] Đổi cluster phải reset selection/action/evidence của cluster cũ; không dùng
  stale target cho execute.
- [ ] Test tenant isolation, cross-cluster target, direct API, WebSocket và
  background worker; không chỉ test UI navigation.

### 4.2 PostgreSQL, rollback và recovery

- [ ] Chạy migration rehearsal trên PostgreSQL cô lập, có backup trước migration,
  checksum, restore-list và health witness.
- [ ] Thử rollback app/image và recovery migration sau migration failure.
- [ ] Diễn tập DB unavailable, RabbitMQ unavailable, SSH unavailable và AI provider
  unavailable; hệ thống phải giữ evidence, không tạo mutation mù.
- [ ] Cập nhật `production-reassessment-follow-up-plan.md` với target/owner/window;
  không đánh dấu PostgreSQL/DR pass bằng SQLite evidence.

### 4.3 Performance và cost baseline

- [ ] Đo API p50/p95/p99, SSH calls/poll, collector lag, DB connections, queue
  age, CPU/RAM/disk và AI calls/token/cost theo cluster.
- [ ] Chạy ma trận 1/5/10 tabs và nhiều cluster trên staging; lưu trace/HAR chỉ
  khi đã redact cookie/token.
- [ ] Đặt SLO/budget trước canary; phân biệt cache hit, stale fallback, live query
  và provider retry.

## 5. P2 — tài liệu, UX và maintainability

### 5.1 Documentation/release manifest

- [x] Viết quickstart dev riêng: SQLite/lab, process, seed account, feature flags.
- [x] Viết deployment production riêng: PostgreSQL/RabbitMQ/Vault/SSH, TLS,
  trusted proxy, secrets, migrations, health checks, backup và rollback.
- [x] README không được mô tả nohub/SQLite như production mặc định.
- [x] Sinh release manifest theo SHA gồm image digest, migration head, dependency
  lock hash, SBOM/scan, test/JUnit, config fingerprint, rollback artifact và
  residual risk/approval expiry; manifest không ghi secret và giữ trạng thái
  production approval riêng.
- [ ] Bổ sung LICENSE/COPYING hoặc công bố rõ điều kiện sử dụng/phân phối.

### 5.2 Incident workspace và UI consistency

- [ ] Tập trung luồng `phát hiện → evidence → RCA → đề xuất → duyệt → xác minh`.
- [ ] Hiển thị rõ observed fact, AI inference, missing/stale evidence và action
  risk; không dùng confidence của model như sự thật.
- [ ] Action preview hiển thị target, capability, evidence age/fingerprint,
  preflight, approval expiry và post-check.
- [ ] Thống nhất design tokens/i18n giữa React và Jinja; test loading, empty,
  error, stale, keyboard và viewport 1280/1440/1920/mobile.
- [ ] Ẩn menu/action theo RBAC nhưng vẫn giữ server-side enforcement.

### 5.3 Tách module lớn

- [ ] Lập dependency graph cho module 3.000–4.100 dòng; chọn một vertical slice
  ít rủi ro để tách service/domain boundary.
- [ ] Không refactor hàng loạt trong cùng release với F01–F07; mỗi slice phải giữ
  API contract, migration compatibility và regression test.
- [ ] Xoá staging/transfer/backup source khỏi production artifact và thêm hygiene
  check vào CI.

## 6. Thứ tự triển khai theo đợt

### Đợt A — Safety containment

F01 → F03 containment → xác nhận autopilot/Single Full disabled → F02 session
revocation. Không triển khai feature autonomy mới trong đợt này.

### Đợt B — Durable operations

F04 Incident outbox → F05 clean release gate → F06 audit parser. Chạy chaos
RabbitMQ/DB cô lập và clean-runner workflow.

### Đợt C — Reproducible release

F07 immutable image/digest → PostgreSQL migration/restore rehearsal → rollback
rehearsal → release manifest. Chỉ dùng artifact đã scan để deploy staging.

### Đợt D — Operator workflow

RBAC/cluster scope → incident workspace → browser smoke/load → documentation.

### Đợt E — AI evidence and autonomy

Temporal holdout, independent labels, cost/unsafe/abstention metrics, shadow/canary
và chỉ sau đó mới xem xét mở từng action nhỏ theo cluster. Không auto-promote chỉ
vì loss thấp hoặc unit test xanh.

## 7. Definition of Done cho Plan này

- [ ] F01, F03 P0 pass trên test matrix và sandbox boundary; Single Full vẫn có
  kill switch và operator approval.
- [ ] F02 session cũ bị revoke đúng; WebSocket/HTTP cùng policy.
- [ ] F04 incident không mất sau broker/worker failure; consumer idempotent.
- [ ] F05/F06/F07 clean runner, parser, image digest và deployment rehearsal pass.
- [ ] PostgreSQL backup/restore/migration/rollback có artifact và witness.
- [ ] RBAC/capability/cluster scope pass cả direct API, worker và WebSocket.
- [ ] README, runbook và release manifest khớp đúng SHA đang phát hành.
- [ ] Có owner, expiry và residual-risk acceptance cho mọi blocker còn mở.
- [ ] Chỉ khi các gate trên pass mới được đề xuất mở rộng autonomy; không tự động
  chuyển mode trong code.

## 8. Nhật ký evidence

| Ngày | Hạng mục | Kết quả | Artifact/command | Ghi chú |
|---|---|---|---|---|
| 2026-09-22 | Independent source review | 94 trọng tâm pass; 3 probe tái hiện; 1 Alembic head; SQLite upgrade pass | Báo cáo review tại user input | Chưa kiểm PostgreSQL, browser, production, chaos |
| 2026-09-22 | Baseline source | `e5a1df16` | `git rev-parse --short HEAD` | Không coi source review là runtime sign-off |
| 2026-09-23 | F05/F06 release gate slice | F06 tests `3 passed`; clean release job now installs application dependencies; nested/malformed pip-audit reports fail closed and carry report hash, scanner version, timestamp, SHA and Python metadata | `tests/test_release_gate.py`, `.github/workflows/ci-cd.yml`, `scripts/ci/release_gate.py` | Clean GitHub runner and full integration/deploy evidence remain pending |
| 2026-09-23 | F02 migration rollout | PostgreSQL current head `m20260923incidentoutbox`; backup-first wrapper completed | `alembic current`, `/var/backups/ceph-ai/ceph-ai-20260923T015841Z.dump` | Staging restore/rehearsal and release witness remain pending |
| 2026-09-23 | F04 consumer idempotency | Consumer claim/done/release added; outbox test `4 passed`; migration applied | `m20260923incidentconsumer`, `tests/test_incident_outbox.py` | Publisher-confirm proof, reconciler and chaos remain pending |
| 2026-09-23 | F04 metrics/confirm slice | Explicit `publisher_confirms=True`; delivery stats include due count, oldest pending age, max attempts and average publish latency; outbox/release tests `7 passed` | `m20260923incidentmetrics`, `tests/test_incident_outbox.py tests/test_release_gate.py` | Live RabbitMQ confirm and chaos evidence remain pending |
| 2026-09-23 | F04 producer outbox slice | Implementation + isolated SQLite smoke pass | `m20260923incidentoutbox`, transaction/success/failure-retry smoke | Chưa chạy PostgreSQL/RabbitMQ chaos; consumer idempotency và reconciler còn mở |
| 2026-09-23 | F04 reconciler | Worker periodically scans stale `NEW` incidents and pending/processing outbox rows; test `5 passed` | `shared/incident_outbox.py::reconcile_stale`, `worker/main.py`, `tests/test_incident_outbox.py` | Operator alert routing and RabbitMQ/DB chaos evidence remain pending |
| 2026-09-23 | F05 integration gate | MQ integration job now runs on push/PR/workflow dispatch and is required by deploy; RabbitMQ URL is explicit | `.github/workflows/ci-cd.yml` | Clean GitHub Actions run and PostgreSQL integration evidence remain pending |
| 2026-09-23 | F08 documentation slice | Added separate development and production deployment quickstarts; README now labels legacy SQLite guidance as lab-only | `docs/deployment/development.md`, `docs/deployment/production.md`, `README.md` | Release manifest generation, LICENSE/COPYING and operator sign-off remain pending |
| 2026-09-23 | F07 immutable production packaging slice | Removed application source bind mounts from runtime Compose; removed Code Repair from Compose and kept it in a host supervisor with push/deploy/promotion disabled. Added hashed Python lock, digest-pinned Python/apt inputs, runtime docs/migrations in image, GHCR push artifact, digest/commit checks, image-based migration, boot-time approved reference and explicit container rollback. Final local image build/import smoke passed; packaging/release tests 12/12. | `Dockerfile`, `requirements-prod.lock`, `compose.yaml`, `container-up`, `scripts/deploy/restart_container_stack.sh`, `scripts/deploy/run_migrations.sh`, `scripts/deploy/rollback_container_stack.sh`, `docs/immutable-production-release.md` | GHCR authentication/push-pull, clean GitHub Actions run, PostgreSQL staging rehearsal and witnessed rollback remain pending |
| 2026-09-23 | F08 release manifest slice | Secret-free manifest binds commit, branch/clean state, one migration head, dependency hashes, image digest, test/JUnit, quality, pip-audit, scan, SBOM, config/rollback references and residual risk | `scripts/ci/release_manifest.py`, `tests/test_release_manifest.py`, `.github/workflows/ci-cd.yml`; focused release/security batch `63 passed` | GitHub Actions artifact and operator approval evidence remain pending |
| 2026-09-24 | Follow-up review control plan | Ghi nhận HEAD `7abd88ca` chưa release-ready vì Python 3.11/3.12 và integration đỏ; mở các workstream sửa contract/test, thu verified River outcomes, phân loại 21 drift scopes, PostgreSQL/RabbitMQ/RBAC/credential rehearsal, GHCR witness và handoff/license | `Plan/in-progress/release-stability-and-ai-evidence-follow-up-2026-09-24.md`; CI run `35822665843` | Chưa sửa code trong bước lập Plan; giữ Advisory/Approval Required và Single Full nguyên trạng |

## 9. Quy tắc cập nhật trạng thái

- `[ ]` chưa có implementation/evidence đủ.
- `[~]` đã có code hoặc test một phần nhưng còn thiếu acceptance/runtime/owner.
- `[x]` chỉ dùng khi có command, kết quả, artifact, môi trường và rollback note.
- `[!]` bị chặn bởi quyền, hạ tầng, safety window hoặc operator decision; không
  tự ý vượt blocker.
