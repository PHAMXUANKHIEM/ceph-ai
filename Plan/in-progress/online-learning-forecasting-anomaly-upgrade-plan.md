# Kế hoạch nâng cấp Online Learning, Forecasting và Anomaly Detection

> **Trạng thái:** `IN_PROGRESS` — lập kế hoạch, chưa bật model mới vào active và
> chưa cho phép auto-remediation.
>
> **Phạm vi:** CPU/RAM node, IOPS/latency, capacity và các metric time-series có
> thể thu thập theo chu kỳ. Kế hoạch này bổ sung cho
> `Plan/in-progress/self-learning-predictive-alerting-plan.md`; không thay thế
> các production/security gate trong `Plan/in-progress/production-readiness-*`.

## 1. Mục tiêu

Xây dựng một pipeline dự báo và phát hiện bất thường có thể kiểm chứng, chạy được
trên CPU và giới hạn RAM, hỗ trợ online learning nhưng vẫn giữ baseline hiện tại
làm phương án an toàn:

```text
raw metrics
  -> data-quality gate
  -> feature/state builder
  -> active baseline + shadow candidates
  -> paired evaluation
  -> drift/quality state
  -> operator-approved promotion
  -> rollback về version trước
```

Mục tiêu nghiệm thu:

- Mỗi model state được định danh đầy đủ bằng `cluster + host/entity + metric +
  horizon + algorithm + version + feature_schema`.
- Model candidate chỉ chạy shadow/canary, không tạo alert, không gửi Telegram và
  không gọi remediation.
- Forecast có horizon cụ thể, feature provenance, quality status, confidence và
  prediction interval.
- Drift làm giảm confidence hoặc chuyển candidate sang `DRIFT`; không tự reset,
  promote hoặc thay đổi policy.
- Promotion dựa trên paired walk-forward outcomes cùng timestamp, có operator
  approval, audit và rollback.
- Benchmark nặng chạy ngoài poll loop; Watcher vẫn giữ bounded CPU, RAM, latency
  và I/O.

## 2. Không nằm trong phạm vi

- Không đưa deep learning, GPU, GluonTS/Chronos hoặc model cần nhiều RAM vào
  production phase này.
- Không chạy StatsForecast, PyOD, Evidently hoặc sktime trong mỗi Watcher poll.
- Không cho LLM tự chọn detector, tự đổi threshold hoặc tự promote model.
- Không thay active baseline chỉ vì một benchmark offline tốt.
- Không bật autonomous remediation mới khi các production/security/release gate
  còn mở.
- Merlion chỉ là tài liệu tham khảo vì repository đã archive; Alibi Detect chỉ được
  xem xét sau license review riêng.

## 3. Hiện trạng đã xác nhận

| Thành phần | Hiện trạng | Hệ quả kế hoạch |
|---|---|---|
| River | `river==0.25.0`; adapter hiện là `RiverMeanLearner`, `river_mean`, `scalar-v1` | Nâng cấp theo version mới nhưng giữ `river_mean` làm fallback |
| Node forecast | `watcher/node_resource_forecast.py` có linear, rolling quantile và River shadow | Không thay operational candidates trong phase đầu |
| Volume forecast | Có nhiều horizon và seasonal baseline trong `watcher/volume_learning.py` | Chuẩn hóa identity/horizon và paired evaluation |
| Drift | Có `shared/forecast_drift.py` dựa trên bounded window/residual | ADWIN chỉ là candidate, phải chạy song song trước |
| Registry/shadow | Đã có registry, evaluation, promotion guard và `SHADOW_ONLY` | Tái sử dụng, không tạo lifecycle thứ hai |
| PyOD | Đã có `pyod==2.0.5` | Chỉ dùng offline benchmark, không gọi từ Watcher |
| StatsForecast/RRCF/Evidently/NAB | Chưa thấy dependency production | Đặt trong benchmark/reporting environment riêng |
| Data contract | Đã có quality gate và metric-quality status | Dùng chung, không bypass khi tạo feature |
| Production policy | Promotion/operator approval/rollback là bắt buộc | Mọi phase đều fail-closed |

