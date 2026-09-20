# Production Reassessment and Release-Blocker Closure Plan

## 1. Mục tiêu và phạm vi

Plan này chuyển báo cáo đánh giá tại commit `e71ba63` thành chuỗi công việc có
thể thực thi, kiểm chứng và ký duyệt. Mục tiêu là tạo bằng chứng đủ để phân
biệt ba trạng thái:

1. **RC/lab** — chạy trong lab hoặc canary có người giám sát.
2. **Staging-approved** — qua security, failure injection, backup và rollback
   rehearsal trên môi trường không production.
3. **Production-approved** — có operator approval, runtime owner duy nhất,
   migration backup/rollback và giới hạn blast radius được ghi nhận.

Phạm vi gồm lỗi full-suite `DetachedInstanceError`; Python 3.11/3.12; session
cookie và trusted proxy; least privilege/container/SSH-key/code-repair;
CI quality gate, warning budget, timezone-aware timestamp, dependency/image
scan; mâu thuẫn `.env.example`/README; các feature gate còn mở trong E2E plan;
staging, rollback rehearsal, live DR drill và sign-off.

Không bật autonomous remediation hoặc chạy destructive/live DR trong quá trình
đóng task code-side nếu chưa có approval riêng và safety window.

## 2. Bằng chứng đầu vào và quy ước

### 2.1 Baseline phải được tái lập

Báo cáo đầu vào ghi nhận:

- Python 3.12: `3853 passed, 1 failed, 16 deselected, 17132 warnings`;
- failure: `test_multiple_simultaneous_checks_create_one_incident_each_and_publish_all`
  với `DetachedInstanceError` tại `watcher/main.py`;
- test riêng pass nhưng full suite fail, có dấu hiệu state/order leakage;
- CI hiện chỉ có Python 3.11;
- Alembic một head, SQLite migration round-trip pass, `npm audit` production
  không có lỗ hổng tại thời điểm đánh giá;
- release manifest còn operator approval, migration rehearsal và deployment
  ở trạng thái `PENDING`;
- runtime ownership giữa Podman và systemd chưa được chốt.

Trước khi đánh dấu task `[x]`, phải chạy lại baseline trên HEAD đang làm việc và
ghi SHA, Python version, dependency hash, database backend, command, duration,
warning count và artifact path. Không coi kết quả của `e71ba63` là bằng chứng cho
HEAD mới.

### 2.2 Trạng thái

- `[ ]` chưa làm;
- `[~]` đang làm hoặc chỉ có một phần bằng chứng;
- `[x]` code, test, tài liệu và runtime evidence đã đủ;
- `[!]` bị chặn bởi approval, môi trường, credential hoặc safety window.

Mỗi task phải có owner, commit SHA, rollback path, test command và evidence link.

## 3. Thứ tự thực thi bắt buộc

```text
P0 correctness
  -> P0 security/session
  -> P0 CI matrix
  -> P1 isolation/runtime ownership
  -> P1 static quality/warnings/docs
  -> feature acceptance gates
  -> staging soak + backup/rollback rehearsal
  -> operator/security sign-off
  -> production canary
```

Không chuyển bước khi còn failure chưa phân loại. Ngoại lệ phải là waiver có
owner, lý do, ngày hết hạn và blast-radius review.

## 4. Workstream P0 — deterministic suite và Python 3.12

### PR-01.1 Reproduce và cô lập failure `[~]`

- [ ] Tạo môi trường sạch riêng cho Python 3.11 và 3.12 từ cùng dependency
  constraints; không dùng database, cache, `.env` hoặc broker của service đang chạy.
- [ ] Chạy failure riêng, test module, rồi full suite tối thiểu hai lần trên mỗi
  Python version.
- [ ] Chạy hai thứ tự test khác nhau bằng order seed hoặc chia batch theo module.
- [ ] Ghi `pytest --durations=50`, warning summary, JUnit XML và environment
  manifest cho từng run.

Lệnh gate tối thiểu:

```bash
pytest -q tests/<watcher-test-file>.py -k multiple_simultaneous
pytest -q -k 'not live and not integration' --junitxml=artifacts/pytest.xml
pytest -q -k 'not live and not integration' --junitxml=artifacts/pytest-repeat.xml
```

