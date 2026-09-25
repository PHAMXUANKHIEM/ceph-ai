# Strict Production Readiness Closure Plan — Ceph AI

**Ngày lập:** 25/09/2026  
**Quyết định hiện tại:** `NO-GO production`  
**Mục tiêu:** đóng các khoảng trống bằng bằng chứng runtime/staging có thể lặp lại, không coi roadmap, unit test hoặc fixture là bằng chứng production.

## 0. Phạm vi và nguyên tắc

Đây là kế hoạch canonical cho các blocker release nghiêm ngặt. Các kế hoạch cũ trong `Plan/in-progress/` vẫn được giữ để truy vết lịch sử, nhưng không được dùng để tuyên bố production-ready nếu chưa có evidence trong kế hoạch này.

Nguyên tắc nghiệm thu:

- Tài liệu, adapter, API hoặc migration chỉ được tính là `IMPLEMENTED`; chỉ command + artifact + môi trường + người chứng kiến mới được tính là `ACCEPTED`.
- Không dùng SQLite thay cho PostgreSQL; không dùng mock RabbitMQ thay cho outage/restart rehearsal.
- Không gọi deployment thành công nếu `deploy` hoặc post-deploy smoke thất bại.
- Không promotion model sang `ACTIVE` khi chưa có verified outcome độc lập và rollback rehearsal.
- Không bật autonomous remediation production trong khi một gate P0/P1 còn mở.
- Giữ nguyên quyền đầy đủ của Single Full theo quyết định vận hành hiện tại. Kế hoạch này không hạ quyền Single Full; chỉ kiểm tra boundary, audit, kill switch và điều kiện môi trường của nó.
- Không xóa hoặc reset các thay đổi chưa commit trong worktree dùng chung. Deployment phải dùng checkout/artifact riêng.

## 1. Bằng chứng đã xác nhận và giới hạn hiện tại

| Hạng mục | Trạng thái đã kiểm tra | Kết luận |
|---|---|---|
| Test Python 3.11/3.12 | Run `36086871922` pass | Chỉ là CI code evidence |
| Integration RabbitMQ | Run `36086871922` pass | Chưa phải process-kill/broker-restart witness |
| Quality/security/image scan | Run `36086871922` pass sau commit `87ac883d` | Image scan đã sạch, chưa đồng nghĩa deploy thành công |
| Release gate | Run `36086871922` pass | Mới là artifact gate |
| Deploy | Run `36086871922` fail | Chưa có release thành công; log phase/exit phải được lưu rõ |
| Run mới `36089126816` | Integration pass; test 3.11/3.12 tại thời điểm lập kế hoạch còn chạy | Chưa kết luận CD cho run này |
| Coverage | Không thấy `pytest-cov`, XML, `--cov` hoặc `cov-fail-under` trong gate | Chưa có coverage gate |
| Static analysis | `scripts/ci/quality_gate.py` chỉ chặn diagnostic mới so với baseline | Legacy debt vẫn còn; cần full-tree report |
| Live tests | Workflow yêu cầu manual `CEPH_AI_RUN_LIVE_TESTS=true` | CI xanh chưa chứng minh Ceph thật |
| Online learning | Default `online_learning_enabled=false`, mode `AUDIT_ONLY`, River v2 chưa active | Chỉ là shadow commissioning |
| River evidence | Review gần nhất ghi nhận 2 verified outcomes và 0 scored outcome | Chưa đủ promotion evidence |
| PostgreSQL/DR | Artifact rehearsal ghi `database_rollback=NOT_RUN` | Chưa có rollback DB/restore acceptance |
| Incident Outbox | Deterministic chaos pass; process-kill/broker-restart witness còn mở | Chưa đóng recovery gate |
| Worktree deploy | `/root/ceph-ai` còn nhiều file modified/untracked | Không được nới dirty guard; phải tách deployment checkout |

## 2. Thứ tự ưu tiên bắt buộc

1. P0 — Làm CD/deploy quan sát được, idempotent và chạy từ checkout sạch.
2. P0 — PostgreSQL restore/migration/rollback và Incident Outbox recovery rehearsal.
3. P1 — Coverage gate, full-tree static-analysis debt và live read-only acceptance.
4. P1 — Credential separation, RBAC/cross-cluster và audit completeness.
5. P1 — Khép kín River v2 shadow → candidate → active → rollback bằng dữ liệu thật.
6. P2 — HA/SLO/RTO/RPO, supply-chain provenance và repository maintainability.

