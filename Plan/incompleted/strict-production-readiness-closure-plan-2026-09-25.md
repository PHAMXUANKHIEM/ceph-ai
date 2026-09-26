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

- [~] Workflow đã capture deploy stdout/stderr qua streaming redactor, exit code, host, `DEPLOY_SHA`, immutable image ref và timestamp bằng `if: always()`; cần CI failure witness để acceptance.
- [~] Đã có marker `preflight`, `checkout`, `runtime_setup`, `registry_pull`, `migration`, `restart`, `health`, `consumer`, `smoke` và terminal result; marker `rollback` còn nằm ngoài deploy flow và chưa hoàn thiện.
- [~] Preflight ghi tên command thiếu; artifact ghi phase/exit code. Việc ghi `PATH` đã sanitize và failure injection trên self-hosted runner còn thiếu.
- [~] Workflow tạo artifact `deploy-evidence-<sha>`; release manifest ghi `evidence.deploy_evidence_artifact` (`8be8cb9b`). Failed preflight check được xuất thành annotation công khai vì log self-hosted cần quyền admin (`576f8c5b`).
- [~] Run `36141457826`/`36146314354` lần đầu tới deploy và dừng ở preflight read-only (không mutation). Đã sửa 2 check không thể pass: registry probe dùng `--fail` với GHCR trả 401, và backup dir lệch với backup script (`7b67a78e`). Nguyên nhân còn lại trên runner chờ annotation của run `36208592580`.

**Đạt khi:** một deploy fail có thể xác định phase và nguyên nhân từ artifact mà không cần SSH thủ công vào runner.

### 3.2 Preflight dependency và quyền trên self-hosted host

- [~] Tạo `scripts/deploy/deploy_preflight.sh` chạy trước mọi mutation; đã nối vào deploy job trước registry login/rollout, còn cần acceptance trên self-hosted runner.
- [~] Kiểm tra `bash`, `git`, `podman`, compose provider thực tế, `systemctl`, `install`, `curl`, `awk`, `mktemp` và `rabbitmqctl` trong RabbitMQ container; còn thiếu version floor acceptance trên host thật.
- [~] Kiểm tra checkout, quyền `/etc/systemd/system`, `/var/lib/ceph-ai`, `/var/lib/containers`, `/run/ceph-ai`; còn thiếu user/group và version tối thiểu.
- [~] Kiểm tra systemd unit, RabbitMQ container, registry credential source và HTTPS tới GHCR; chưa thực hiện registry login acceptance thật.
- [~] Kiểm tra migration tool, PostgreSQL URL profile và backup destination trước rollout; chưa có staging witness.
- [~] Xuất report pass/fail mode `0640`, không ghi DATABASE_URL/credential; workflow upload report bằng `if: always()`.
- [~] Có unit tests cho missing command, permission target, registry unavailable, RabbitMQ unavailable, redaction và thứ tự preflight trước rollout; live negative injection để sau.

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

- [~] Quality job build/scan/push một image; deploy chỉ nhận `ghcr.io/...@sha256` từ artifact. Chờ deploy pass để có witness.
- [x] Manifest ghi source SHA, image digest, base image digest, lock hash, migration head, SBOM/scan sha256 + tool version, provenance (`8be8cb9b`); release_gate pass trong run `36141457826`.
- [x] Mọi Action pin full SHA; RabbitMQ/Trivy/Syft pin digest; `tests/test_workflow_pinning.py` giữ ràng buộc (`8be8cb9b`, CI xanh `36141457826`). RabbitMQ production container do host quản lý, ngoài repo.
- [x] Mọi job CI cài `requirements-prod.lock --require-hashes` trước dev extra và chạy `verify_lock_parity.py` (`8be8cb9b`; pass trong run `36141457826`).
- [x] SLSA provenance ký Sigstore qua `actions/attest-build-provenance`, push lên GHCR (`8be8cb9b`, `c6187f69`); pass trong run `36141457826`.
- [~] Deploy ghi `running-images.jsonl` (image đang chạy/approved/match từng service), kể cả khi lệch (`fb4cedac`). Chờ deploy witness.