## 4. Quy ước trạng thái và bằng chứng

- `[ ]` chưa làm.
- `[~]` đang làm hoặc chỉ có một phần.
- `[x]` chỉ dùng sau khi có code, test và runtime evidence.
- Mỗi mục `[x]` phải ghi commit, lệnh test, kết quả, artifact/report và rollback.
- `ACTIVE` chỉ là model dùng trong operational alert sau approval.
- `SHADOW` chỉ tính điểm trên cùng dữ liệu; không tạo side effect.
- `CANDIDATE` là model đã đăng ký nhưng chưa đủ điều kiện shadow hoặc promotion.
- `DRIFT` là trạng thái chất lượng/model, không phải lệnh reset model.
- `BLOCKED` là fail-closed khi thiếu dữ liệu, thiếu label, lỗi license hoặc không
  đạt resource budget.

## 5. Kiến trúc đích

### 5.1 Identity bắt buộc

Chuẩn hóa một `ForecastScopeKey` dùng cho model, state, forecast run, evaluation,
drift và audit:

```text
cluster_id | entity_type | entity_id | host | metric | horizon_hours |
algorithm | model_version | feature_schema
```

Quy tắc:

- Node CPU/RAM: `entity_type=node`, bắt buộc `host`, `metric` và horizon.
- OSD/volume/pool: `entity_id` và host/pool/image theo loại resource; không dùng
  một state chung cho nhiều entity.
- Không dùng key chỉ gồm `cluster` hoặc chỉ `metric`.
- `host`/`metric` không được rơi mất khi serialize canary report, group evaluation,
  cache hoặc query Dashboard.
- Nếu scope cũ thiếu identity, report phải trả `MIGRATION_REQUIRED`/`UNKNOWN_SCOPE`,
  không gộp vào model mới.

### 5.2 Active, shadow và fallback

```text
active: linear/rolling baseline hiện tại
shadow: river_linear_v2, sn_arimax, HST/RRCF candidate
fallback: linear hoặc rolling baseline gần nhất đã được operator chấp thuận
```

Shadow sử dụng cùng timestamp, cùng input quality gate và cùng target outcome với
active. Candidate không được gọi `sync_forecast_alerts`, notification publisher,
executor hoặc remediation API.

### 5.3 Feature contract

Feature builder phải trả cả giá trị và provenance:

```json
{
  "features": {"current": 0.0, "lag_1": 0.0, "rolling_mean_6": 0.0},
  "feature_schema": "resource-v2",
  "observed_at": "...",
  "sample_count": 0,
  "coverage_ratio": 0.0,
  "max_gap_seconds": 0,
  "missing_features": [],
  "quality_status": "OK"
}
```

Không forward-fill qua gap vượt ngưỡng. Calendar feature dùng UTC đã chuẩn hóa;
lag/rolling chỉ lấy dữ liệu trước `observed_at`, tránh leakage.

## 6. Các phase triển khai

### Phase 0 — Baseline, identity và safety contract (P0)

#### 0.1 Sửa canary identity

- [x] Kiểm tra mọi API/report/query/group trong `ForecastModelRegistry`, persisted
  evaluation và canary report có giữ `host/entity + metric + horizon`.
- [x] Viết regression test cho hai host cùng metric, hai metric cùng host và hai
  horizon cùng scope; bảo đảm không gộp outcome.
- [x] Thêm `ForecastScope.canonical_key` và parser không đoán scope legacy hỏng.
- [x] Quét và backfill các row legacy parse được bằng migration
  `m20260921forecastscope`; row không parse được vẫn để nullable.
- [x] Chặn promotion nếu candidate có `UNKNOWN_SCOPE`, `MISSING_HOST_METRIC` hoặc
  mixed feature schema.

**Exit gate:** canary report hiển thị riêng từng host/metric/horizon; test suite
chứng minh không có cross-scope contamination.

#### 0.2 Chụp baseline

- [ ] Ghi MAE/RMSE/SMAPE/bias, false-positive rate, alert volume và data-quality
  rate theo từng scope.
