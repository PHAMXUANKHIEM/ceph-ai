# AI Self-Learning Repository Improvement Roadmap

## Mục tiêu

Mở rộng AI self-learning của Ceph-AI bằng các repository phù hợp nhưng vẫn giữ
kiến trúc an toàn hiện tại: consumer chỉ chạy `target="shadow"`, promotion phải
qua model registry + operator approval, mọi model phải có scope đầy đủ
`cluster + host/entity + metric + horizon`, và rollback phải fail-closed.

Không đưa toàn bộ các repository vào production image. Runtime Watcher chỉ dùng
thành phần đã được chứng minh về CPU, RAM, latency và failure behavior.

## Phân loại repository

| Repository | Vai trò dự kiến | Môi trường | Quyết định ban đầu |
|---|---|---|---|
| [River](https://github.com/online-ml/river) | Online/incremental learning | Watcher shadow | Dùng tiếp |
| [Evidently](https://github.com/evidentlyai/evidently) | Data quality, drift, evaluation report | Offline/staging | Thêm evaluator riêng |
| [NannyML](https://github.com/NannyML/nannyml) | Performance estimation khi label đến muộn | Offline/staging | Thử nghiệm sau delayed feedback |
| [MLflow](https://github.com/mlflow/mlflow) | Registry, artifact, version và promotion metadata | Tách khỏi runtime | Chỉ làm adapter, không đổi source of truth ngay |
| [Vowpal Wabbit](https://github.com/VowpalWabbit/vowpal_wabbit) | Contextual bandit/online policy learning | Research sandbox | Chưa production |
| [Alibi Detect](https://github.com/SeldonIO/alibi-detect) | Drift/outlier detector bổ sung | Offline/shadow | Benchmark với detector hiện tại |
| [Feast](https://github.com/feast-dev/feast) | Feature store dùng chung | Future platform | Chưa cần ở quy mô hiện tại |

## Nguyên tắc không thay đổi

- Không cho thư viện mới tự gọi Ceph, SSH, Telegram hoặc remediation.
- Không cho model mới ghi trực tiếp vào model active.
- Không suy đoán scope từ dữ liệu thiếu; thiếu `cluster`, `host/entity` hoặc
  `metric` phải trả `UNKNOWN_SCOPE`/`PROMOTION_BLOCKED`.
- Không dùng feedback chưa được xác minh làm label học.
- Không tăng CPU/RAM/DB pool của Watcher vượt resource budget hiện tại.
- Mọi artifact model phải có checksum, feature schema, model version, scope và
  rollback reference.

## Phase 0 — Contract và adapter boundary

- [x] Tạo interface nội bộ `OnlineModelBackend`: `predict_one`, `learn_one`,
  `snapshot`, `restore`, `resource_cost`.
- [x] Đảm bảo River, detector và evaluator không truy cập trực tiếp SQLAlchemy;
  chỉ nhận dữ liệu đã qua quality gate.
- [x] Thêm `backend_name`, `backend_version`, `feature_schema` vào audit/evidence.
- [ ] Pin version và license của từng dependency; chạy pip-audit, SBOM và image
  scan trước khi thử nghiệm.
- [x] Test backend không thể ghi active model hoặc tự tạo promotion audit.

### Phase 0 evidence

- `shared/online_model_backend.py` định nghĩa boundary thuần model; backend
  không có quyền truy cập DB, registry, Ceph, SSH hoặc promotion.
- River khai báo metadata `river/0.25.0`, algorithm `river_mean`, model
  version `river-mean-v1` và schema `scalar-v1`; consumer kiểm tra metadata
  trước khi ghi audit.
- Migration `m20260921onlinebackend` bổ sung identity cho learner audit và
  cycle audit; Alembic đã được kiểm tra còn đúng một head.
- Test contract/consumer/registry: `17 passed`; migration column test:
  `1 passed` trên SQLite tạm.

## Phase 1 — River production hardening

- [x] Giữ consumer ở `target="shadow"`.
- [x] Lưu state bền vững theo scope `cluster + host + metric`.
- [x] Có checksum, reset fail-closed, bounded batch và circuit breaker.
- [x] Thêm replay determinism test: cùng input/snapshot phải cho cùng output.
- [x] Thêm resource benchmark cho 1, 10, 100 stream; ghi CPU, RAM, latency và
  DB writes.
- [x] Thêm corruption/partial snapshot/old-version migration test.

### Phase 1 evidence

- Snapshot có `schema_version=1`; snapshot legacy không có trường này được
  migrate trong memory, còn snapshot thiếu trường, sai schema hoặc checksum
  vẫn reset fail-closed về baseline rỗng.
- Replay test giữ cùng snapshot/input cho cùng prediction và state.
- Resource benchmark `scripts/online_learning_resource_benchmark.py` đã chạy
  1/10/100 stream × 100 sample. Kết quả 100 stream: khoảng `5.96 ms CPU`,
  `0.53 MiB` RSS tăng đỉnh, p95 mỗi update `0.000581 ms`, state `11,126`
  bytes; `db_writes=0` vì benchmark cô lập model, persistence đã có test
  consumer riêng.
- Phase 1 test batch: `23 passed`.

## Phase 2 — Evidently evaluator

- [x] Viết exporter read-only từ forecast run, verified outcome và learner audit
  sang dataset có schema cố định.
- [x] Chạy data-quality report: missingness, freshness, gap, duplicate,
  out-of-range và scope leakage.
- [x] Chạy drift report theo từng `cluster + host/entity + metric`, không gộp
  nhiều host hoặc metric vào một baseline.
- [x] So sánh MAE, RMSE, SMAPE, bias, false-positive rate và alert volume giữa
  active/candidate.
- [x] Lưu report dưới artifact immutable; không ghi report trực tiếp vào DB
  production từ evaluator.
- [x] Test drift report không tạo alert, notification, remediation hoặc promotion.

### Phase 2 evidence

- `shared/online_learning_evaluator.py` và
  `scripts/online_learning_evaluator.py` tạo dataset/report JSON thuần
  read-only với schema `ceph-ai-online-eval-v1`; không import SQLAlchemy và
  không có đường gọi alert, Telegram, remediation hay registry.
- Mỗi scope được tính riêng bằng `cluster_id + host + metric`; scope thiếu,
  duplicate, gap, stale, out-of-range hoặc leakage đều làm report `FAIL` và
  `promotion_safe=false`.
- Báo cáo có MAE, RMSE, SMAPE, bias và false-positive rate; artifact chỉ tạo
  file mới (`x` mode), không overwrite artifact cũ.
- Evidently được giữ ở evaluation/staging boundary; chưa thêm vào Watcher
  production image. Khi benchmark dependency này, adapter chỉ được nhận
  dataset đã qua contract trên.
- Scope report có alert volume và schema `model_role`; evaluator so sánh
  active/candidate theo từng scope, nhưng chỉ trả evidence, không promotion.
- Phase 2 test: `24 passed` (bao gồm evaluator, drift và consumer regression).

## Phase 3 — NannyML cho delayed/no-label feedback

- [x] Xác định label availability SLA và phân biệt `NO_LABEL`, `PENDING_LABEL`,
  `VERIFIED_SUCCESS`, `VERIFIED_FAILURE`, `INCONCLUSIVE`.
- [x] Chạy performance estimation chỉ trên dataset đã cố định scope và time
  window.
- [ ] So sánh estimated performance với verified outcome khi label xuất hiện.
- [x] Chặn promotion nếu estimation không đủ confidence, coverage hoặc bị drift.
- [x] Test delayed label, missing label, label đảo ngược và feedback duplicate.
- [ ] Đo CPU/RAM của NannyML offline; không đưa dependency vào Watcher nếu chưa
  đạt budget.

### Phase 3 evidence

- `shared/delayed_feedback_evaluator.py` là boundary fail-closed cho delayed
  feedback; `scripts/delayed_feedback_evaluator.py` chỉ đọc JSON và ghi artifact
  mới, không gọi DB, registry, alert hay remediation.
- Đã test SLA pending/overdue, verified success/failure, insufficient coverage,
  scope isolation và promotion luôn bị khóa trong estimator.
- NannyML chưa được đưa vào image production; còn hai việc: benchmark NannyML
  offline và đối chiếu estimated performance với verified outcome thật.
- Phase 3 test hiện tại: `19 passed`.

## Phase 4 — Model registry adapter với MLflow

- [x] Định nghĩa mapping một chiều:
  `CANDIDATE/SHADOW/ACTIVE/RETIRED/BLOCKED` ↔ registry metadata/alias.
- [x] Giữ `shared/model_registry.py` là source of truth trong giai đoạn đầu;
  MLflow chỉ nhận artifact và metadata sau khi local transaction thành công.
- [x] Artifact phải chứa model snapshot, checksum, feature schema, scope,
  training window, evidence IDs và rollback target.
- [x] Promotion sequence bắt buộc: evaluate → request → operator approve →
  update local registry → publish MLflow metadata → select runtime state.
- [x] Nếu publish MLflow thất bại, không được đổi active runtime; phải audit
  `PROMOTION_BLOCKED` hoặc `PROMOTION_PUBLISH_FAILED`.
- [x] Rollback phải chọn đúng previous active model và kiểm tra checksum trước
  khi restore.
- [x] Test registry unavailable, duplicate publish, stale artifact, wrong scope,
  checksum mismatch và rollback sau restart.

### Phase 4 evidence

- `shared/mlflow_registry_adapter.py` không import MLflow và không có quyền
  đổi local runtime; publisher chỉ được gọi sau khi local transaction đã commit.
- Manifest bắt buộc full scope `cluster_id + entity_type + entity_id + metric +
  horizon_hours`, snapshot, checksum, feature schema, evidence và rollback
  target; duplicate/stale/wrong-scope/checksum đều fail-closed.
- Local registry lifecycle test đã có candidate/shadow/active/rollback; adapter
  test hiện tại: `8 passed`, gồm registry unavailable, duplicate, stale, wrong
  scope, checksum mismatch và re-validation sau serialization/restart.

## Phase 5 — Vowpal Wabbit research sandbox

- [x] Chỉ tạo benchmark offline với dữ liệu replay đã được anonymize.
- [x] Chỉ nghiên cứu contextual bandit sau khi có verified action outcome và
  policy-safe action set.
- [x] Không cho bandit quyết định lệnh Ceph trực tiếp; output chỉ là candidate
  recommendation để operator review.
- [ ] So sánh River baseline với VW về regret, false-positive, stability,
  CPU/RAM và explainability.
- [x] Có kill switch, time budget và artifact cleanup cho mọi benchmark.

### Phase 5 evidence

- `shared/bandit_sandbox.py` chỉ nhận verified outcomes, giới hạn action set
  (`OBSERVE`, `COLLECT_DIAGNOSTICS`, `OPEN_TICKET`), full scope và trả
  recommendation `executable=false`; không có Ceph/SSH/remediation call.
- Có kill switch, sample/time budget và dataset checksum; outcome không verified
  hoặc action ngoài policy bị bỏ qua.
- Vowpal Wabbit chưa đưa vào runtime; còn benchmark so sánh River/VW về regret,
  stability, resource và explainability.
- Phase 5 test hiện tại: `11 passed`.

## Phase 6 — Alibi Detect bổ sung drift/anomaly

- [ ] Benchmark detector hiện tại với Alibi Detect trên cùng dataset/scope.
- [ ] Đo false-positive, detection delay, missing-data behavior và CPU cost.
- [x] Không để detector mới tự thay đổi lifecycle alert; chỉ tạo evidence
  `DRIFT`/`DATA_QUALITY` cho promotion gate.
- [x] Chặn detector nếu thiếu baseline, thiếu scope hoặc schema không khớp.
- [ ] Chỉ chọn detector mới nếu tốt hơn baseline trong acceptance window đã định.

### Phase 6 evidence

- `shared/alibi_detect_boundary.py` cung cấp boundary bounded cho detector
  optional: thiếu baseline/scope/schema trả `INSUFFICIENT_DATA` hoặc
  `DATA_QUALITY`; detector chỉ trả evidence, không tự mở alert/promotion.
- Có test stable/drift, detection delay, missing baseline và scope/schema
  mismatch: `10 passed` trong nhóm detector/sandbox/registry.
- Alibi Detect thật và benchmark so sánh với River ADWIN vẫn chưa cài; chưa
  được phép đưa dependency này vào Watcher image.

## Phase 7 — Feast, chỉ khi có nhu cầu thật

- [ ] Chỉ mở phase này khi có ít nhất hai consumer cần cùng feature và đã có
  vấn đề rõ về consistency/feature reuse.
- [ ] Thiết kế feature key bắt buộc gồm cluster, entity/host, metric và event
  time; không cho cross-cluster lookup.
- [ ] Chạy Feast ngoài Watcher runtime ban đầu; đánh giá latency, availability,
  backup/restore và blast radius.
- [ ] Không thay durable learner state bằng Feast nếu chưa có migration và
  rollback rehearsal.

## Phase 8 — Acceptance và promotion gate

- [ ] Unit/regression test cho shadow, candidate, active, blocked, retired và
  rollback.
- [ ] Test toàn bộ scope dimension trên node resource và volume forecast.
- [ ] Replay tối thiểu 14 ngày với active/candidate; không dùng production
  promotion để thay thế replay evidence.
- [ ] Chạy 72 giờ shadow/canary, theo dõi alert volume, false-positive,
  delayed feedback, CPU, RAM, DB pool và queue latency.
- [ ] Rollback rehearsal trên artifact immutable trong staging.
- [ ] Chỉ promotion khi operator, security và operations ký duyệt; production
  vẫn giữ `SHADOW_ONLY` nếu chưa có verified outcomes đủ điều kiện.

## Definition of Done

- Có report cho từng backend, dataset version, scope và thời gian chạy.
- Không có dependency research-only trong production image nếu chưa được duyệt.
- Consumer không thể tự promote, notify hoặc remediate.
- Promotion/rollback có local registry state, artifact checksum và append-only
  audit khớp nhau.
- Mọi failure quan trọng đều fail-closed và có rollback path.