Không được đảo thứ tự để promotion AI hoặc mở autonomy trước khi P0 hoàn thành.

## 3. P0 — Đóng CD/deploy và release identity

### 3.1 Thu thập failure evidence của deploy

- [ ] Sửa workflow để upload deploy stdout/stderr đã redact, exit code, command phase, host, `DEPLOY_SHA`, image digest và timestamp kể cả khi job fail.
- [ ] Mỗi phase phải có marker rõ: `preflight`, `checkout`, `registry_pull`, `migration`, `restart`, `health`, `consumer`, `smoke`, `rollback`.
- [ ] Không chỉ báo `exit 127`; nếu command thiếu phải ghi chính xác command và `PATH` đã dùng.
- [ ] Tạo artifact `deploy-evidence-<sha>` và liên kết artifact đó vào release manifest.
- [ ] Kiểm tra run `36086871922` và run mới bằng artifact/log; không ghi nguyên nhân “dirty worktree” nếu chưa có log trực tiếp chứng minh.

**Đạt khi:** một deploy fail có thể xác định phase và nguyên nhân từ artifact mà không cần SSH thủ công vào runner.

### 3.2 Preflight dependency và quyền trên self-hosted host

- [ ] Tạo `scripts/deploy/deploy_preflight.sh` chạy trước mọi mutation.
- [ ] Kiểm tra `bash`, `git`, `podman`, `podman-compose` hoặc compose provider thực tế, `systemctl`, `install`, `curl`, `awk`, `mktemp`, `rabbitmqctl`.
- [ ] Kiểm tra version tối thiểu, `PATH`, user/group, quyền `/etc/systemd/system`, `/var/lib/ceph-ai`, `/var/lib/containers`, `/run/ceph-ai`.
- [ ] Kiểm tra systemd units, socket, RabbitMQ container, registry login và DNS/HTTPS tới GHCR.
- [ ] Kiểm tra migration tool, database URL profile và backup destination trước khi sửa database.
- [ ] Xuất report pass/fail đã redact; fail trước khi checkout/reset hoặc restart.
- [ ] Bổ sung unit tests cho missing command, permission denied, registry unavailable và RabbitMQ unavailable.

**Đạt khi:** cố ý làm thiếu từng dependency tạo lỗi có tên dependency/phase, không phải `127` chung chung.

### 3.3 Tách worktree phát triển khỏi deployment checkout

- [ ] Không dùng `/root/ceph-ai` đang có thay đổi operator làm source duy nhất cho CD.
- [ ] Chọn một deployment root ổn định, ví dụ `/var/lib/ceph-ai/releases/<sha>` và symlink/current pointer có quyền kiểm soát.
- [ ] Chuyển systemd units, `container-up`, `container-down`, migration và compose path sang deployment root ổn định; không hard-code nhầm worktree phát triển.
- [ ] Mỗi release checkout phải sạch, detached tại đúng SHA, và được verify trước khi restart.
- [ ] Giữ dirty guard: nếu deployment root bẩn thì dừng; không cho phép “force deploy” để vượt guard.
- [ ] Preserve/restore chỉ áp dụng cho dữ liệu operator được phân loại rõ, không áp dụng cho source/config/runtime credential.
- [ ] Có rollback checkout về release trước mà không cần reset worktree phát triển.

**Đạt khi:** operator có thể sửa code trong worktree phát triển mà CD vẫn deploy được release artifact sạch; không mất uncommitted data.

### 3.4 Immutable artifact và supply chain

- [ ] Build một lần, push một image, deploy đúng GHCR digest đã scan; không build lại trên host.
- [ ] Ghi source SHA, image digest, base image digest, dependency lock hash, migration head, SBOM và scanner metadata trong manifest.
- [ ] Pin GitHub Actions bằng full commit SHA; pin RabbitMQ và các sidecar image bằng digest.
- [ ] Đồng nhất dependency install giữa quality image, release gate và runtime image; không để release gate cài một bộ khác production.
- [ ] Thêm signing/provenance tối thiểu bằng cosign/SLSA hoặc ghi rõ blocker nếu registry chưa hỗ trợ.
- [ ] Xác minh digest đang chạy của từng service bằng `podman inspect`; lưu kết quả vào deploy artifact.