- [ ] Ghi CPU time, wall time, peak RSS và kích thước state của active baseline.
- [ ] Ghi sample interval, history length, missing/gap rate và số outcome đã label.
- [ ] Lưu report bất biến trong `docs/benchmark/` hoặc artifact CI.

**Exit gate:** có baseline trước nâng cấp; mọi so sánh sau này là paired và không
được dùng số liệu aggregate để che scope xấu.

#### 0.3 Khóa side effect

- [ ] Test candidate không tạo `Incident`, `Action`, Telegram, executor task hoặc
  remediation case.
- [ ] Test restart Watcher/Worker không làm mất snapshot/registry/evaluation.
- [ ] Kiểm tra promotion và rollback vẫn yêu cầu operator approval.
- [ ] Đặt feature flag riêng cho từng candidate; mặc định `OFF` hoặc `SHADOW_ONLY`.

### Phase 1 — Feature/state layer và `river_linear_v2` (P0)

#### 1.1 Feature builder

- [x] Tạo module thuần, bounded, không gọi DB/SSH trong hàm transform tại
  `shared/forecast_features.py`.
- [x] Hỗ trợ `current`, lag `1/3/6/12/24`, rolling mean/std `6/24`, slope `6` và
  calendar sin/cos hour/weekday.
- [~] Cho phép cấu hình metric/horizon-specific feature set; feature builder đã
  được gọi trong runtime shadow nhưng cấu hình theo metric/horizon còn pending.
- [x] Bảo đảm thứ tự timestamp, timezone UTC, gap detection và no leakage.
- [x] Trả missing reason thay vì NaN/Inf; không forward-fill qua gap.
  Việc nối trực tiếp vào mọi runtime feature path còn ở bước sau; quality gate
  dùng `shared.metric_quality` vẫn là nguồn quyết định duy nhất.

#### 1.2 River model adapter

- [ ] Tạo interface chung: `predict_one`, `learn_one`, `score_one` nếu cần,
  `snapshot`, `restore`, `resource_usage`.
- [ ] Dùng `StandardScaler() | LinearRegression()` hoặc pipeline tương đương cho
  feature vector; xác định rõ thứ tự feature bằng `feature_schema`.
- [ ] Giới hạn state JSON, không pickle executable object; checksum và version
  mismatch phải fail-closed.
- [ ] Đặt model identity `river_linear_v2`, version và schema mới; không ghi đè
  snapshot `river_mean`.
- [ ] Cấu hình learning chỉ nhận outcome đã đủ quality/verified theo runtime gate.

#### 1.3 Horizon-specific model

- [ ] Node CPU/RAM tối thiểu hỗ trợ horizon `1h`, `6h`, `24h` theo cấu hình; volume
  giữ danh sách horizon hiện có nhưng phải dùng cùng contract.
- [ ] Mỗi horizon có state, target_at, evaluation và metrics riêng.
- [ ] Không suy ra kết quả `24h` từ model `1h` nếu chưa được đánh giá như một model
  riêng.
- [ ] Chặn horizon không đủ history bằng `INSUFFICIENT_SAMPLES`, không fallback im
  lặng sang horizon khác.

#### 1.4 Test và exit gate

- [ ] Unit test feature values, lag boundary, rolling warm-up, timezone, duplicate,
  gap, counter reset, missing feature và serialization round-trip.
- [ ] Property/fuzz test input cực lớn, âm, NaN, Inf, timestamp đảo chiều và empty
  series.
- [ ] Replay test đảm bảo v2 không đọc future sample.
- [ ] Resource gate: xác định ngưỡng CPU/RSS/state size trên máy production; nếu
  vượt thì giữ candidate ở `BLOCKED`.

**Exit gate:** `river_linear_v2` chạy shadow trên dữ liệu replay và runtime thật,
không side effect, có outcome riêng theo scope/horizon và không làm tăng latency
poll loop.

### Phase 2 — Residual ADWIN và drift state machine (P0)

#### 2.1 ADWIN adapter

- [x] Thêm adapter River ADWIN sau license/dependency review, có `snapshot`/`restore`
  hoặc cơ chế khởi tạo lại có audit.
