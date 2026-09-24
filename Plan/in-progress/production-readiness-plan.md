# Production Readiness Plan — Ceph-AI

Tài liệu này theo dõi các blocker phát hành phát hiện trong review commit
`ddc4de7` và đối chiếu với `origin/main` hiện tại. `ddc4de7` không còn là
`origin/main` mới nhất; mọi kết luận phải ghi rõ commit hoặc workflow được kiểm
tra để tránh trộn kết quả giữa các snapshot.

## Quy tắc phát hành

- `[ ]` Chưa làm hoặc chưa có bằng chứng nghiệm thu.
- `[~]` Đang xử lý hoặc mới có một phần bằng chứng.
- `[x]` Đã sửa, có test hồi quy và workflow deploy thành công.
- Không phát hành production khi còn blocker P0/P1, dù unit test tổng thể đang
  xanh.
- Số lượng test, warning và static-analysis finding phải đi kèm log hoặc
  artifact có commit SHA; không dùng số liệu cũ để kết luận cho code mới.

## Phạm vi execution wave hiện tại — CI để sau

Wave này chỉ xử lý blocker trong mã nguồn, runtime safety và AI promotion
boundary. Không sửa workflow CI, không pin dependency/image/action và không
chạy lại release pipeline trong wave này. Các mục đó vẫn là release gate bắt
buộc, nhưng được theo dõi ở phần **Deferred CI/supply-chain work**.

Thứ tự thực hiện:

1. Đồng bộ cache metrics contract và làm full test suite xanh.
2. Khóa backup application hook bằng allowlist và security regression.
3. Chặn hoàn toàn bulk bucket deletion legacy và hoàn tất flow preview/recheck/
   confirmation/audit.
4. Sửa Patch runtime và thêm regression test cho nhánh lịch sử có `log_excerpt`.
5. Đổi fingerprint SHA-1 của Performance RCA sang SHA-256/BLAKE2, có kiểm tra
   tương thích event/dedupe.
6. Sửa operational alert từ counter tích lũy sang trạng thái có time window và
   recovery.
7. Hoàn thiện AI self-learning ADWIN/state, scope boundary, shadow → candidate → active →
   rollback và production acceptance; vẫn giữ `SHADOW_ONLY` trong suốt wave.

### Implementation status — 2026-09-21

Đã triển khai một phần trong worktree sạch `/home/vc/ceph-ai-implementation`
(dựa trên `origin/main`); worktree người dùng `/home/vc/ceph-ai` vẫn được giữ
nguyên vì đang có thay đổi chưa commit.

- `[~]` Cache metrics contract: metrics hiện tính mọi regular non-symlink file,
  gồm `.lock`; đã thêm regression cho file lạ, symlink và directory.
- `[~]` Backup application hook: chuyển sang `hook_id` allowlist, manifest
  root-owned/checksum, reject shell và raw command; đã thêm security tests.
- `[~]` RGW bulk delete: endpoint one-click trả `410`; flow mới có preview,
  inventory hash/count/size, HMAC token TTL, exact cluster confirmation,
  recheck và audit; mặc định `rgw_delete_all_enabled=false`.
- `[~]` Patch: thêm `import re` và regression cho `log_excerpt` history.
- `[~]` Performance RCA: fingerprint/code dùng SHA-256 v2 và migrate identity
  SHA-1 cũ để tránh duplicate incident.
- `[~]` Operational alert: thêm failure window 15 phút, timestamps và
  `UNKNOWN/ACTIVE/RECOVERED` recovery state.
- `[~]` AI forecast: sửa ADWIN newest-window ordering, truyền đủ runtime scope,
  và đổi tên Candidate D thành robust multivariate z-score.

Validation hiện có: targeted regression đạt `137 passed, 13 warnings`; full
suite chạy được `3993 passed, 16 deselected, 1492 warnings` trước khi bị
interrupt ở test SSH cuối do Paramiko chờ kết nối. `compileall`, `F821/F811`
trên các file wave và `git diff --check` đều đạt. Vì full suite chưa hoàn tất,
CI/deploy chưa chạy và supply-chain gate vẫn deferred, các mục trên chưa được
đánh dấu hoàn tất; quyết định phát hành vẫn là **NO-GO**.

## Snapshot đánh giá mới nhất — `ddc4de7`