### PR-01.2 Sửa ownership/lifecycle của Incident `[x]`

- [x] Rà toàn bộ path tạo incident trong `watcher/main.py`, đặc biệt path commit
  trước khi đọc `incident.id`.
- [x] Dùng pattern được review: `session.flush()` rồi lấy id trước commit, hoặc
  copy scalar `incident_id` trước commit; không đọc attribute ORM sau commit nếu
  object có thể detached/expired.
- [x] Không dùng `expire_on_commit=False` để che lỗi toàn cục nếu chưa review
  transaction semantics.
- [x] Bổ sung regression cho một check, nhiều check đồng thời, publish đủ event,
  rollback transaction và session close/detach.

### PR-01.3 Chặn test-state leakage `[~]`

- [ ] Kiểm tra fixture database/cache/session/monkeypatch; mọi resource phải reset
  trong `yield`/finalizer.
- [ ] Không dùng singleton mutable state giữa test nếu không có reset hook.
- [ ] Xác nhận event bus/WebSocket publisher được đóng và drain sau mỗi test.
- [ ] Bật repeat run trên cùng process và trên process mới.

### Acceptance PR-01

- Full suite pass liên tiếp **2 lần** trên Python 3.11 và 3.12.
- Failure pass khi chạy riêng, theo module, theo thứ tự ngẫu nhiên và full suite.
- Không có `DetachedInstanceError`, order dependency hoặc state leakage chưa phân loại.
- CI lưu JUnit, warning report, timing report và commit SHA.

Evidence hiện tại trên server `10.3.55.213`:

- `tests/test_watcher_incident_flow.py tests/test_watcher_main.py`: `73 passed`;
- full deterministic Python 3.11: `3834 passed, 47 deselected, 235 warnings`
  trong `1748.10s`; JUnit: `/tmp/ceph-ai-pr01-py311-final.xml`;
- `watcher/main.py` đã dùng `flush -> scalar id -> commit -> session.get(id)`
  trước khi chạy mute inheritance, tránh đọc ORM attribute đã expired và tránh
  query pre-commit làm nhiễu SQLite `StaticPool`;
- `.github/workflows/ci-cd.yml` đã có matrix Python `3.11`/`3.12`, nhưng chưa có
  kết quả GitHub Actions Python 3.12 trong evidence; vì vậy PR-01 chưa được
  đánh dấu hoàn thành toàn bộ.

## 5. Workstream P0 — session cookie và trusted HTTPS

### PR-02.1 Chốt mô hình TLS

- [~] Ghi rõ TLS termination ở reverse proxy nào và IP/CIDR proxy nào được tin.
  `DASHBOARD_TRUSTED_PROXY_IPS` đã là boundary bắt buộc và đã có test peer IP;
  topology reverse-proxy thực tế trên staging/production vẫn cần operator xác nhận.
- [x] Production/staging bắt buộc `https_only=True` cho session cookie; local HTTP
  chỉ được dùng khi environment không phải production/staging.
- [x] Middleware cấu hình tường minh `https_only`, `HttpOnly`, `SameSite=lax` và
  `max_age=1209600`; regression kiểm tra cả options và header phát ra.

### PR-02.2 Dùng một trusted-scheme helper

- [x] Chuẩn hóa helper xác định effective scheme: chỉ dùng `X-Forwarded-Proto`
  khi socket peer thuộc `DASHBOARD_TRUSTED_PROXY_IPS` và giá trị hợp lệ.
- [x] Request trực tiếp giả mạo forwarded header phải bị từ chối hoặc bỏ qua.
- [x] CSRF cookie, HSTS và origin check dùng cùng helper; các nhánh còn lại không
  còn quyết định HTTPS độc lập bằng `request.url.scheme`.
- [~] Nếu có proxy chain, document thứ tự hop và chỉ lấy giá trị đã xác minh; code
  chỉ nhận direct socket peer hiện tại, còn thứ tự hop thực tế chờ topology sign-off.

### PR-02.3 Regression matrix