- [~] Duy trì tối thiểu ba stream: residual có dấu đã nối vào runtime shadow;
  absolute error/MAE và metric gốc còn cần stream riêng.
- [x] ADWIN chỉ nhận sample đã có actual outcome và quality `OK`.
- [x] Lưu detector version, delta/confidence, sample count, detected_at và scope
  trong snapshot/report JSON bounded.

#### 2.2 Quy tắc phản ứng

- [ ] Drift residual: giảm confidence và đánh dấu candidate `DRIFT`.
- [ ] Drift MAE: chặn promotion và kéo dài cửa sổ đánh giá.
- [ ] Drift metric gốc: tạo evidence workload changed, không kết luận model hỏng.
- [ ] Không reset model ngay; yêu cầu operator/audited policy và đủ warm-up window.
- [ ] Sau drift, candidate chỉ trở lại `ELIGIBLE` khi có N outcome tốt liên tiếp,
  cấu hình được ghi trong registry.

#### 2.3 Tương thích drift hiện có

- [ ] Chạy ADWIN song song với `shared.forecast_drift.evaluate_drift` trong shadow.
- [ ] So sánh detection delay, false drift và alert suppression trên replay.
- [ ] Chỉ thay detector hiện tại sau paired benchmark và operator sign-off.

**Exit gate:** drift không tự gây alert/remediation, không làm mất active alert khi
quality xấu và có audit đầy đủ cho mọi chuyển trạng thái.

### Phase 3 — SNARIMAX shadow cho metric có seasonality (P1)

- [ ] Đánh giá API/version/license của River SNARIMAX trên Python 3.11 và 3.12.
- [ ] Chọn CPU/RAM/IOPS làm scope thử nghiệm; mỗi scope phải có seasonality và đủ
  history, nếu không thì `NOT_ELIGIBLE`.
- [ ] Xác định p/d/q, seasonal lag, error lag, exogenous features và giới hạn
  iteration theo benchmark; không cho config từ LLM.
- [ ] Persist prediction interval, training duration, sample count và model state
  bounded.
- [ ] Chạy chỉ trong background shadow job hoặc worker budget riêng, không block
  Watcher poll.
- [ ] Test restart, corrupted snapshot, timeout, circuit breaker và fallback về
  active baseline.

**Exit gate:** SNARIMAX có paired outcomes tốt hơn hoặc có early-warning benefit
được chứng minh mà không vượt resource budget; nếu không thì giữ `SHADOW`.

### Phase 4 — Multivariate anomaly candidate (P1)

#### 4.1 Feature vector

- [ ] Chuẩn hóa vector CPU, RAM, read/write IOPS, read/write latency, OSD apply
  latency và PG degraded ratio.
- [ ] Đồng bộ timestamp và quality từng thành phần; missing component phải làm
  giảm confidence hoặc bỏ sample, không zero-fill im lặng.
- [ ] Có entity peer baseline cho OSD/device class nếu đủ dữ liệu.

#### 4.2 Half-Space Trees

- [ ] Thử River HST với số cây/chiều cao/cửa sổ giới hạn.
- [ ] Đánh giá warm-up, score normalization, threshold calibration và clustered
  anomaly.
- [x] HST chỉ là anomaly candidate, không dự báo capacity/forecast target.

#### 4.3 RRCF

- [ ] Đưa RRCF vào benchmark environment tùy chọn, kiểm tra compatibility,
  maintenance và license.
- [ ] Giới hạn forest size, shingle/window, random seed, memory và eviction FIFO.
- [ ] So sánh HST với RRCF trên cùng replay; chưa thêm dependency production nếu
  không có owner bảo trì và security scan.

#### 4.4 Alert aggregation

- [ ] Chuẩn hóa anomaly score về contract chung và lưu top contributing features.
- [ ] Gom điểm theo incident window để tránh một event dài tạo hàng trăm alert.
- [ ] Candidate anomaly không gọi remediation; chỉ tạo `ANOMALY_CANDIDATE`/shadow
  evidence.

**Exit gate:** có event-level precision/recall/delay và resource profile; chọn tối
đa một detector vào phase canary.