- **Điểm tổng thể:** `6.8/10`; quyết định phát hành: **NO-GO**.
- **Test:** `1 failed, 4038 passed, 16 deselected`; lỗi tại
  `tests/test_ceph_query_cache.py::test_storage_metrics_are_bounded_and_include_file_sizes`.
- **Static/complexity:** 276 Ruff, 901 mypy và 181 C901 theo báo cáo review;
  đây là baseline quan sát, chưa phải bằng chứng để miễn lỗi mới.
- **Security:** còn 1 Bandit High do SHA-1; delete-all và backup application
  hook vẫn là rủi ro P0/P1.
- **AI:** ADWIN policy/state, bounded replay và Phase 6 paired-evaluation /
  promotion service đã có trong forecast path; drift ghi confidence/status/
  promotion block vào evidence. Online consumer vẫn ghi cứng `target="shadow"`;
  production wiring, soak và active promotion vẫn mở.
- **Workflow evidence:** run `35576037223` thất bại trên Python 3.11/3.12;
  run `35578062186` không được dùng làm evidence hoàn tất nếu chưa có kết quả
  cuối cùng.

## Blocker P0 — thao tác phá hủy

- [~] **Object Storage bulk delete**: hoàn thành mục `3.7 Bulk bucket deletion
  hardening` trong `Plan/object-storage-roadmap.md`.
  - Endpoint legacy không được purge chỉ vì nhận POST của admin.
  - Bắt buộc preview, inventory hash/count, confirmation token, recheck và
    audit đầy đủ.
  - Có test chứng minh request trực tiếp, confirmation sai và inventory stale
    đều không xóa dữ liệu.

## Blocker P0 — test contract đang đỏ

- [~] **Đồng bộ contract storage metrics của `shared/ceph_query_cache.py` và
  test.**
  - Chốt rõ `.lock` là file được tính trong bounded storage metrics hay là
    runtime artifact bị loại trừ.
  - Nếu loại trừ `.lock`, cập nhật test và tài liệu contract; nếu tính, sửa
    collector để tính đúng size mà không phá giới hạn tổng dung lượng.
  - Thêm test cho `.json`, `.lock`, file lạ, symlink, directory và giới hạn
    tổng size/file count; không thay đổi behavior chỉ để làm test xanh.
  - Chạy targeted test và full suite sau khi contract được chốt. Đây là code/test
    consistency work, không phải CI workflow work.

## Blocker P0 — backup application hook / RCE

- [~] **Khóa application-consistency hook, không cho cấu hình command tùy ý.**
  - Thay `command` array tự do bằng `hook_id` thuộc allowlist cố định trong
    application code/policy.
  - Hook script phải là file root-owned, quyền ghi bị giới hạn, có checksum
    hoặc signature được kiểm tra trước khi chạy.
  - Từ chối tuyệt đối `/bin/sh`, `/bin/bash`, `-c`, `--command` và mọi shell
    interpreter; `shell=False` không đủ nếu argv vẫn gọi được shell trực tiếp.
  - Giữ timeout, output redaction, environment allowlist, audit và fail-closed
    khi hook không hợp lệ.
  - Thêm test cho hook hợp lệ, unknown hook, shell interpreter, path traversal,
    checksum mismatch, timeout và output chứa secret.

## Blocker P1 — lỗi runtime đã xác minh

- [~] **Patch route thiếu `import re`** trong `dashboard/routes/patch.py`.
  - Thêm import rõ ràng.
  - Thêm test có `Incident.log_excerpt` không rỗng để chạy qua nhánh
    `re.search()` trong `_patch_history()`.
  - Chạy lại toàn bộ `tests/test_dashboard_patch.py` và các route sử dụng
    patch history; lưu JUnit XML cùng commit SHA.
  - Chạy compile và targeted quality check trên clean checkout; không coi
    `py_compile` là đủ vì undefined name chỉ lộ ra lúc chạy nhánh đó.
  - Việc bắt `F821` trên toàn cây được chuyển sang deferred CI wave; fix mã và
    regression test vẫn phải hoàn thành trong wave hiện tại.
  - Đóng implementation khi test hồi quy đạt; release gate vẫn mở cho tới khi
    CI/deploy wave sau hoàn tất.

## Blocker P1 — fingerprint và operational alert