**Đạt khi:** một SHA map duy nhất tới một image digest, một SBOM, một manifest và một runtime digest.

### 3.5 Migration, restart, smoke và retry

- [ ] Thứ tự bắt buộc: preflight → pull/verify image → backup → migration rehearsal check → migration → persist approved digest → restart.
- [ ] Migration fail phải dừng trước restart; không tự rollback database nếu chưa có migration-specific rollback evidence.
- [ ] Restart phải idempotent; chạy lại sau timeout không tạo worker/consumer trùng.
- [ ] Post-deploy smoke phải kiểm tra `/login`, authenticated dashboard health, Worker heartbeat, Watcher heartbeat, RabbitMQ consumer, migration head và release SHA.
- [ ] Smoke phải chạy đúng target host, không chỉ chạy trên runner.
- [ ] Thử deploy cùng artifact ít nhất 3 lần liên tiếp: fresh deploy, retry sau timeout, retry sau service restart.

**Đạt khi:** 3 lần CD liên tiếp pass với artifact và host evidence đầy đủ.

### 3.6 Rollback artifact

- [ ] Chọn release/image digest trước đó hợp lệ và ghi trong manifest.
- [ ] Diễn tập container rollback sau lỗi health nhưng trước schema change.
- [ ] Diễn tập migration failure giữa chừng với PostgreSQL staging; ghi rõ schema compatibility.
- [ ] Diễn tập rollback sau migration chỉ khi có kế hoạch downgrade/forward-fix đã review; không gọi container rollback là database rollback.
- [ ] Xác minh service, consumer, dashboard và digest sau rollback.
- [ ] Ghi thời gian recovery và người witness.

**Đạt khi:** rollback không làm mất dữ liệu, không tạo mutation trùng và có RTO đo được.

## 4. P0 — PostgreSQL, RabbitMQ, backup và DR

### 4.1 PostgreSQL rehearsal

- [ ] Dựng PostgreSQL staging gần production về version, extension, collation, pool và credential mode.
- [ ] Restore backup thực tế vào database mới; xác minh row counts, migration head, checksum và các bảng critical.
- [ ] Chạy migration từ một revision cũ có dữ liệu; inject failure trước/sau từng phase chính.
- [ ] Kiểm tra backup trước migration bị thiếu/quyền sai thì deploy dừng.
- [ ] Xác minh restore không cần database production đang chạy.
- [ ] Đo RPO/RTO và lưu report JSON + log + operator witness.

### 4.2 Incident Outbox

- [ ] Kill publisher sau database commit nhưng trước RabbitMQ confirm.
- [ ] Kill publisher sau confirm nhưng trước mark `SENT`.
- [ ] Restart RabbitMQ và xác minh retry/backoff/reconciliation/DLQ.
- [ ] Kill Worker giữa claim, mutation, post-check và mark terminal.
- [ ] Xác minh mỗi Incident cuối cùng được xử lý đúng một lần; redelivery không tạo mutation hoặc Telegram trùng.
- [ ] Kiểm tra queue age, oldest pending, max attempts và alert khi backlog quá hạn.

### 4.3 Backup/DR thật

- [ ] Backup database và runtime metadata ra storage độc lập khỏi host ceph-ai.
- [ ] Restore trên host/network namespace độc lập.
- [ ] Chạy RBD restore drill trên disposable target, không dùng volume production.
- [ ] Chạy RBD mirror planned failover/failback với fencing và split-brain guard.
- [ ] Ghi measured RPO/RTO, data-loss result, credential path và rollback result.
- [ ] Không đánh dấu DR pass bằng unit test hoặc scratch-only artifact.

## 5. P1 — Test và quality nghiêm ngặt

### 5.1 Coverage gate