- [x] Direct HTTP/HTTPS, trusted proxy HTTPS/HTTP, untrusted proxy và malformed
  forwarded header.
- [x] Assert `Secure`, `HttpOnly`, `SameSite`, HSTS và CSRF behavior.
- [~] Có regression login/logout và thay thế session state; kiểm tra rotation ở
  mức cookie cần bổ sung trong browser/e2e matrix.

### Acceptance PR-02

- [x] Production không thể phát session cookie thiếu `Secure`.
- [x] Forwarded HTTPS chỉ có hiệu lực từ trusted proxy.
- [x] Client trực tiếp không bật được secure-origin behavior bằng header giả.
- [~] Security regression pass trên Python 3.11 và 3.12; Python 3.11 đã pass,
  Python 3.12 chưa có runtime/CI artifact trên server.

### Evidence PR-02 hiện tại

- Code/test commit: `c915132286fb3b743b2455179f6247698ae5fa0a`.
- `tests/test_production_readiness.py`: `19 passed, 1 warning`.
- Security/auth regression: `tests/test_production_readiness.py
  tests/test_dashboard_auth.py tests/test_security_audit.py tests/test_nl_security.py`:
  `44 passed, 1 warning` trên Python 3.11.
- Coverage của regression: production/staging Secure session cookie; explicit
  HttpOnly/SameSite/max-age; trusted forwarded HTTPS bật HSTS và Secure CSRF;
  direct/untrusted/malformed forwarded headers không được tin.
- Còn chờ: xác nhận topology reverse proxy/IP thật, browser cookie rotation và
  chạy lại matrix Python 3.12.

## 6. Workstream P0/P1 — container, SSH key và code-repair isolation

### PR-03.1 Inventory và owner matrix

Tạo artifact không chứa secret cho từng service:

| Service | User/group | Root FS | Writable mounts | SSH key | Egress | Runtime owner |
|---|---|---|---|---|---|---|
| Dashboard | non-root | read-only | state tối thiểu | không nếu không cần | API/DB | canonical |
| Worker | non-root | read-only | queue/state | scoped key | Ceph/broker | canonical |
| Watcher | non-root | read-only | cache/state | scoped key | Ceph/broker | canonical |
| full-executor | `aiagent` | read-only | artifact/state | capability key | allowlist | worker-owned |
| code-repair | isolated identity | explicit write | no prod secret | none default | no Ceph mutation | approval-only |

- [x] Xác nhận bằng `id`, `/proc/1/status`, mount/capability inspection và
  container security options; không chỉ đọc compose.
- [~] Dashboard/Worker/Watcher không chạy root; full-executor, Telegram và
  vault-monitor cũng đã chạy UID `10001`. Code-repair vẫn là root exception vì
  đang ghi trực tiếp checkout, chưa có risk owner/expiry được ký.
- [~] SSH private key `/root/.ssh` không còn mount vào app services; bootstrap
  copy key vào `/var/lib/ceph-ai/full-executor-ssh` với owner `10001`, mode `0600`,
  và các app dùng path này. Phạm vi host/capability và tách UID giữa app services
  vẫn cần hoàn thiện.
- [ ] Code-repair tách khỏi runtime image và production credentials; repository
  write phải qua explicit operator-approved job.
- [ ] Xóa `curl | sh`; tải artifact tạm, verify checksum/signature rồi install.
- [ ] Egress deny-by-default tới đúng Ceph, PostgreSQL, RabbitMQ và provider.

### Evidence PR-03 hiện tại

- Runtime/code commit: `85ddf8cde5cd7ae6ffdb06e9db3d94184a6a0220`.
- `podman inspect`/`podman top` trên `10.3.55.213` xác nhận Dashboard, Worker,
  Watcher, Telegram, full-executor và vault-monitor chạy `10001:10001`, rootfs
  read-only, `no-new-privileges`, `cap_drop=ALL`; code-repair chạy root nhưng
  rootfs read-only và chỉ checkout bind mount writable.