- [~] **Đổi fingerprint SHA-1 của Performance RCA** trong
  `watcher/performance_rca_monitor.py` sang SHA-256 hoặc BLAKE2.
  - Kiểm kê toàn bộ fingerprint production dùng cho dedupe/event identity;
    không đổi âm thầm các identity đang được dùng trong audit/outbox.
  - Chốt format/version mới, ví dụ `perf-rca:v2:<sha256>`, và xử lý tương thích
    với event/fingerprint cũ để không gửi lại hàng loạt alert hoặc làm mất dedupe.
  - Thêm test ổn định digest, khác payload tạo fingerprint khác và không chứa
    credential/raw payload trong event ID.
  - Bandit/CI toàn cây thuộc deferred wave; implementation vẫn phải sạch ở các
    test fingerprint liên quan.

- [~] **Sửa operational alert event-bus publish failure**.
  - Giữ `publish_failure_total` như telemetry tích lũy, nhưng không dùng nó
    một mình làm điều kiện alert đang hoạt động.
  - Bổ sung `last_failure_at`, `last_success_at`, số lỗi trong cửa sổ bounded
    5–15 phút và trạng thái `ACTIVE`/`RECOVERED`/`UNKNOWN`.
  - Chỉ mở alert khi có lỗi trong cửa sổ; chỉ chuyển `RECOVERED` sau publish
    thành công sau lỗi. Sau process restart thiếu state phải trả `UNKNOWN`,
    không tự kết luận healthy.
  - Thêm test: một lỗi rồi recovery, lỗi lặp trong window, lỗi ngoài window,
    success trước mọi failure và restart/mất state.
  - Dashboard/API phải hiển thị thời điểm lỗi gần nhất, thời điểm recovery và
    window đang dùng; không lộ payload event hoặc credential.

## Blocker P1 — CI/CD và deploy

- [ ] Điều tra workflow deploy thất bại: lưu job log, exit code, commit SHA,
  target host và phase lỗi vào artifact hoặc release note.
  - Phân loại failure theo `test`, `quality`, `build`, `deploy`, `migration`,
    `smoke` và `rollback`; không ghi chung là “deploy failed”.
  - Artifact tối thiểu phải gồm workflow/run ID, commit SHA đã test, commit
    SHA thực sự deploy, thời điểm bắt đầu/kết thúc, target host, exit code,
    stdout/stderr đã redact và trạng thái service sau failure.
  - Nếu SSH/deploy script thất bại giữa chừng, phải chứng minh checkout,
    migration, container/service và static asset đang ở trạng thái nào trước
    khi cho phép retry.
- [ ] Chạy lại một workflow từ commit ứng viên sau khi sửa; chỉ đóng mục khi
  cả test, quality gate và deploy đều thành công.
- [ ] Kiểm tra deploy idempotency: chạy lại sau failure phải tiếp tục an toàn,
  không tạo migration/service/container trùng và không làm mất dữ liệu.
- [ ] Có smoke test sau deploy: dashboard health, Worker, Watcher, migration
  head, API đăng nhập, read-only Ceph status và ít nhất một flow Object Storage.
  - Smoke test phải chạy trên đúng target đã deploy và ghi response/status,
    service health, migration head và release SHA vào artifact; không dùng
    kết quả từ một môi trường khác để đóng blocker.

## Quality gate và nợ kỹ thuật

- [ ] **Static-analysis gate:** chạy Ruff với tối thiểu `F821,F811` và
  security scanner trên clean checkout; lỗi mới trên diff phải là `0`.
  - Baseline chỉ được dùng để theo dõi finding cũ đã được ghi nhận theo
    `commit SHA + file + rule + line/fingerprint`; không được dùng baseline
    để miễn finding mới, finding bị di chuyển hoặc lỗi runtime tương đương.
  - PR không được sửa baseline cùng lúc với code nếu không có review riêng;
    mọi thay đổi baseline phải nêu owner, lý do và ngày hết hạn.
- [ ] **Coverage gate:** tạo coverage report XML/HTML trên môi trường sạch,
  gắn với commit SHA và test command. Gate ban đầu là coverage tổng thể không
  thấp hơn baseline release gần nhất và không thấp hơn 80%; các critical paths
  Object Storage destructive flow, Patch, executor và AI promotion phải đạt
  tối thiểu 90% branch/line coverage theo metric đã chốt.
  - Nếu baseline chưa tồn tại hoặc không truy nguyên được, gate là **fail**;
    không được tự tạo baseline từ một run thiếu test/service.
  - Test flake, test bị skip do service ngoài và test fail thật phải là ba
    nhóm riêng trong artifact; skip không được tính là coverage đạt.