**Đạt khi:** một SHA map duy nhất tới một image digest, một SBOM, một manifest và một runtime digest.

### 3.5 Migration, restart, smoke và retry

- [ ] Thứ tự bắt buộc: preflight → pull/verify image → backup → migration rehearsal check → migration → persist approved digest → restart.
- [ ] Migration fail phải dừng trước restart; không tự rollback database nếu chưa có migration-specific rollback evidence.
- [ ] Restart phải idempotent; chạy lại sau timeout không tạo worker/consumer trùng.
- [~] `post_deploy_smoke.py`: login page, heartbeat Watcher/Worker, authenticated health (khi có smoke account), migration head, release SHA từng image; consumer đã có sẵn (`42d6f6b8`). Chạy read-only trên lab phát hiện đúng migration chưa apply và image chưa gắn nhãn.
- [~] Smoke chạy trong `restart_container_stack.sh` trên target host. **Blocker:** runner `ceph-ai-lab` hiện cài trên máy không phải Ceph AI host (preflight run `36208592580`: thiếu podman/checkout/unit/RabbitMQ/DATABASE_URL).
- [ ] Thử deploy cùng artifact ít nhất 3 lần liên tiếp: fresh deploy, retry sau timeout, retry sau service restart.

**Đạt khi:** 3 lần CD liên tiếp pass với artifact và host evidence đầy đủ.

### 3.6 Rollback artifact

- [~] Deploy ghi `rollback-target.json`; sửa lỗi deploy lại cùng digest ghi đè mất rollback target (`b9f58e48`).
- [ ] Diễn tập container rollback sau lỗi health nhưng trước schema change.
- [ ] Diễn tập migration failure giữa chừng với PostgreSQL staging; ghi rõ schema compatibility.
- [ ] Diễn tập rollback sau migration chỉ khi có kế hoạch downgrade/forward-fix đã review; không gọi container rollback là database rollback.
- [ ] Xác minh service, consumer, dashboard và digest sau rollback.
- [ ] Ghi thời gian recovery và người witness.

**Đạt khi:** rollback không làm mất dữ liệu, không tạo mutation trùng và có RTO đo được.

## 4. P0 — PostgreSQL, RabbitMQ, backup và DR

### 4.1 PostgreSQL rehearsal

- [~] Dựng PostgreSQL staging gần production về version, extension, collation, pool và credential mode: đã thêm restore-rehearsal tooling và strict target guards; chưa có staging witness mới.
- [~] Restore backup thực tế vào database mới; xác minh row counts, migration head, checksum và các bảng critical: script `scripts/deploy/postgresql_restore_rehearsal.py` đã thực hiện flow này khi được cấp PostgreSQL staging.
- [~] Chạy migration từ một revision cũ có dữ liệu; inject failure trước/sau từng phase chính: đã có failure-injection phases trong script, chưa chạy acceptance trên database thật.
- [~] `verify_migration_backup.py` chặn migration nếu backup thiếu/rỗng/cũ/symlink/quyền rộng/không đọc được bằng `pg_restore --list` (`709b1f55`); backup production mới nhất pass. Chờ deploy witness.
- [ ] Xác minh restore không cần database production đang chạy.
- [ ] Đo RPO/RTO và lưu report JSON + log + operator witness.

### 4.2 Incident Outbox

- [ ] Kill publisher sau database commit nhưng trước RabbitMQ confirm.
- [ ] Kill publisher sau confirm nhưng trước mark `SENT`.
- [ ] Restart RabbitMQ và xác minh retry/backoff/reconciliation/DLQ.
- [ ] Kill Worker giữa claim, mutation, post-check và mark terminal.
- [ ] Xác minh mỗi Incident cuối cùng được xử lý đúng một lần; redelivery không tạo mutation hoặc Telegram trùng.
- [~] `/api/system/reliability` có dead-letter (critical), max attempts/retry exhaustion, overdue retry, stuck claim; DEAD không còn làm backlog alert treo mãi (`a2f874ef`). Chờ chaos witness.