- Sau recreate từng service: Dashboard, Worker, Watcher, Telegram, full-executor
  và code-repair đều `healthy`; vault-monitor chạy không có healthcheck. Worker
  và Watcher tiếp tục SSH authenticate thành công; Telegram không còn
  `PermissionError` với state JSON sau ACL mask fix.
- Credential mount trực tiếp `/root/.ssh` đã bị loại khỏi Dashboard/Worker/Watcher/
  Telegram; runtime key được provision bởi `scripts/bootstrap_container_config.py`.
- Còn chờ: code-repair workspace/approval isolation, egress policy, xóa installer
  `curl | sh`, scope SSH theo capability/UID và root-exception sign-off.

### Acceptance PR-03

- [~] Container audit chứng minh non-root cho các service runtime chính,
  `no-new-privileges`, capability tối thiểu và rootfs/mount policy; code-repair
  root exception và secret/egress scope chưa đóng.
- Prompt injection/path traversal/command injection/secret exfiltration tests pass.
- Kill/restart không để lại process hoặc owner trùng.
- Root exception (nếu bắt buộc) có risk owner và expiry.

## 7. Workstream P1 — CI quality và release evidence

### PR-04.1 CI matrix

- [~] Matrix Python 3.11 + 3.12 cùng test addopts đã được khai báo; chưa có
  GitHub Actions artifact xác nhận cả hai version trên commit này.
- [x] Pin Node 20.x và dùng `npm ci` với `ceph-health-dashboard/package-lock.json`.
- [~] Unit và frontend build là required qua `test`; RabbitMQ service đã khai báo
  nhưng integration vẫn bị pytest addopts loại, migration/security chưa thành
  required checks riêng.
- [~] Workflow không dùng credential runtime; dependency/cache còn do
  setup action/registry cung cấp và cần xác nhận trong Actions run.
- [x] `live` và destructive tests vẫn manual/approval-only, không vào default CI.

### PR-04.2 Static/security gates

- [~] Ruff đã thêm với version pin và xuất SARIF; hiện report-only để đo baseline,
  chưa chặn issue mới ở changed-files.
- [~] mypy đã thêm với version pin và JUnit; chưa có config strict/baseline gate.
- [~] Bandit, `pip-audit` và `npm audit` đã thêm; image scan/Trivy và SBOM còn thiếu.
- [~] SARIF/JUnit/test evidence đã upload theo SHA; coverage, SBOM và image digest
  chưa được xuất.
- [ ] Security/dependency/changed-path type regressions luôn block release.

### PR-04.3 Release report

- [ ] Report gồm SHA, Python/Node, dependency hash, Alembic head, test counts,
  deselected, failures, warnings, coverage, scans và image digest.
- [ ] Report không chứa secret hoặc environment dump nguyên bản.
- [ ] Full suite chạy hai lần; flaky test phải fail gate hoặc có waiver hết hạn.

### Acceptance PR-04

- Required checks xanh trên 3.11/3.12.
- Không có required static/security job bị bỏ qua.
- Warning budget và scan baseline được lưu; warning mới vượt budget làm CI fail.

### Evidence PR-04 hiện tại

- CI/tooling commit: `91e7ae03da6a07c420e9c7555beb21a549ec792f`.
- Workflow đã có Python `3.11`/`3.12`, Node `20.x`, `npm ci`, JUnit test artifacts
  và quality/security artifact upload.
- Local server validation: workflow YAML parse pass; Python 3.11 security/auth
  regression `44 passed, 1 warning`; `cryptography==50.0.1`; sau nâng pip/
  setuptools, `pip-audit` trả `No known vulnerabilities found`.
- Baseline trước khi gate: Ruff trên file thay đổi pass; Bandit quét hai file
  thay đổi có baseline findings và toàn repo chưa được chặn; GitHub Actions 3.12
  chưa có artifact trên server.
- Còn chờ: chạy Actions thật cả hai Python, chuyển report-only thành required
  gates sau khi xử lý baseline, thêm Trivy/SBOM/coverage/release report.

## 8. Workstream P1 — time and warning hygiene

### PR-05.1 Time model

- [~] Đã tạo helper UTC tập trung và migrate production call sites; helper hiện
  trả naive UTC để tương thích các cột legacy, nên schema/model timezone-aware
  chưa hoàn tất.