- [ ] Tạo backlog có owner cho các lỗi Ruff/mypy hiện hữu; ghi số liệu theo
  commit và không biến baseline thành cơ chế che lỗi runtime.
- [ ] Chạy quality gate ở cả PR và release candidate; lưu test count, warning,
  F821/F811, security findings, coverage và baseline diff trong một artifact
  duy nhất để reviewer đối chiếu được cùng snapshot.
- [ ] Chạy test có kiểm soát giữa môi trường sạch và môi trường production;
  phân loại rõ test flake, test phụ thuộc service ngoài và test thực sự fail.

## Supply-chain và reproducibility

- [ ] Thêm Python lockfile được review và cập nhật có kiểm soát; chọn một
  nguồn chuẩn (ưu tiên `uv.lock`) và dùng lockfile trong CI/release thay vì
  `pip install -e .[dev]` không có resolver snapshot. Node dependency phải
  tiếp tục dùng `ceph-health-dashboard/package-lock.json` với `npm ci`.
  - Mỗi lockfile update phải có diff review, dependency audit và release note
    nêu lý do nâng/hạ phiên bản.
- [ ] Pin mọi Docker/OCI image bằng digest thay vì tag mutable, bao gồm image
  service RabbitMQ trong CI và mọi image runtime/sidecar được deploy; ghi
  registry, image name, digest và thời điểm resolve vào release artifact.
- [ ] Pin mọi GitHub Action bằng full commit SHA, không dùng `@v4`, `@v5`,
  `@main` hoặc tag mutable; action update phải có review và audit riêng.
- [ ] Lưu SBOM (CycloneDX hoặc SPDX) cho Python, Node và OS/container package
  nếu có; kèm `pip-audit`/npm audit result, license/security exceptions và
  commit/release SHA. Thiếu SBOM hoặc audit artifact là chưa đạt
  reproducibility gate.

## AI self-learning release gate

- [~] Hoàn thành các mục review follow-up trong
  `Plan/ai-self-learning-online-implementation-plan.md`; nền tảng, shadow
  canary và operator controls đã có, nhưng active-promotion wiring, E2E và
  long-running production acceptance còn mở.
- [x] Chứng minh `SHADOW_ONLY` không thể cập nhật active model hoặc chạy
  remediation; runtime hiện giữ `can_update_active=false`.
- [ ] Chứng minh active promotion chỉ xảy ra sau approval, quality/drift/resource
  gate và có rollback evidence production; hiện chưa có candidate được promote.
- [~] Kiểm tra runtime scope theo `cluster + host + metric` ở Watcher, Worker và
  Dashboard; canary guard/UI đã có, nhưng caller audit và regression test riêng
  cho node forecast vẫn chưa hoàn tất.
- [ ] Chưa mở rộng canary hoặc bật `ACTIVE` cho tới khi có verified outcome,
  production rollback acceptance và operator approval riêng.

### AI self-learning execution wave

- [x] Hoàn thiện ADWIN policy nền: 512 run mới nhất theo thời gian, state JSON
  có checksum theo scope, fail-closed khi hỏng, residual/MAE/metric drift,
  confidence multiplier, hysteresis, warm-up và replay so sánh detector cũ.
- [x] Hoàn thiện paired evaluation/promotion primitive: đủ metric MAE/RMSE/
  SMAPE/bias/p95/coverage/recall/delay, gate theo scope/horizon, approval có
  expiry, append-only audit và rollback sau health verification.
- [~] Hoàn tất caller scope audit: mọi call tới `shared.learning_runtime.evaluate()`
  truyền đủ `cluster_id`, `host` và `metric`; thiếu identity trong canary phải
  fail-closed.
- [ ] Nối consumer với promotion decision/model registry; bỏ đường cập nhật
  production bị cố định ở `target="shadow"`, nhưng mặc định vẫn phải là
  `SHADOW_ONLY`.
- [ ] Thêm E2E cho shadow-only, candidate bị từ chối khi xin active, approved
  candidate cập nhật active và rollback có append-only evidence.