### Phase 5 — Offline benchmark stack (P1)

#### 5.1 StatsForecast

- [~] Tạo benchmark/container hoặc optional extra riêng; không cài vào image
  Watcher nếu không cần runtime.
- [x] Benchmark Naive/SeasonalNaive/Linear bằng bounded offline runner; các model
  nặng Theta/AutoETS/AutoARIMA/MSTL vẫn chưa đưa vào image production.
  phù hợp; giới hạn parallelism để không tranh CPU production.
- [ ] Chạy job định kỳ 6–24 giờ hoặc on-demand trên dataset snapshot; lưu model
  config và report, không tự promote.

#### 5.2 PyOD

- [~] Benchmark anomaly baseline, River HST, Candidate D isolation và PyOD
  Isolation Forest nếu optional extra có sẵn; ECOD/COPOD/HBOS/PCA còn pending.
- [ ] Không chạy hàng chục detector trong poll loop; không để PyOD tự phát alert.
- [ ] Tách kết quả point-level và event-level, ghi rõ label quality.

#### 5.3 NAB scoring

- [ ] Import/adapt NAB scoring với version pin và license review.
- [ ] Mapping anomaly window cho CPU/RAM/IOPS/latency/capacity; không coi một điểm
  metric là một incident độc lập.
- [ ] Báo cáo event recall, mean detection delay, time-to-detect, false alerts/day,
  precision theo incident, CPU time và peak memory.
- [ ] Giữ dataset hiện tại 72 điểm làm regression fixture nhưng không coi đó là
  benchmark đủ; bổ sung synthetic + anonymized real series.

#### 5.4 sktime/Evidently tùy chọn

- [ ] Chỉ dùng sktime cho nghiên cứu/backtest khi cần model selection.
- [ ] Dùng Evidently trong job report định kỳ cho feature/prediction drift,
  MAE/SMAPE, missing/stale rate và interval coverage.
- [ ] Version/license/resource scan trước khi đưa vào image benchmark.

**Exit gate:** benchmark reproducible từ snapshot có checksum, cùng seed/config,
có report so sánh active/candidate và không tác động cluster.

### Phase 6 — Canary/shadow soak và release scan

- [x] Thêm `shared/canary_soak.py`: kiểm tra tối thiểu evaluations, thời lượng,
  drift, resource budget và `SHADOW_ONLY`; report read-only.
- [x] Thêm `scripts/forecast_release_scan.py`: kiểm tra dependency pin/license
  policy, cấm pickle/eval/exec/destructive Ceph command trong forecast runtime,
  và xác nhận JSON-only state/resource policy.
- [~] Chạy soak trên cluster/node thật trong tối thiểu 24 giờ; runtime shadow đã
  được deploy nhưng đủ 24 giờ evidence vẫn pending; chưa được tự promotion và
  chưa bật remediation.

- [x] Đã deploy migration scope lên server `10.3.55.213`; registry hiện có 192/192
  row ở `forecast-scope-v2`, không còn row thiếu dimension. Watcher và dashboard
  đã recreate và đều `healthy`.
- [x] Nối feature quality, Candidate D isolation và River ADWIN residual vào
  `watcher/node_resource_forecast.py` dưới `SHADOW_ONLY`; Watcher healthy sau
  recreate.
- [x] Dashboard model quality hiển thị paired outcomes, MAE/SMAPE/bias, drift và
  resource-budget failure theo model; Dashboard healthy sau recreate.

**Exit gate:** chỉ khi soak PASS, benchmark có report bất biến và operator ký
approval mới được gửi promotion request; rollback vẫn dùng model version trước.

### Phase 6 — Paired walk-forward evaluation và promotion (P0)

- [ ] Tất cả active/candidate dự báo cùng input window, target timestamp và quality
  decision.
- [ ] Tính MAE, RMSE, SMAPE, bias, p95 absolute error, interval coverage, event
  recall, detection delay, false-positive rate và alert volume theo scope.
- [ ] Không aggregate che mất regression của một host/metric/horizon; cần pass theo
  scope hoặc có policy ghi rõ ngoại lệ.