- [~] Inventory production `dashboard config shared watcher worker` hiện còn
  `0` call `datetime.utcnow()`; tests, migration và `.codex-stage` chưa migrate.
- [~] Serialization hiện giữ contract legacy naive-UTC; conversion dữ liệu cũ và
  API/frontend timezone contract vẫn cần một migration riêng.
- [~] Regression nhóm model/watcher/backup/action cùng security/auth đạt `124
  passed, 1 warning`; DST/timezone non-UTC và full RPO/RTO matrix chưa chạy.

### PR-05.2 Warning budget

- [~] Đã tách được warning focused hiện tại: regression 44/124 test chỉ còn một
  warning deprecation từ Starlette/httpx; full-suite category report chưa rerun
  sau migration clock.
- [ ] Application warning mới là error trong CI; dependency warning có owner,
  upstream issue/version và expiry date.
- [x] Không thêm blanket `filterwarnings` để che warning logic.
- [ ] Release report có warning theo category, không chỉ tổng số.

### Acceptance PR-05

- [~] Không còn trực tiếp gọi `datetime.utcnow()` trong production path đã
  migrate; helper/schema vẫn đang ở compatibility mode naive UTC.
- [~] Focused warning count đã giảm còn một dependency deprecation; full-suite
  warning budget và production blocker chưa được bật.
- Warning còn lại có owner và ngày xử lý.

### Evidence PR-05 hiện tại

- Clock migration commit: `466e7e81fef7a49a830d113982072502e436720a`.
- `grep` trên production package dirs: `0` direct `datetime.utcnow()` call sites;
  helper `shared/time.py` dùng `datetime.now(timezone.utc)` rồi giữ naive-UTC
  compatibility representation.
- Regression sau migration: `124 passed, 1 warning` trên Python 3.11; focused
  production-readiness/auth/security: `44 passed, 1 warning`.
- Còn chờ: đổi DB columns/API contract sang timezone-aware, migrate tests/fixtures,
  chạy full suite hai lần, phân loại warning toàn bộ và biến warning budget thành
  required CI gate.

## 9. Workstream P1 — tài liệu và operator contract

### PR-06.1 Sửa mâu thuẫn discovery

- [x] Chốt `CEPH_RBD_POOLS` rỗng nghĩa là auto-discovery; thêm
  `CEPH_RBD_AUTO_DISCOVERY_ENABLED=false` để tắt rõ ràng.
- [~] README, `.env.example`, settings help text và Plan đã đồng bộ; startup log
  explicit về effective policy và tải dự kiến vẫn cần bổ sung.
- [~] Code/documentation đã ghi SSH query và RBD application filtering; inventory
  per-image, timeout và tải theo cluster cần đưa vào operator runbook.
- [x] Regression test pin manual-list precedence, auto-discovery mặc định, disable
  path và discovery failure fallback.

### Evidence PR-06.1 hiện tại

- Commit: `605fff56fd77c0cb6d12239ef16cebd807b169b3`.
- `tests/test_ceph_client.py -k configured_rbd_pools`: `5 passed, 124 deselected,
  1 warning` trên Python 3.11.
- Hành vi thống nhất: danh sách explicit luôn thắng; danh sách rỗng auto-discovers
  RBD application pools khi flag mặc định `true`; flag `false` trả empty và không
  tạo SSH discovery call.
- Còn chờ: startup effective-policy log và runbook định lượng SSH/query load.

### PR-06.2 Release manifest

- [ ] Manifest ghi observed SHA, rollback SHA, image digest, migration backup/
  rehearsal, runtime owner, canary cluster, approval và residual risk.
- [ ] Không đánh dấu deployment approved khi runtime owner hoặc migration rehearsal
  còn `PENDING`.

## 10. Workstream P1 — feature acceptance còn mở

Các mục dưới đây chỉ được đánh dấu hoàn thành khi có evidence runtime; endpoint/UI
đơn thuần không đủ.

### PR-07.1 Browser/multi-tab