### 4.3 Backup/DR thật

- [ ] Backup database và runtime metadata ra storage độc lập khỏi host ceph-ai.
- [ ] Restore trên host/network namespace độc lập.
- [ ] Chạy RBD restore drill trên disposable target, không dùng volume production.
- [ ] Chạy RBD mirror planned failover/failback với fencing và split-brain guard.
- [ ] Ghi measured RPO/RTO, data-loss result, credential path và rollback result.
- [ ] Không đánh dấu DR pass bằng unit test hoặc scratch-only artifact.

## 5. P1 — Test và quality nghiêm ngặt

### 5.1 Coverage gate

- [x] `pytest-cov==7.1.0`, `coverage==7.16.1` trong dev extra (`41e1314a`).
- [x] Test matrix xuất XML/HTML coverage theo Python version; upload `if: always()` (CI `36141457826`).
- [x] Baseline đo trên 4.332 test: 74,72% line / 63,02% branch; floor = đo − 1,0 điểm (`57f6e3fa`).
- [x] `coverage_gate.py` chặn giảm total và từng critical group; release gate bắt buộc (`41e1314a`, pass CI `36141457826`).
- [~] 6 critical group có floor riêng; deploy preflight là bash nên đo gián tiếp qua test hành vi.
- [~] Chỉ `action_gateway` đạt (99,7/99,1); `--enforce-critical-target` chưa bật. Skip được báo cáo riêng, không tính coverage.
- [x] Report ghi trước exit status; bước upload `if: always()`.

### 5.2 Static-analysis debt

- [x] `static_analysis_inventory.py` chạy full-tree trong quality job (`41e1314a`, pass CI `36141457826`).
- [x] `static-analysis-inventory.json`/`-findings.json` theo file/rule/owner/tier.
- [x] Budget chỉ được hạ; burn-down target rc-2026-10 (−25%) và rc-2026-11 (−50%) cho critical tier.
- [x] Critical tier = security/auth/executor/migration/worker/deploy, có ceiling riêng.
- [x] Code mới vượt budget được sửa, không re-record (`dc3812ad`: mypy 968→828, complexity/bandit về ceiling).
- [x] Release evidence ghi `static_analysis.before/after`.

### 5.3 Live read-only acceptance

- [x] `.github/workflows/live-readonly-acceptance.yml`: dispatch-only, environment `live-readonly`, safety window UTC (`54674c19`). Cần tạo environment + reviewer trên GitHub.
- [ ] Chạy health, inventory, pool/PG/CRUSH, RBD/RGW read-only trên ít nhất hai Ceph version và hai cluster/scope.
- [~] Probe cố ý với pool không tồn tại phải trả lỗi, không phải 0 image; chưa chạy live.
- [~] Report ghi SSH call/p95/timeout/exit/latency, API p95 tùy chọn; collector lag lấy từ `/api/system/reliability`. Chưa chạy live.
- [x] Allowlist probe + chặn mutation verb ở tầng SSH transport, có test (`54674c19`).
- [x] Artifact riêng `live-readonly-evidence-<sha>-<attempt>`.

### 5.4 Browser and operator acceptance

- [x] `scripts/browser_acceptance.mjs`: 10 trang × 4 viewport; phát hiện + sửa 3 trang tràn ngang trên mobile (`dadc9ecf`). 46/46 pass trên instance cô lập.
- [~] Permission denied, error/stale (unknown không thành 0: sửa MON 0/0 `15029290`) và reconnect đã tự động hóa; loading/empty chưa có assert riêng.
- [~] Browser test xác nhận `?cluster=` giữ đúng cluster qua điều hướng; action/evidence cũ chưa assert.
- [~] Tab focus + indicator có test; audit preview chưa.
- [x] `browser-acceptance.json` + screenshot theo SHA.