- [ ] Minimum sample, minimum evaluation streak và maximum data-quality rate phải
  cấu hình được trong registry.
- [ ] Promotion gate tối thiểu:
  - candidate đủ outcome và đủ evaluation streak;
  - MAE/SMAPE không kém active quá tolerance;
  - false-positive và alert volume không tăng ngoài budget;
  - interval coverage đạt ngưỡng;
  - không có drift `DRIFT`, `UNKNOWN_SCOPE` hoặc thiếu label bắt buộc;
  - CPU/RSS/state size nằm trong budget;
  - rollback artifact đã tồn tại.
- [ ] Operator approval ghi scope, model/version, evidence report, expiry và người
  duyệt.
- [ ] Promotion tạo audit append-only và cập nhật đúng một `ACTIVE` cho scope.
- [ ] Rollback dùng previous active version, kiểm tra health sau rollback và tạo
  audit event; không xóa candidate/evaluation history.

**Exit gate:** promotion/rollback có thể diễn tập trên staging và không có đường
code nào để worker tự promote hoặc candidate tự remediation.

### Phase 7 — Monitoring, canary và production sign-off (P0)

- [ ] Dashboard hiển thị scope đầy đủ, active/shadow version, feature schema,
  horizon, freshness, quality, drift, metrics và resource budget.
- [ ] Có report định kỳ 6/24 giờ cho model quality; report không gọi model training
  trong request UI.
- [ ] Canary chọn cluster/host/metric/horizon cụ thể; không canary toàn bộ cluster
  mặc định.
- [ ] Chạy ít nhất một chu kỳ đầy đủ cho từng horizon trước khi xem xét promotion.
- [ ] Staging rehearsal: deploy, migrate nếu cần, snapshot registry, restart
  Watcher/Worker, rollback model, restore DB evidence.
- [ ] Canary soak không tăng alert spam, không làm stale source thành NORMAL và
  không ảnh hưởng latency/health loop.
- [ ] Security/license/dependency/image scan và operator sign-off hoàn tất trước
  khi thêm dependency runtime.
- [ ] Cập nhật release manifest, runbook và completed plan chỉ sau khi có evidence.

## 7. Dependency và packaging policy

| Thư viện | Vị trí cho phép | Điều kiện |
|---|---|---|
| River | production online/shadow | pin version, snapshot safe, CPU/RAM test |
| StatsForecast | benchmark job/container | không chạy mỗi poll, license/scan |
| PyOD | offline benchmark | không alert trực tiếp, pin version |
| NAB scorer | benchmark/report | kiểm tra license, checksum dataset |
| RRCF | candidate benchmark trước | owner bảo trì, memory/license/compatibility |
| Evidently | periodic monitoring job | report bounded, không trong request/poll |
| sktime | research/offline | optional, không production dependency mặc định |
| PySAD | interface/reference | chỉ import nếu có use case và scan đạt |

Mọi dependency mới phải có SBOM, license result, vulnerability result, Python
3.11/3.12 result và rollback bằng cách tắt feature/extra. Không dùng `pip install`
động trong container production.

## 8. Resource budget và vận hành

Trước khi chốt con số, đo trên host/server thật và lưu baseline. Các giới hạn bắt
buộc phải có config hard cap:

- poll loop không chờ benchmark/training dài;
- số sample mỗi learning cycle, timeout, retry và circuit breaker hữu hạn;
- window/forest/tree/parallelism cố định hoặc bounded;
- model state có kích thước tối đa và checksum;
- queue background có backpressure, không tăng vô hạn;
- CPU time/RSS và latency p95 được ghi theo model;
- khi vượt budget: candidate chuyển `BLOCKED`, active/fallback tiếp tục chạy.

## 9. Kiểm thử bắt buộc

### Unit/contract

- scope key, host/metric/horizon isolation;
- feature leakage, lag/rolling/calendar, gap/reset/missing;
- snapshot/checksum/version migration;
- ADWIN state, drift hysteresis, no immediate reset;
- score normalization và event aggregation;
- promotion/rollback guard.

### Integration