- [ ] Thêm `pytest-cov` vào dependency/toolchain được lock.
- [ ] Xuất XML và HTML coverage theo SHA, Python version và test command.
- [ ] Tạo baseline coverage release hiện tại trước khi đặt threshold.
- [ ] Gate không được giảm total line/branch coverage so với baseline.
- [ ] Đặt threshold tối thiểu sau khi có baseline; critical path gồm auth/session, cluster scope, action gateway, migration wrapper, outbox và deploy preflight.
- [ ] Critical path đạt tối thiểu 90% line/branch theo metric đã chốt; test skip không tính là pass.
- [ ] Upload report ngay cả khi gate fail.

### 5.2 Static-analysis debt

- [ ] Chạy full-tree Ruff, mypy, Bandit/F821/F811 và complexity trên clean runner.
- [ ] Xuất inventory legacy findings theo file/rule/owner.
- [ ] Giữ rule “không thêm lỗi mới” trong ngắn hạn nhưng bổ sung burn-down target theo release.
- [ ] Ưu tiên security, auth, executor, migration, worker và deploy script trước UI legacy.
- [ ] Không dùng baseline để che lỗi mới ở critical path.
- [ ] Ghi số lỗi trước/sau vào release artifact.

### 5.3 Live read-only acceptance

- [ ] Tạo workflow manual có environment protection và safety window.
- [ ] Chạy health, inventory, pool/PG/CRUSH, RBD/RGW read-only trên ít nhất hai Ceph version và hai cluster/scope.
- [ ] Kiểm tra stale/error/unknown không bị biến thành số 0.
- [ ] Ghi SSH call count, timeout, collector lag, p95 API latency và raw command exit code đã redact.
- [ ] Không chạy mutation/destructive action trong wave live read-only.
- [ ] Tách live artifact khỏi fixture artifact.

### 5.4 Browser and operator acceptance

- [ ] Smoke authenticated dashboard ở 1280/1440/1920px và viewport hẹp.
- [ ] Kiểm tra loading, empty, stale, error, permission denied và reconnect.
- [ ] Kiểm tra cluster switching không giữ action/evidence/selection của cluster cũ.
- [ ] Kiểm tra keyboard/focus và audit preview cho action quan trọng.
- [ ] Ghi screenshot/video hoặc structured browser report theo SHA.

## 6. P1 — Safety, RBAC, audit và credential boundary

### 6.1 RBAC/cross-cluster

- [~] WebSocket incidents/cluster-state đã có guard query cluster và stale approval fingerprint test.
- [ ] Hoàn thiện user-to-cluster capability grant model hoặc ghi rõ mô hình admin-only được chấp nhận bởi operator.
- [ ] Test direct API, Dashboard, Worker, Telegram approval và WebSocket với user thiếu capability.
- [ ] Replay envelope/action/evidence của cluster A lên cluster B phải bị từ chối ở server-side.
- [ ] Target node/pool/volume/bucket phải được resolve lại ở execution-time.
- [ ] Audit phải ghi actor, role/capability, cluster, action, target, evidence fingerprint, decision và reason.
- [ ] Không dùng UI ẩn nút thay cho authorization.

### 6.2 Credential separation

- [ ] Watcher/Dashboard dùng SSH identity read-only.
- [ ] Mutation identity chỉ tồn tại ở executor boundary được phê duyệt.
- [ ] Tách OAuth/AI account directory theo service capability.
- [ ] Thu hẹp mount `/var/lib/ceph-ai` và network egress theo service.
- [ ] Test read-only identity không mutation được; mutation key không xuất hiện trong read-only container.
- [ ] Redact credential trong log, AI context, artifact, traceback và release manifest.

### 6.3 Single Full

- [ ] Giữ nguyên quyền toàn quyền của Single Full theo quyết định operator.
- [ ] Bổ sung audit bắt buộc: actor, session, target cluster, start/end, command class, result và operator acknowledgement.
- [ ] Kiểm tra kill switch, session expiry, reconnect và retry không tạo hành động ngoài context.
- [ ] Red-team prompt injection trong staging cô lập; không dùng cluster production.
- [ ] Không coi prompt “cấm phá hoại” là security boundary; chỉ ghi nhận typed remediation boundary ở các flow chuẩn.

## 7. P1 — AI diagnosis và online learning

### 7.1 RCA quality