- [ ] Hoàn thiện champion–challenger: deterministic ensemble là champion, River
  là candidate; đánh giá rolling nhiều horizon bằng MAE/SMAPE, coverage,
  false-positive và lead time.
- [ ] Nối drift detector vào promotion/rollback decision; drift hoặc thiếu
  evidence phải chặn promotion và giữ candidate ngoài notification/remediation.
- [ ] Chạy production acceptance 24–72 giờ, xác nhận state sau restart, không
  soft-lockup/poll timeout/DB saturation mới và đo rollback trong thời gian mục
  tiêu. Chưa xin operator approval hoặc mở rộng scope trước khi đủ evidence.
- [ ] Nối runtime caller thật cho các boundary hiện mới được gọi từ test hoặc
  evaluator: `alibi_detect_boundary`, `bandit_sandbox`,
  `mlflow_registry_adapter`, `controlled_action_contract`,
  `delayed_feedback_evaluator`, `remediation_state_machine`, `rollback_planner`
  và `time_series_pipeline`; nếu chưa nối được thì phải gắn nhãn
  `EXPERIMENTAL`, không tính vào production capability.

## Deferred CI/supply-chain work

- [ ] Full-tree F821/F811, Bandit High, mypy baseline, coverage gate và warning
  budget.
- [ ] Sửa `release_gate` để cài dependency Python của dự án trước khi gọi
  Alembic trên clean runner.
- [ ] Sửa parser `scripts/ci/release_gate.py` để đọc đúng `pip-audit` JSON tại
  `dependencies[].vulns[]`; thêm fixture test có vulnerability và fixture sạch
  để tránh báo cáo giả `0 vulnerability`.
- [ ] Deploy workflow xanh, deploy idempotency và post-deploy smoke evidence.
- [ ] Python lockfile, Docker/OCI digest, GitHub Actions full SHA và SBOM.

Các mục trên **không thuộc execution wave hiện tại**, nhưng vẫn là điều kiện
bắt buộc trước khi chuyển quyết định release từ **NO-GO**.

## Release decision

### Hiện tại: NO-GO

Giữ trạng thái **NO-GO** cho production cho tới khi hoàn thành tối thiểu:

1. Bulk bucket deletion hardening.
2. Cache metrics contract và full test suite không còn failure.
3. Patch runtime fix và regression test.
4. Backup application hook allowlist và security regression.
5. SHA-1 fingerprint migration và operational alert recovery state.
6. AI scope/promotion/rollback acceptance theo execution wave.
7. Các mục deferred CI/supply-chain ở trên.

Các số liệu như tổng số test, Ruff, mypy, complexity và warning chỉ được dùng
để đánh giá xu hướng sau khi có artifact gắn đúng commit; không dùng riêng
chúng để thay thế các điều kiện blocker trên.

## Nhật ký review

| Ngày | Phạm vi | Kết quả | Việc tiếp theo |
|---|---|---|---|
| 2026-09-21 | Cập nhật production-readiness gate | Bổ sung blocker Patch thiếu `import re`, evidence bắt buộc cho workflow deploy thất bại, quality gate F821/F811 + coverage/baseline và supply-chain reproducibility (lockfile, digest, Action SHA, SBOM) | Giữ **NO-GO**; tạo artifact baseline/release candidate trước khi xem xét chuyển trạng thái |
| 2026-09-21 | Commit `88605a7` và workflow deploy liên quan | Xác minh bulk delete thiếu confirmation, Patch thiếu `import re`, online learner còn shadow-only; deploy run được dẫn chứng bị fail | Xử lý blocker P0/P1 và cập nhật artifact/test evidence |
| 2026-09-21 | Commit `9e6ffc6` — execution wave ngoài CI | Xác nhận SHA-1 Performance RCA, operational alert dùng counter tích lũy và AI caller/promotion boundary còn thiếu | Làm bulk delete, Patch runtime, fingerprint, recovery state và AI promotion boundary; giữ CI/supply-chain deferred |
| 2026-09-21 | Commit `ddc4de7` — reassessment | Test suite còn `1 failed`; cache contract lệch test; release gate thiếu dependency/parser đúng; backup hook cho phép command array tùy ý; ADWIN window và AI production wiring còn gap | Ưu tiên cache contract, backup hook, delete-all, Patch; bổ sung AI ADWIN/state/promotion plan; giữ CI workflow deferred |