## 6. P1 — Safety, RBAC, audit và credential boundary

### 6.1 RBAC/cross-cluster

- [~] WebSocket incidents/cluster-state đã có guard query cluster và stale approval fingerprint test.
- [~] Đã thêm user-to-cluster capability grant model, migration và API quản lý grant; còn cần operator sign-off capability catalogue.
- [~] Dashboard action approval, cluster selection và WebSocket đã kiểm tra capability server-side; còn test Worker/Telegram approval với user thiếu capability.
- [~] Cluster selection, action approval và WebSocket đã chặn cross-cluster ở server-side; còn replay envelope/action/evidence qua Worker.
- [~] OSD/PG resolve lại bằng `ceph osd ls`/`ceph pg map` ngay trước lease, fail-closed; trước đó scope tự sinh từ chính ID yêu cầu (`349552d2`). Pool/volume/bucket chưa có contract typed.
- [~] Single Full có audit bền vững actor/session/run/cluster/start/end/result và prompt fingerprint; audit capability grant/action đầy đủ còn mở.
- [~] Capability kiểm tra server-side ở selection/approval/WebSocket; test nhánh từ chối trong `tests/test_action_gateway_boundaries.py` (`3f27f5c0`).

### 6.2 Credential separation

- [~] Dashboard/Watcher/Telegram đã nhận read-only identity riêng; cần acceptance chứng minh key trên node thực sự không có quyền mutation.
- [~] Worker typed remediation nhận mutation identity riêng và Single Full giữ identity riêng; còn acceptance boundary trên host thật.
- [ ] Tách OAuth/AI account directory theo service capability.
- [~] Compose đã che khuất các thư mục full-executor-ssh/secrets/accounts khỏi service thường và tách mount identity; network egress và toàn bộ `/var/lib/ceph-ai` còn cần thu hẹp.
- [~] `tests/test_compose_credential_boundaries.py` + sửa vault-monitor đọc được mutation key/Single Full token/OAuth executor (`f57e332f`). Acceptance trên host thật còn mở.
- [~] Single Full audit chỉ lưu prompt SHA-256, không lưu prompt/output thô; cần hoàn tất kiểm tra redaction trên mọi artifact/traceback.

### 6.3 Single Full

- [~] Đã giữ nguyên quyền toàn quyền của Single Full và không hạ quyền model; boundary vẫn dựa trên Telegram allowlist, token, scope và confirmation.
- [~] Đã bổ sung durable audit bắt buộc cho actor, run/session, target cluster, start/end, command class, result và operator acknowledgement; còn kiểm tra runtime production.
- [ ] Kiểm tra kill switch, session expiry, reconnect và retry không tạo hành động ngoài context.
- [ ] Red-team prompt injection trong staging cô lập; không dùng cluster production.
- [ ] Không coi prompt “cấm phá hoại” là security boundary; chỉ ghi nhận typed remediation boundary ở các flow chuẩn.

## 7. P1 — AI diagnosis và online learning

### 7.1 RCA quality

- [ ] Xây golden set đã redacted với hàng trăm incident, ground truth độc lập và operator label.
- [ ] Tách train/validation/test theo thời gian và cluster; không để cùng incident xuất hiện ở hai tập.
- [x] Precision, ECE, hallucination (ngoài catalogue), breakdown theo health code/prompt version (`1322d1dc`).
- [~] Cost per correct diagnosis + breakdown theo prompt version/provider; cần nguồn `cost_usd` cho production export.
- [ ] Chạy prompt injection/redaction test trên staging.

### 7.2 River v2 evidence

- [x] `scripts/river_v2_promotion_evidence.py` (`00e920fb`); live 2026-09-25: 4 verified, 0 scored, 2 scope → KEEP_SHADOW.
- [ ] Thu tối thiểu 100–300 verified outcomes độc lập trên nhiều cluster/scope trước promotion; nếu không đủ phải giữ shadow.
- [~] Label policy yêu cầu audit độc lập; report đếm self-labelled (live = 0).
- [~] `paired_holdout` trong runtime replay (`1cfdd002`); live 2026-09-26: 2 verified outcome, chưa có paired case.
- [x] Đủ các metric + bootstrap 95% CI của MAE delta (`shared/forecast_comparison.py`).