- [ ] Xây golden set đã redacted với hàng trăm incident, ground truth độc lập và operator label.
- [ ] Tách train/validation/test theo thời gian và cluster; không để cùng incident xuất hiện ở hai tập.
- [ ] Đo precision, recall, abstention recall, unsafe proposal rate, calibration và hallucination rate theo health code.
- [ ] Ghi prompt/model/evidence version và cost per correct diagnosis.
- [ ] Chạy prompt injection/redaction test trên staging.

### 7.2 River v2 evidence

- [ ] Xác minh runtime count theo scope: verified outcomes, scored outcomes, rejected/stale outcomes, sample age và data quality.
- [ ] Thu tối thiểu 100–300 verified outcomes độc lập trên nhiều cluster/scope trước promotion; nếu không đủ phải giữ shadow.
- [ ] Không tạo label từ forecast do chính candidate tạo nếu chưa có outcome độc lập.
- [ ] Chạy temporal holdout và so sánh deterministic champion với River v2.
- [ ] Báo MAE/RMSE/SMAPE/bias/interval coverage/false-positive/alert volume/confidence interval.

### 7.3 Khép kín promotion lifecycle

- [ ] Sửa các consumer path còn gán cứng `target="shadow"` nếu đó là đường runtime cần đọc model active.
- [ ] Chứng minh end-to-end: shadow → candidate → quality gate → operator approval → active → health regression → rollback.
- [ ] Mỗi scope/horizon phải có model version và registry state nhất quán.
- [ ] Active model phải được đọc từ registry đã approve, không bootstrap ngầm từ default.
- [ ] Rollback candidate/active trên staging bằng simulated regression; ghi audit và khôi phục champion.
- [ ] Soak active canary tối thiểu 7–14 ngày, theo dõi CPU, DB latency, queue lag và alert volume.
- [ ] Giữ `online_learning_enabled=false`, `AUDIT_ONLY` và các candidate shadow-only cho đến khi toàn bộ gate trên pass.

### 7.4 Anomaly/forecast candidates

- [ ] Phân loại 21 scope drift bằng data-quality, workload shift hoặc model failure.
- [ ] Không promote HST với false-positive cao chỉ dựa trên fixture.
- [ ] RRCF/SNARIMAX phải có live temporal evidence và baseline comparison.
- [ ] Tách forecast correctness khỏi alert correctness; không dùng loss thấp để tự động promotion.

## 8. P1/P2 — Reliability, HA và vận hành

- [x] Xác định runtime owner duy nhất cho Dashboard, Watcher, Worker, Remediation Worker, Telegram outbox và Code Repair: Podman Compose do `ceph-ai-containers.service` sở hữu trên server hiện tại; không chạy song song systemd legacy units.
- [x] Kiểm tra restart/reconnect/reconcile của từng process: endpoint `/api/system/reliability` đọc heartbeat, collector/SSH failure và durable queue/role-mapping states; không tự restart hoặc reconcile từ diagnostics.
- [ ] Thiết kế HA hoặc ghi rõ single-node limitation trong release contract.
- [~] Đo p95 API, collector lag, DB connections, queue age, CPU/RAM, SSH calls và database growth: endpoint đã có API/collector/DB/queue/CPU-RAM/SSH; database growth và 24h time-series persistence còn cần bổ sung.
- [x] Đặt SLO/error budget cho dashboard freshness, incident processing, notification delivery và post-check; contract 30 ngày nằm trong `docs/operations/reliability-slo.md`.
- [x] Có alert khi stale snapshot, queue backlog, DB pool exhaustion, outbox retry/collector failure và failed deploy qua `/api/system/reliability`.
- [~] Chạy 24 giờ soak trên cluster/node thật cho collector và control plane: đã có script read-only `scripts/reliability_soak.py`, chưa chạy đủ 24 giờ nên chưa đánh dấu hoàn thành.

## 9. P2 — Supply chain và repository maintainability

- [~] Thêm `LICENSE` hoặc `COPYING` sau khi xác nhận điều khoản phân phối: đã
  thêm `LICENSE` cho internal use; cần owner phê duyệt trước khi phân phối bên ngoài.
