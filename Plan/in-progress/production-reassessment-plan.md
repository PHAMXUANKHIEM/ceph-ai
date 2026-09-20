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

### PR-01.1 Reproduce và cô lập failure

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

### PR-01.2 Sửa ownership/lifecycle của Incident

- [ ] Rà toàn bộ path tạo incident trong `watcher/main.py`, đặc biệt path commit
  trước khi đọc `incident.id`.
- [ ] Dùng pattern được review: `session.flush()` rồi lấy id trước commit, hoặc
  copy scalar `incident_id` trước commit; không đọc attribute ORM sau commit nếu
  object có thể detached/expired.
- [ ] Không dùng `expire_on_commit=False` để che lỗi toàn cục nếu chưa review
  transaction semantics.
- [ ] Bổ sung regression cho một check, nhiều check đồng thời, publish đủ event,
  rollback transaction và session close/detach.

### PR-01.3 Chặn test-state leakage

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

## 5. Workstream P0 — session cookie và trusted HTTPS

### PR-02.1 Chốt mô hình TLS

- [ ] Ghi rõ TLS termination ở reverse proxy nào và IP/CIDR proxy nào được tin.
- [ ] Production/staging bắt buộc `https_only=True` cho session cookie; local HTTP
  chỉ được dùng khi environment không phải production/staging.
- [ ] `Secure`, `HttpOnly`, `SameSite` và session lifetime phải có startup check.

### PR-02.2 Dùng một trusted-scheme helper

- [ ] Chuẩn hóa helper xác định effective scheme: chỉ dùng `X-Forwarded-Proto`
  khi socket peer thuộc `DASHBOARD_TRUSTED_PROXY_IPS` và giá trị hợp lệ.
- [ ] Request trực tiếp giả mạo forwarded header phải bị từ chối hoặc bỏ qua.
- [ ] CSRF cookie, HSTS, absolute URL, origin check và security header dùng cùng
  helper; không điều kiện rời rạc dựa trực tiếp vào `request.url.scheme`.
- [ ] Nếu có proxy chain, document thứ tự hop và chỉ lấy giá trị đã xác minh.

### PR-02.3 Regression matrix

- [ ] Direct HTTP/HTTPS, trusted proxy HTTPS/HTTP, untrusted proxy và malformed
  forwarded header.
- [ ] Assert `Secure`, `HttpOnly`, `SameSite`, HSTS và CSRF behavior.
- [ ] Test session fixation/rotation sau login và logout.

### Acceptance PR-02

- Production không thể phát session cookie thiếu `Secure`.
- Forwarded HTTPS chỉ có hiệu lực từ trusted proxy.
- Client trực tiếp không bật được secure-origin behavior bằng header giả.
- Security regression pass trên Python 3.11 và 3.12.

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

- [ ] Xác nhận bằng `id`, `/proc/1/status`, mount/capability inspection và
  container security options; không chỉ đọc compose.
- [ ] Dashboard/Worker/Watcher không chạy root nếu không có risk exception.
- [ ] SSH key read-only, owner đúng service user, scoped theo host/capability.
- [ ] Code-repair tách khỏi runtime image và production credentials; repository
  write phải qua explicit operator-approved job.
- [ ] Xóa `curl | sh`; tải artifact tạm, verify checksum/signature rồi install.
- [ ] Egress deny-by-default tới đúng Ceph, PostgreSQL, RabbitMQ và provider.

### Acceptance PR-03

- Container audit chứng minh non-root, `no-new-privileges`, capability tối thiểu,
  mount đúng và không có secret ngoài scope.
- Prompt injection/path traversal/command injection/secret exfiltration tests pass.
- Kill/restart không để lại process hoặc owner trùng.
- Root exception (nếu bắt buộc) có risk owner và expiry.

## 7. Workstream P1 — CI quality và release evidence

### PR-04.1 CI matrix

- [ ] Matrix Python 3.11 + 3.12; cùng dependency constraints và test addopts.
- [ ] Pin Node 20, npm lockfile và image/system package versions.
- [ ] Chạy unit, RabbitMQ ephemeral integration, migration, frontend build và
  security regression thành required checks.
- [ ] Không phụ thuộc proxy/credential/cache ngoài khai báo.
- [ ] `live` và destructive tests manual/approval-only, không vào default CI.

### PR-04.2 Static/security gates

- [ ] Thêm Ruff; trước hết chặn issue mới ở changed-files, sau đó giảm baseline
  khoảng 1.063 issue theo module lớn.
- [ ] Thêm type checker đã pin (mypy hoặc pyright), config strict theo module.
- [ ] Thêm Bandit, `pip-audit`/SBOM, `npm audit` và image scan bằng Trivy hoặc
  scanner tương đương.