- [~] Đã cài Chromium Playwright và chạy canary local không-auth trên 1/5/10 tab:
  login/product-selection trả HTTP 200, p95 lần lượt 813/921/883 ms; HAR được
  ghi tại `/tmp/pr07-browser.har`. Chưa thể đánh dấu auth acceptance vì server
  chỉ có password hash và chưa có credential test/operator được cấp; lần thử
  `admin/admin` bị giữ ở `/login`.
- [ ] Đo p50/p95/p99, reconnect, stale badge, duplicate request và Ceph/SSH query
  count bằng DevTools/server metrics.
- [~] Canary scope/timeout đã cố định ở localhost, 20s navigation timeout và
  chỉ GET sau login; trace/video khi fail và auth hợp lệ còn chờ credential.

### PR-07.2 Cinder mapping

- [~] Contract/controller fixture acceptance đã pass: volume/project,
  attachment/instance, orphan, missing/insufficient evidence, snapshot,
  reconciliation và approval-gated mutation.
- [~] Pagination bounded (`page_size <= 20`), cluster scope và two-way
  mapping đã có test; live controller, tenant-isolation với OpenStack policy
  thật và eventual-consistency window còn chờ staging credential/controller.
- [x] Mapping endpoint là read-only; attach/detach/snapshot mutation chỉ tạo
  typed approval action và không có direct mutation path. Evidence:
  `tests/test_cinder_discovery.py`, `tests/test_cinder_reconciliation.py`,
  `tests/test_cinder_mapping_api.py`, `tests/test_dashboard_openstack.py`,
  `tests/test_dashboard_volumes.py` — `177 passed, 1 warning`.

### PR-07.3 DR/RBD mirroring

- [~] Read-only mirror info/status query và pool/cluster scoping đã có contract
  test; peer setup/teardown chưa chạy vì không có disposable peer được cấp và
  không được chạm production peer.
- [~] Mirror telemetry hiện trả raw Ceph info/status và xử lý disabled/error
  fail-closed; lag/RPO normalization, planned failover/failback, fencing và
  split-brain behavior chưa có implementation/runtime evidence.
- [~] Restore/integrity/rollback/operator-confirmation contracts đã được test
  trong bộ DR/backup; checksum và recovery point live vẫn chờ disposable DR.
  Evidence hiện tại: mirror client tests và `175 passed, 1 warning` từ bộ
  restore/backup/RGW/AI acceptance.

### PR-07.4 Backup multi-cluster

- [~] Backup/restore/digest/anomaly/restore-drill contract và secret-redaction
  tests đã pass; các route đã mang cluster scope ở fixture hiện có.
- [~] Policy per-cluster và inactive rejection có nhánh code/test, nhưng chưa
  có audit evidence API trên hai cluster thật cùng lúc.
- [~] Parallel backup hai cluster và live multi-cluster audit còn chờ staging
  controller/storage; chưa dùng singleton global làm bằng chứng pass.

### PR-07.5 RGW monitoring/remediation

- [~] RGW evidence, audit, quota/diagnosis và multisite fixture tests đã pass;
  metric names/labels/cardinality/retention cần review trên Prometheus thật.
- [~] Guardrail hiện giữ remediation typed/approval/audit và redacts evidence;
  end-to-end RGW remediation, idempotency/post-check/rollback runtime chưa có.
- [~] Dedupe/failure/no-action branches có coverage fixture; silence/retry và
  canary Prometheus/RGW evidence còn chờ staging.

### PR-07.6 AI post-check/rollback

- [~] Typed action policy, bounded executor/post-check, idempotency/lease và
  Ceph/RBD reconciliation contracts đã có test; chưa chứng minh fresh telemetry
  và health floor cho mọi action trên runtime thật.
- [~] Worker crash/timeout/reconcile branches có fixture coverage, nhưng chưa
  có kill/restart soak evidence với command orphan trên staging.
- [~] Thiếu inverse rollback đã fail-closed trong registry/policy tests; operator
  runbook và full action registry audit còn mở.
- [~] Failure injection post-check/rollback/partial-success có coverage trong
  bộ `175 passed`; chưa có live worker failure drill.

## 11. Workstream P0/P1 — staging, rollback và DR evidence