- Watcher/Worker chạy shadow không side effect;
- DB restart/migration/restore giữ registry và evaluation;
- canary API không mutate state;
- fallback khi River/SNARIMAX/HST/RRCF timeout, corrupt hoặc dependency lỗi;
- Python 3.11 và 3.12.

### Replay/benchmark

- synthetic trend/seasonality/spike/clustered anomaly;
- anonymized CPU/RAM/IOPS/latency thực tế;
- missing/stale/gap/counter reset;
- NAB early/late/missed event scoring;
- paired walk-forward active vs candidate;
- CPU time, wall time, peak RSS, state size.

### Release quality

- full deterministic suite không lỗi theo thứ tự;
- Ruff/mypy/Bandit/dependency/image/SBOM gate theo policy hiện hành;
- không có secret trong dataset/artifact;
- report có checksum và version của dataset, code, model, dependency.

## 10. Rollback và failure handling

- Candidate lỗi: tắt feature flag, giữ active baseline.
- Snapshot lỗi/schema mismatch: không load partial state; tạo fresh candidate ở
  `BLOCKED` và audit lý do.
- Drift: giảm confidence/hold alert theo policy; không xóa history.
- Benchmark lỗi: report `INCOMPLETE`, không có promotion decision.
- Dependency vulnerability/license fail: không build runtime image mới; quay về image
  trước.
- Promotion regression: rollback previous active, verify alert path và ghi incident.
- DB/migration failure: dùng backup/restore drill theo production readiness plan;
  không downgrade production tùy tiện.

## 11. Thứ tự thực thi và commit slices

1. `P0-identity`: canary scope key, host/metric/horizon regression, legacy report.
2. `P1-features`: feature builder và data contract tests.
3. `P2-river-v2`: linear v2, snapshots, horizon models, shadow-only integration.
4. `P3-drift`: ADWIN adapter và paired drift report.
5. `P4-snarimax`: shadow candidate có resource budget.
6. `P5-anomaly`: HST trước, RRCF sau khi review đạt.
7. `P6-benchmark`: StatsForecast/PyOD/NAB offline container và dataset manifest.
8. `P7-promotion`: paired walk-forward gate, approval, rollback rehearsal.
9. `P8-monitoring`: dashboard/report/Evidently tùy chọn và canary sign-off.

Mỗi slice phải đi qua test/quality gate riêng. Không gộp migration, dependency
runtime và promotion policy vào một commit lớn khó rollback.

## 12. Ma trận nghiệm thu cuối

| Gate | Tiêu chí | Trạng thái ban đầu |
|---|---|---|
| Identity | Không mất host/entity, metric, horizon | `[ ]` |
| Feature | Không leakage, quality fail-closed | `[ ]` |
| River v2 | Shadow có outcome riêng, state bounded | `[ ]` |
| Drift | ADWIN/old detector paired, không auto-reset | `[ ]` |
| SNARIMAX | Chỉ shadow, có resource profile | `[ ]` |
| Anomaly | HST/RRCF event benchmark và explainability | `[ ]` |
| Offline benchmark | StatsForecast/PyOD/NAB reproducible | `[ ]` |
| Monitoring | quality/drift/MAE/interval report | `[ ]` |
| Promotion | operator approval, audit, rollback | `[ ]` |
| Production | staging/soak/security/license/runtime sign-off | `[ ]` |

## 13. Những việc làm ngay sau khi Plan được duyệt

1. Hoàn thành `P0-identity`; đây là blocker trước mọi model mới.
2. Chốt dataset manifest và baseline resource profile.
3. Implement feature builder thuần và test leakage/gap/reset.
4. Implement `river_linear_v2` ở `SHADOW_ONLY`, không thay `river_mean` ngay.
5. Chạy replay + targeted tests trên Python 3.11/3.12.
6. Chỉ sau khi scope/horizon outcomes đúng mới bắt đầu ADWIN/SNARIMAX/anomaly.

Không chuyển mục nào sang `Plan/completed/` chỉ vì dependency đã cài hoặc UI đã
hiển thị. Chỉ chuyển sau khi exit gate và runtime evidence tương ứng đạt.