- [ ] Xuất SARIF/JUnit/coverage/SBOM theo commit/release artifact.
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

## 8. Workstream P1 — time and warning hygiene

### PR-05.1 Time model

- [ ] Tạo helper UTC timezone-aware duy nhất; model/API/log lưu UTC.
- [ ] Inventory toàn bộ `datetime.utcnow()` trong production code, migration và
  fixture; thay theo nhóm module, không sửa mù toàn repo.
- [ ] Chuẩn hóa serialization/frontend timezone; parse dữ liệu naive cũ theo
  assumption được ghi rõ.
- [ ] Regression cho DST, TTL, cooldown, stale snapshot, retention, forecast và
  backup RPO/RTO ở UTC và timezone non-UTC.

### PR-05.2 Warning budget

- [ ] Phân loại 17.000+ warning thành application, dependency, test và deprecation.
- [ ] Application warning mới là error trong CI; dependency warning có owner,
  upstream issue/version và expiry date.
- [ ] Không dùng blanket `filterwarnings` để che warning logic.
- [ ] Release report có warning theo category, không chỉ tổng số.

### Acceptance PR-05

- Không còn naive timestamp trong production path đã migrate.
- Warning count không tăng; production warning blocker bằng zero.
- Warning còn lại có owner và ngày xử lý.

## 9. Workstream P1 — tài liệu và operator contract

### PR-06.1 Sửa mâu thuẫn discovery

- [ ] Chốt `CEPH_RBD_POOLS` rỗng nghĩa là auto-discovery hay disable. Nếu giữ
  behavior hiện tại, document auto-discovery và thêm biến explicit để tắt.
- [ ] Đồng bộ README, `.env.example`, settings help text, startup log và Plan.
- [ ] Ghi rõ SSH query, pool/image allowlist, timeout và tải dự kiến.
- [ ] Thêm config-doc test kiểm tra semantics nhất quán.

### PR-06.2 Release manifest

- [ ] Manifest ghi observed SHA, rollback SHA, image digest, migration backup/
  rehearsal, runtime owner, canary cluster, approval và residual risk.
- [ ] Không đánh dấu deployment approved khi runtime owner hoặc migration rehearsal
  còn `PENDING`.

## 10. Workstream P1 — feature acceptance còn mở

Các mục dưới đây chỉ được đánh dấu hoàn thành khi có evidence runtime; endpoint/UI
đơn thuần không đủ.

### PR-07.1 Browser/multi-tab

- [ ] Playwright/Chromium smoke với auth hợp lệ trên 1/5/10 tab.
- [ ] Đo p50/p95/p99, reconnect, stale badge, duplicate request và Ceph/SSH query
  count bằng DevTools/server metrics.
- [ ] Có canary scope, timeout và trace/video khi fail.

### PR-07.2 Cinder mapping

- [ ] Controller acceptance với volume/project, attachment/instance, orphan và
  insufficient evidence.
- [ ] Kiểm tra two-way mapping, pagination, tenant isolation và eventual consistency.
- [ ] Không có mutation ngoài approval path; evidence theo cluster/site.

### PR-07.3 DR/RBD mirroring

- [ ] Peer setup/teardown trên disposable cluster, không đụng production peer.
- [ ] Normalize mirror status/lag/RPO, planned failover/failback, fencing và
  split-brain behavior.
- [ ] Test checksum, recovery point, rollback và operator confirmation.

### PR-07.4 Backup multi-cluster

- [ ] Audit API chứng minh cluster scope cho job/history/digest/anomaly/restore.
- [ ] Thiết kế policy per-cluster cho Digest và RestoreDrill; không dùng singleton
  global khi bật cluster phụ.
- [ ] Test parallel backup hai cluster, inactive rejection và secret redaction.

### PR-07.5 RGW monitoring/remediation

- [ ] Review Prometheus metric names/labels/cardinality, retention, quota trend và
  alert threshold.
- [ ] Remediation chỉ typed action, preview/approval, idempotency, post-check,
  rollback và audit; không gửi raw log/secret cho AI.
- [ ] Test alert dedupe, silence, retry, failure và no-action mode.

### PR-07.6 AI post-check/rollback

- [ ] Mọi action contract có timeout bounded, fresh telemetry, fault absence,
  health floor và evidence before/after.
- [ ] Worker timeout không để command mồ côi; lease/idempotency reconcile sau crash.
- [ ] Inverse rollback phải được registry kiểm thử; thiếu inverse thì fail-closed
  và yêu cầu operator runbook.
- [ ] Failure injection cho post-check fail, rollback fail và partial success.

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