### PR-08.1 Staging identity và ownership

- [ ] Gán staging, canary, production cluster ID và owner; không dùng tên mơ hồ.
- [ ] Ghi `autonomy_environment`, feature flags, allowed actions, maintenance
  window và data isolation.
- [ ] Chọn một runtime owner duy nhất giữa Podman/systemd; disable legacy owner
  sau khi inventory và rollback path được lưu.

### PR-08.2 Rehearsal

- [ ] Database backup → migration upgrade → health/API/browser smoke → rollback
  migration/app → restore backup → verify data and audit.
- [ ] Backup restore vào scratch, checksum/size verification, đo RPO/RTO.
- [ ] Kill Worker/Watcher/Dashboard ở nhiều phase; chứng minh resume/reconcile
  không chạy duplicate action.
- [ ] Rollback bằng image digest + app SHA, không dùng `latest`.

### PR-08.3 Soak và live DR

- [ ] Staging soak tối thiểu một maintenance cycle, có p95/p99, warning, error,
  memory, disk, queue depth, duplicate owner và alert flood report.
- [ ] Live DR drill chỉ sau safety approval: isolated target, backup chain, RBD
  mirror/restore, fencing, failover, failback, measured RPO/RTO và cleanup.
- [ ] Không gọi live DR “pass” nếu chỉ có unit/scratch evidence.

### Acceptance PR-08

- Có rehearsal report, operator witness, timestamps, artifacts và residual risks.
- Rollback thành công trong target time.
- DR report có measured RPO/RTO và không có data loss/cross-tenant leak chưa giải thích.

## 12. Release gates và điều kiện dừng

### Gate A — RC code quality

- PR-01, PR-02 và PR-04 pass trên Python 3.11/3.12.
- Alembic một head; upgrade/downgrade/re-upgrade pass trên disposable DB.
- Ruff/type/security/dependency/image scan không có blocker chưa waive.

### Gate B — Staging approved

- PR-03, PR-05, PR-06 và PR-08.1/08.2 pass.
- Browser smoke, backup restore và rollback rehearsal pass.
- Runtime owner duy nhất, SSH/mount/egress audit đạt.

### Gate C — Production canary

- Operator/security approval bằng văn bản.
- Canary cluster và blast-radius limit được ghi rõ.
- Autopilot disabled; chỉ allowlist SAFE/approval-required.
- Có live monitoring, kill switch và người trực trong maintenance window.

### Gate D — mở rộng production

- Canary soak đạt SLO, không có critical alert, audit loss, duplicate action,
  cross-cluster leak hoặc unexplained warning.
- DR/rollback evidence còn hiệu lực và manifest cập nhật SHA mới.
- Chỉ sau Gate D mới xem xét LIMITED_AUTOPILOT; production destructive
  remediation vẫn cần approval riêng.

Dừng release ngay khi có full-suite failure, migration mismatch, security/cookie
regression, secret exposure, duplicate runtime owner, cross-cluster leak, failed
rollback, inconclusive post-check, alert flood, RPO/RTO vượt ngưỡng hoặc manifest
thiếu approval.

## 13. Evidence và handoff template

Mỗi task ghi:

```text
ID: PR-xx.y
Owner:
Commit SHA:
Environment / cluster:
Commands:
Result:
Test counts / warnings / duration:
Artifacts:
Rollback:
Residual risk:
Approval / expiry:
```

## 14. Definition of Done toàn plan

- Full suite deterministic trên Python 3.11 và 3.12, không còn failure phụ thuộc order.
- Session cookie và forwarded HTTPS đúng theo trusted proxy boundary.
- Runtime/container/SSH/code-repair blast radius được đo và giới hạn.
- CI có test matrix, static/type/security/dependency/image gates và warning budget.
- Time/warning contract và `.env.example`/README thống nhất.
- Tất cả feature còn mở có evidence runtime hoặc deferred có owner/lý do.
- Staging rollback, backup restore, browser smoke và DR rehearsal có report.
- Manifest có SHA, migration, runtime owner, rollback, residual risk và sign-off.
- Không bật autonomous production remediation trước khi các gate tương ứng pass.