### 7.3 Khép kín promotion lifecycle

- [x] Không còn path gán cứng: consumer dùng `resolve_update_target` (exact ACTIVE registry + PROMOTED audit).
- [ ] Chứng minh end-to-end: shadow → candidate → quality gate → operator approval → active → health regression → rollback.
- [ ] Mỗi scope/horizon phải có model version và registry state nhất quán.
- [~] Bootstrap chỉ nhận deterministic champion, từ chối `river_*`, ghi audit `BASELINE_REGISTERED` (`addeb568`). 21 scope `seasonal_median:168h` ACTIVE từ bootstrap trước đó chưa có audit.
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
- [x] Availability model single-node/no-HA trong `docs/operations/reliability-slo.md` + runbook (`04aad5fe`).
- [~] Đo p95 API, collector lag, DB connections, queue age, CPU/RAM, SSH calls và database growth: endpoint đã có API/collector/DB/queue/CPU-RAM/SSH và current database size; growth rate cần hai mẫu soak liên tiếp.
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

- [x] CI test 3.11/3.12, integration, quality, release gate pass (run `36141457826`, `36146314354`).
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
| 25/09/2026 | Deploy preflight code/test | Thêm preflight read-only, report luôn được upload và negative tests; chưa dùng kết quả này để tuyên bố host/deploy accepted | `tests/test_deploy_preflight.py` 11 passed; deploy regression chạy cục bộ | Partial |
| 25/09/2026 | Deploy failure evidence code/test | Thêm phase log, metadata kết quả, stdout/stderr streaming redaction và artifact `if: always()`; chưa có deploy fail/pass witness thật | Deploy/preflight regression `13 passed`; YAML/Python/Bash syntax đạt | Partial |
| 25/09/2026 | Coverage + static-analysis gate | Gate/budget chạy trong CI, release gate bắt buộc | `41e1314a`, `57f6e3fa`, CI `36141457826` | Partial |
| 25/09/2026 | Supply chain | Pin SHA/digest, prod lock parity, SLSA provenance, manifest mở rộng | `8be8cb9b`, `c6187f69`, CI `36141457826` | Partial |
| 25/09/2026 | Main đỏ do cache cluster 1s | 11 test secondary-cluster fail mỗi leg; sửa invalidation theo Cluster flush/commit | `bc9a8b2c` | Accepted |
| 25/09/2026 | Online learning | Crash naive/aware datetime chặn mọi mẫu từ 23/09; lỗi bị nuốt; gap 900s loại 96% mẫu → sửa + log + gap 1,5× cadence | `0670de30`; dry-run DB thật không commit | Partial |
| 25/09/2026 | CI end-to-end | test/integration/quality/release_gate xanh; deploy dừng ở preflight read-only | CI `36141457826`, `36146314354` | Partial |
| 26/09/2026 | Deploy runner | Preflight annotation: runner `ceph-ai-lab` không phải Ceph AI host (9 check fail) | CI `36208592580` | Blocked |
| 26/09/2026 | Code closure wave 2 | Smoke, running image, rollback target, live OSD/PG resolution, Telegram approver allowlist, vault-monitor secrets, RCA metrics, holdout, browser acceptance | `fb4cedac`..`dadc9ecf` | Partial |

## 13. Quy tắc trạng thái

- `[ ]` Chưa có implementation hoặc evidence đủ dùng.
- `[~]` Có implementation/test một phần nhưng chưa acceptance.
- `[x]` Chỉ dùng khi có command, artifact, môi trường, kết quả và rollback note.
- `[!]` Bị chặn bởi hạ tầng, safety window hoặc quyết định operator.

**Không được đổi trạng thái thành `[x]` chỉ vì unit test hoặc CI artifact pass.**