- [~] Cập nhật `README`, handoff, production runbook và release manifest cùng một SHA/runtime model: tài liệu và generator đã cập nhật; manifest cuối cùng sẽ được sinh ở release checkout sạch.
- [x] Thêm documentation freshness check cho commit/migration/image digest.
- [x] Phân loại và archive các kế hoạch trùng; không xóa trước khi có mapping lịch sử.
- [x] Dọn `.codex-stage`, `transfer`, `*.bak-*`, schema dump và artifact tạm khỏi source package/Git; giữ artifact cần thiết trong storage có kiểm soát.
- [ ] Tách module lớn theo domain sau khi P0/P1 release blocker đóng; không refactor lớn cùng release deploy.
- [x] Ghi capability matrix theo Ceph version/deployment mode/cluster.

## 10. Evidence bắt buộc cho mỗi release candidate

Mỗi RC phải có một thư mục artifact chứa:

- `source-sha.txt`, branch, dirty-state và target host.
- `release-manifest.json` với migration head, image digest, base digest và lock hash.
- Test JUnit 3.11/3.12, integration JUnit, coverage XML/HTML.
- Ruff/mypy/Bandit full-tree report và baseline diff.
- pip-audit, license, SBOM, image scan và provenance/signature.
- Deploy preflight, deploy phase log, smoke report, running digest và migration head.
- PostgreSQL backup/restore/migration report.
- RabbitMQ/Outbox chaos report.
- Live read-only acceptance report.
- AI evaluation, verified outcome report, promotion/rollback audit nếu có.
- Operator approval, residual risk và rollback decision.

## 11. Release gate cuối cùng

Chỉ chuyển `NO-GO` sang `STAGING-APPROVED` khi:

- [ ] CI test 3.11/3.12, integration, quality, release gate pass.
- [ ] Deploy preflight và deploy pass trên checkout sạch.
- [ ] Post-deploy authenticated smoke pass.
- [ ] CD pass ít nhất 3 lần liên tiếp.
- [ ] Không còn deploy failure chưa phân loại.
- [ ] Live read-only acceptance pass trên ma trận cluster/version đã chọn.
- [ ] PostgreSQL restore/migration/rollback rehearsal pass.
- [ ] RabbitMQ/Worker chaos pass với idempotent recovery.
- [ ] Coverage gate và static-analysis report đạt ngưỡng.
- [ ] RBAC/cross-cluster/credential audit pass.
- [ ] River v2 vẫn shadow nếu chưa đủ verified/scored evidence; không active promotion ngầm.
- [ ] RTO/RPO, backup, rollback và operator witness đã được ghi.

Chỉ chuyển `STAGING-APPROVED` sang `PRODUCTION-CANARY` khi có operator sign-off riêng. Chỉ mở autonomy theo từng action/cluster sau khi action đó có post-check, rollback, rate limit, cooldown và kill switch đã được diễn tập.

## 12. Nhật ký thực hiện

| Ngày | Hạng mục | Kết quả | Evidence | Trạng thái |
|---|---|---|---|---|
| 25/09/2026 | Review nghiêm ngặt 6,3/10 | NO-GO; các khoảng trống production/runtime lớn hơn bằng chứng hiện có | Review do operator cung cấp; cần đối chiếu run thực tế | Recorded |
| 25/09/2026 | Image quality | Trivy HIGH/CRITICAL = 0 sau gỡ build tooling khỏi runtime image | Commit `87ac883d`, CI run `36086871922` | Accepted |
| 25/09/2026 | RBAC WebSocket/stale approval | `42 passed`; cross-cluster query bị từ chối | Commit `6bf41927` | Partial |
| 25/09/2026 | CD deploy | Run trước quality/release pass nhưng deploy fail; run mới chưa tới CD tại thời điểm lập kế hoạch | Actions run `36086871922`, `36089126816` | Open |

## 13. Quy tắc trạng thái

- `[ ]` Chưa có implementation hoặc evidence đủ dùng.
- `[~]` Có implementation/test một phần nhưng chưa acceptance.
- `[x]` Chỉ dùng khi có command, artifact, môi trường, kết quả và rollback note.
- `[!]` Bị chặn bởi hạ tầng, safety window hoặc quyết định operator.

**Không được đổi trạng thái thành `[x]` chỉ vì unit test hoặc CI artifact pass.**
