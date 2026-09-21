# Kế hoạch nâng cấp Self-learning & Predictive Alerting cho Ceph-AI

## Mục tiêu

Xây dựng vòng lặp hoàn chỉnh:

Metrics/Logs -> Data-quality gate -> Multi-model anomaly/forecast -> Consensus + confidence -> Alert state machine -> Operator/remediation outcome -> Evaluation + feedback -> Shadow model -> Promotion có kiểm soát.

Mục tiêu là giảm false positive, dự báo sớm hơn và chỉ cho phép model mới được dùng khi có đủ bằng chứng. Không bật auto-remediation mới trong lúc thay đổi model.

## Quy tắc chung

- [ ] Mọi model mới phải chạy shadow trước khi ảnh hưởng tới cảnh báo thật.
- [ ] Mọi quyết định phải lưu input window, data quality, model/version, confidence, prediction, actual outcome và lý do chuyển trạng thái.
- [ ] Dữ liệu stale, thiếu mẫu hoặc có gap lớn phải trả DATA_QUALITY, không được kết luận là hệ thống bình thường.
- [ ] Không xóa dữ liệu learning/forecast cũ trước khi có báo cáo và backup.
- [ ] Candidate model không được tự ý thay đổi policy RISKY/DESTRUCTIVE.

---

## Phase 0 — Baseline và khóa phạm vi

### Bước 0.1 — Chụp baseline hiện tại

- [x] Ghi số lượng model CPU/RAM/Volume.
- [x] Ghi evaluated, pending, MAE/SMAPE theo host, metric và window.
- [x] Ghi số alert OPEN, RESOLVED, DATA_QUALITY và số lần gửi Telegram.
- [x] Ghi log-learning sample, eligible, verified, success, failure.
- [x] Ghi remediation feedback: tổng case, labeled, scored, precision và coverage.
- [x] Xuất baseline thành Markdown có timestamp: Plan/baseline-self-learning-2026-09-18.md.
- [x] Ghi danh sách host/metric đang stale hoặc thiếu lịch sử; 6/6 host dưới ngưỡng stale 1,800 giây.

Tiêu chí hoàn thành:

- Có báo cáo baseline có timestamp.
- Chỉ đọc và thống kê, không thay đổi production logic.

Bước tiếp theo: Bước 0.2 — Định nghĩa data contract.

### Bước 0.2 — Định nghĩa data contract

- [x] Chuẩn hóa metric: cpu, ram, disk_iops, disk_latency, pool_used_percent, rbd_used_percent.
- [x] Chuẩn hóa timestamp UTC và quy tắc deduplicate.
- [x] Định nghĩa sample interval mong muốn.
- [x] Định nghĩa max_age_seconds, minimum_samples, minimum_coverage_ratio, maximum_gap_seconds và minimum_history_seconds.
- [x] Chuẩn hóa kết quả quality: OK, STALE, INSUFFICIENT_SAMPLES, LOW_COVERAGE, GAP_DETECTED, SOURCE_ERROR.

Tiêu chí hoàn thành:

- Có contract dùng chung cho Loki, Prometheus và database samples trong shared/metric_quality.py.
- Có 14 test cho dữ liệu hợp lệ, stale, duplicate, gap, coverage thấp, source error và ngưỡng không hợp lệ.

Bước tiếp theo: Phase 1 — Data-quality gate.

---

## Phase 1 — Data-quality gate

### Bước 1.1 — Tạo module quality dùng chung

- [x] Tạo shared/metric_quality.py.
- [x] Trả về status, latest_observed_at, age_seconds, sample_count, coverage_ratio, longest_gap_seconds và reason.
- [x] Không để mỗi watcher tự đánh giá quality theo cách khác nhau khi tích hợp.
- [x] Thêm unit test cho từng trạng thái quality.

### Bước 1.2 — Tích hợp CPU/RAM

- [x] Tích hợp vào watcher/node_resource_forecast.py; node_health_monitor.py gọi qua adaptive_forecast.
- [x] Tách rõ no forecast và data quality failure bằng MetricQualityStatus.
- [x] Không resolve alert chỉ vì scan hiện tại không lấy được dữ liệu.
- [x] Ghi quality status/reason persistent vào node_resource_quality_states; alert cũ được giữ nguyên khi nguồn stale để không resolve nhầm.

### Bước 1.3 — Tích hợp capacity/volume

- [x] Tích hợp quality gate vào watcher/capacity_forecast.py; series stale/gap bị loại khỏi forecast.
- [x] Capacity forecast có test cho stale series và large gap.
- [x] Tích hợp vào volume-learning implementation đang chạy trên server.
- [x] Chặn volume forecast khi lịch sử chưa đủ hoặc gap vượt ngưỡng.
- [x] Hiển thị trên UI status DATA_QUALITY và reason chi tiết.

Tiêu chí hoàn thành Phase 1:

- Alert không tự resolve khi nguồn dữ liệu bị stale.
- Dashboard phân biệt NORMAL, NO_FORECAST và DATA_QUALITY.
- Có hồi quy cho các node đang có Loki gap.

Bước tiếp theo: Phase 2 — Multi-model consensus.

---

## Phase 2 — Multi-model anomaly detection và forecasting

### Bước 2.1 — Giữ linear forecast làm baseline

- [x] Giữ model linear hiện tại để không gián đoạn hệ thống; CPU/RAM vẫn dùng linear làm candidate baseline và volume vẫn giữ seasonal baseline cũ.
- [x] Tạo interface consensus dùng chung cho các candidate forecast; volume đã lưu `consensus_status`, `consensus_ratio`, số candidate và prediction interval.
- [x] Bổ sung rolling median/quantile baseline theo host và metric (`rolling_quantile`).
- [x] Tính residual giữa actual và expected.
- [x] Tính anomaly score robust bằng MAD, giới hạn score để tránh giá trị vô hạn.

Bằng chứng kiểm thử bước đã hoàn thành:

- `tests/test_forecast_consensus.py`: median, tolerance, disagreement và minimum-candidate gate.
- `tests/test_node_resource_forecast.py`: CPU/RAM lưu consensus metadata và chặn low-consensus alert.
- `tests/test_volume_learning.py`: volume chạy nhiều training windows, fail-closed khi chỉ có một candidate hoặc candidate bất đồng.
- `b4c5d6e7f8a9_add_volume_forecast_consensus_metadata.py`: migration lưu provenance và prediction interval.

### Bước 2.2 — Thêm candidate model nhẹ

- [x] Candidate A: linear regression hiện tại.
- [x] Candidate B: robust rolling median/quantile.
- [x] Candidate C: seasonal baseline theo giờ/ngày đã được giữ trong volume learning; phần seasonal cho CPU/RAM vẫn để riêng vì chưa đủ bằng chứng chu kỳ.
- [~] Candidate D: bounded multivariate isolation score đã có trong
  `shared/forecast_anomaly.py` và offline benchmark; chưa nối vào runtime shadow
  để không phát alert/remediation ngoài approval.
- [ ] Chưa đưa deep learning vào phase đầu vì dữ liệu label chưa ổn định.

### Bước 2.3 — Consensus kiểu Netdata

- [x] Chạy nhiều candidate trên cùng input window.
- [x] Mỗi model trả prediction, interval, confidence và quality.
- [x] Tạo trạng thái `CANDIDATE` (ANOMALY_CANDIDATE) cho tín hiệu có upper bound/anomaly nhưng consensus chưa đủ; trạng thái này không gửi Telegram và không chạy remediation.
- [x] Nếu model bất đồng mạnh, trả `LOW_CONFIDENCE` và không gửi critical alert.
- [x] Lưu `model_votes_json`, `consensus_ratio` và interval để audit.

### Bước 2.4 — Prediction interval

- [x] Bổ sung `predicted_low` và `predicted_high` cho node/volume forecast và node alert persistence.
- [x] Cảnh báo khi upper bound vượt ngưỡng trong thời gian yêu cầu; khi không có thời điểm crossing cụ thể, dùng horizon làm cận trên bảo thủ.
- [ ] Hiển thị vùng dự báo trên biểu đồ CPU/RAM/volume.

Tiêu chí hoàn thành Phase 2:

- Mỗi forecast có prediction, interval, confidence, quality và consensus.
- Có replay test trên lịch sử.
- So sánh model cũ và consensus bằng MAE/SMAPE/false-positive rate.
- Model mới vẫn chỉ chạy shadow.

Bước tiếp theo: Phase 3 — Alert state machine.

---

## Phase 3 — Alert lifecycle thống nhất

### Bước 3.1 — Chuẩn hóa trạng thái

- [x] Dùng NORMAL, CANDIDATE, WARNING, CRITICAL, RECOVERING, RECOVERED, DATA_QUALITY và SUPPRESSED trong `shared/predictive_alert_lifecycle.py`.
- [x] Ghi `state_changed_at`, `state_reason` và `evidence_version`.
- [x] Tách `lifecycle_state` khỏi `notification_state` (`IDLE`, `SENT`, `COOLDOWN`, `FAILED`, `SUPPRESSED`) trong `node_resource_forecast_alerts`.

### Bước 3.2 — Hysteresis và consecutive breach

- [x] Không mở warning từ một điểm dữ liệu đơn lẻ; mặc định cần 2 breach liên tiếp.
- [x] Yêu cầu N lần breach liên tiếp; lần đầu lưu `CANDIDATE`, không gửi notification.
- [x] Recovery yêu cầu 2 lần healthy liên tiếp và đi qua `RECOVERING`/`RECOVERED`.
- [x] Recovery threshold thấp hơn trigger threshold về giá trị metric: trigger mặc định 90%, recovery mặc định 85%; chỉ bắt đầu recovery khi upper bound xuống dưới recovery threshold.

### Bước 3.3 — Dedupe, cooldown và maintenance

- [x] Dedupe theo cluster, entity, metric và `evidence_fingerprint`; lưu fingerprint cuối cùng đã thông báo.
- [x] Thêm cooldown Telegram theo severity: WARNING và CRITICAL có thời gian riêng.
- [x] Hỗ trợ maintenance/suppression window qua `suppressed_until`, fail-closed notification.
- [x] Reconcile alert đang mở nhưng không còn evidence bằng recovery hysteresis.

Tiêu chí hoàn thành Phase 3:

- Không gửi lặp cùng alert trong cooldown.
- Alert chỉ resolve sau đủ evidence recovery hoặc quality result hợp lệ.
- Có test mở, nâng severity, hạ severity, recovery, stale source và duplicate scan.

Bước tiếp theo: Phase 4.1 — nối forecast với actual outcome và tính rolling MAE/RMSE/SMAPE/bias.

---

## Phase 4 — Feedback loop và self-learning thật sự

### Bước 4.1 — Ghi nhận kết quả forecast

- [x] Mỗi forecast được nối với actual outcome sau horizon.
- [x] Tính MAE, RMSE, SMAPE và bias theo model/window/host/metric.
- [x] Lưu rolling metrics có giới hạn cửa sổ, không chỉ MAE toàn lịch sử.

### Bước 4.2 — Ghi nhận kết quả alert

- [x] Thêm operator verdict: TRUE_POSITIVE, FALSE_POSITIVE, NOT_ACTIONABLE, UNKNOWN.
- [x] Liên kết feedback với incident và remediation case bằng ID, không tự thay đổi lifecycle/policy.
- [x] Ghi note nguyên nhân và impact percent trong bảng feedback append-only.
- [x] API AI Learning tổng hợp verdict count, precision và coverage; recall được báo rõ là chưa đo được.

### Bước 4.2b — Ground truth và recall

- [x] Xây dựng ground-truth tạm thời từ evaluated forecast outcome vượt trigger threshold; ghi rõ giới hạn telemetry.
- [x] Liên kết forecast feedback với Incident/RemediationCase bằng validation tồn tại và cùng cluster.
- [x] Tính recall bảo thủ từ ground-truth evaluated outcomes; vẫn cần telemetry độc lập để đo false negatives đầy đủ.

### Bước 4.3 — Hoàn thiện log learning

- [x] Giữ fail-closed: chưa verified không được tăng trust.
- [x] Có UI admin để operator label sample chưa xác minh, kèm audit event.
- [x] Hiển thị state, label và `exclusion_reason` vì sao sample chưa eligible.
- [x] Chỉ sample có verified outcome hoặc verified negative mới được aggregate trust; aggregate vẫn audit-only.

### Bước 4.4 — Làm đầy remediation feedback

- [x] Tìm nguyên nhân feedback đang có zero labeled/scored: summary cũ chỉ đếm `operator_verdict`, nên telemetry outcome đã verified vẫn bị hiển thị như chưa có label/scored.
- [x] Bổ sung reconcile giữa Incident, Action, RemediationCase và learning sample; ưu tiên `recommended_action_id`, không chọn nhầm case mới nhất khi một Incident có nhiều Action, và fail-closed khi mapping mơ hồ.
- [x] Tách rõ `VERIFIED_SUCCESS`, `VERIFIED_FAILED`, `EXECUTION_FAILED`, `INCONCLUSIVE`, `REGRESSED` cùng `exclusion_reason`; chỉ outcome đủ bằng chứng mới eligible cho learning.
- [x] Bổ sung telemetry truth riêng với operator feedback trên API/UI để chẩn đoán đúng zero operator label mà không biến outcome thành operator verdict.
- [x] Test success, failure, inconclusive, regression, missing/partial telemetry và multi-Action mapping: local `31 passed`; production-side regression suite `28 passed`.

Tiêu chí hoàn thành Phase 4:

- Forecast có outcome được đánh giá tự động.
- Alert có thể được operator label.
- Có precision, recall và coverage theo metric/loại alert.
- Feedback pipeline không còn tồn tại nhưng không có label do thiếu liên kết.

Bước tiếp theo: Bước 5.1 — xây dựng model registry cho candidate/active/shadow/retired/blocked.

---

## Phase 5 — Model selection, shadow và promotion

### Bước 5.1 — Model registry

- [x] Mỗi model có name, version, algorithm, feature_schema và training_window trong `ForecastModelRegistry`.
- [x] Trạng thái gồm CANDIDATE, SHADOW, ACTIVE, RETIRED, BLOCKED; helper lifecycle không tự chọn model và không tự promotion.
- [x] Lưu lý do promote hoặc block, cùng `active_since`/`retired_at`; identity unique theo scope + name + version.
- [x] Migration `c0d1e2f3a456_create_forecast_model_registry.py` đã chạy trên server; helper application-layer chặn một scope có nhiều model ACTIVE, không thay đổi migration graph production.

### Bước 5.2 — Shadow evaluation

- [x] Candidate node (`linear`/`rolling_quantile`) và candidate volume theo các training window chạy song song với active model; persisted forecast runs được ghép theo cùng `target_at`.
- [x] Worker so sánh candidate với active trên các run đã `EVALUATED`, tính MAE/RMSE/SMAPE/bias và precision/false-positive rate; không `commit`, không notification và không remediation (`execution_mode=SHADOW_ONLY`).
- [x] Có replay/backtest read-only và persisted-run comparison tests; affected suite đã đạt `42 passed`. Production worker đã chạy thực tế, trả về các trạng thái `HOLD`/`PROMISING` và vẫn healthy.
- [x] Log shadow được gom theo summary để tránh spam: chỉ log số comparison, phân bố status và tối đa 10 candidate `PROMISING`.

### Bước 5.3 — Guarded promotion

- [x] Đạt số outcome tối thiểu: mặc định `20` outcome cho cả active và candidate.
- [x] MAE/SMAPE tốt hơn active model trong `3` evaluation liên tiếp trên các target timestamp mới.
- [x] False-positive rate không được tăng; thiếu metric bắt buộc fail-closed.
- [x] Operator approval là bắt buộc cho mọi promotion; không có auto-promotion từ worker.
- [x] Có rollback về đúng model version trước, cập nhật lại runtime `selected` state.
- [x] Có append-only evaluation history và promotion audit (`PROMOTION_REQUESTED`, `PROMOTION_BLOCKED`, `PROMOTED`, `ROLLED_BACK`, `ROLLBACK_BLOCKED`).
- [x] Dashboard hiển thị ACTIVE/SHADOW/BLOCKED, guard reason, nút đề xuất/duyệt/rollback; candidate shadow không gửi notification và không remediation.

Tiêu chí hoàn thành Phase 5:

- Promote/rollback có audit log.
- Candidate không thể tự bật remediation.
- Dashboard hiển thị active model, shadow model và lý do blocked.

Bước tiếp theo: Bước 6.1 — dashboard forecast detail: actual, predicted, prediction interval, model/version, confidence, consensus và data quality.

---

## Phase 6 — Dashboard và vận hành

### Bước 6.1 — Forecast detail

- [x] Hiển thị actual, predicted và prediction interval cho forecast CPU/RAM và RBD volume; actual còn thiếu được hiển thị rõ là đang chờ outcome.
- [x] Hiển thị model/window/algorithm/version; node lấy version từ model registry ACTIVE, volume lấy version persisted trên forecast.
- [x] Hiển thị confidence, consensus ratio/số candidate và data quality (`OK`, `NO_FORECAST`, `DATA_QUALITY`, `LOW_CONFIDENCE`).
- [x] Hiển thị MAE/SMAPE gần nhất từ rolling metrics của model state.

### Bước 6.2 — Alert explanation

- [x] Hiển thị lý do alert mở từ `state_reason` và evidence version.
- [x] Hiển thị số model đồng thuận, consensus ratio và prediction interval.
- [x] Hiển thị freshness, coverage và gap; quality evidence được lưu cùng node forecast/alert.
- [x] Hiển thị tối đa 10 state transition gần nhất từ append-only transition history.

### Bước 6.3 — Replay/backtest

- [x] Cho chọn cluster, host, metric, time range, horizon và training windows trên dashboard.
- [x] Chạy lại model trên `HostMetricSample` theo hourly points bằng helper replay thuần, có giới hạn 31 ngày và 5.000 raw rows.
- [x] So sánh active linear với candidate rolling-quantile, hiển thị MAE/RMSE/SMAPE/bias và false-positive rate.
- [x] Replay không ghi model state, không tạo alert, không gửi notification và không thực thi remediation.

### Bước 6.4 — Safeguards

- [x] Rate limit learning job theo `(cluster, pool, image)`; có non-overlap guard sẵn có ở Watcher.
- [x] Giới hạn batch size và Loki query duration bằng settings hard cap (`learning_job_max_batch_size`, `learning_job_timeout_seconds`).
- [x] Timeout, bounded retry và circuit breaker cho Loki push/query; lỗi fail-closed, không làm dừng health/remediation loop.
- [x] Retention cho raw host/volume sample, forecast run, terminal forecast evidence, transition/audit history; không xoá active alert/model state/open operator work.
- [x] Có script/runbook backup database trước migration: `scripts/deploy/backup_database_before_migration.sh` và `docs/learning-safeguards.md`.

Tiêu chí hoàn thành Phase 6:

- Operator hiểu vì sao có cảnh báo.
- Có thể kiểm tra model bằng replay.
- Production không bị ảnh hưởng khi Loki/AI API/database chậm.
- Có runbook cho stale data, model regression và alert spam.

Bước tiếp theo: Phase 7 — Canary production và nghiệm thu.

---

## Phase 7 — Canary và nghiệm thu

- [x] Có báo cáo canary read-only theo cluster đã chọn: `GET /api/ai-learning/canary`.
- [x] Báo cáo candidate shadow qua các evaluation đã persist; không tự chạy hoặc tự promotion.
- [x] So sánh MAE/SMAPE/false-positive rate, alert volume và DATA_QUALITY rate; precision/recall/early detection được đánh dấu thiếu nếu chưa có outcome label.
- [x] Registry/evaluation là durable state; có test bảo đảm report không mutate state.
- [x] Có runbook kiểm tra migration downgrade/backup trong `docs/forecast-canary-acceptance.md`; production drill vẫn cần operator chạy.
- [x] Promotion yêu cầu operator approval riêng; không bật auto-remediation.
- [x] Rollback dùng previous active model từ promotion audit và ghi audit mới.

#### Operator acceptance vẫn còn phải thực hiện

- [ ] Chọn một cluster/nhóm node cụ thể làm canary và chạy đủ một chu kỳ forecast.
- [x] Restart cả Watcher/Worker rồi xác nhận registry/evaluation state không mất (`96` registry rows và `180` evaluation rows giữ nguyên; cả hai container healthy).
- [x] Tạo và xác minh PostgreSQL custom backup trước migration: `/var/backups/ceph-ai/ceph-ai-20260918T095529Z.dump` (`0600`, khoảng 66 MB, `589` entries đọc được bằng `pg_restore --list`). Đã cài PostgreSQL client 18 để khớp server 18.4.
- [x] Restore drill và migration downgrade/upgrade đã chạy thành công trên PostgreSQL 18 ephemeral container: restore `95` bảng từ backup, `c1d2e3f4a5b7` → `b5c6d7e8f9a0` → `e4f5a6b7c8d9`. Extension `pgaudit` được loại riêng khỏi danh sách restore test vì image kiểm thử không cài extension này; production dump vẫn giữ nguyên entry.

Evidence hiện tại: cluster mặc định `CS-LAB` có `72` shadow candidates; cả `72/72` promotion guards đang bị chặn vì chưa đủ evaluation streak hoặc chưa đạt MAE/SMAPE/false-positive guard. Không có candidate nào được promote.

Tiêu chí nghiệm thu cuối:

- Không tăng alert spam.
- Không cảnh báo sai do stale data.
- Feedback label và remediation outcome hoạt động.
- Model promotion có audit và rollback.
- Không bật auto-remediation mới ngoài policy đã duyệt.

---

## Thứ tự ưu tiên thực tế

1. Phase 0 và Phase 1: xử lý stale/gap, chuẩn hóa quality.
2. Phase 3: ổn định alert lifecycle và chống spam.
3. Phase 2: consensus và prediction interval.
4. Phase 4: giải quyết feedback đang có zero label.
5. Phase 5–7: promotion, UI, replay và canary.

Không nên bắt đầu bằng deep-learning model mới. Với dữ liệu hiện tại, quality gate và feedback loop sẽ đem lại hiệu quả cao hơn.

## Trạng thái thực hiện

- [x] Đã rà cấu trúc hiện tại và xác định module forecast, learning, feedback.
- [x] Đã xác định baseline issue: data quality, feedback chưa có label và log learning đang audit-only.
- [x] Đã tạo kế hoạch triển khai.
- [x] Bước 0.1 — Chụp baseline hiện tại đã hoàn thành.
- [x] Bước 0.2 — Định nghĩa data contract đã hoàn thành.
- [x] Bước 1.1 — Tạo module quality dùng chung và test đã hoàn thành.
- [x] Bước 1.2a — Chặn forecast khi CPU/RAM history không đạt quality đã hoàn thành.
- [x] Bước 1.2b — Lưu quality state CPU/RAM persistent và kiểm thử migration đã hoàn thành.
- [x] Bước 1.3a — Capacity forecast quality gate và regression tests đã hoàn thành.
- [x] Bước 1.3b — Volume quality gate, UI reason và server-side regression tests đã hoàn thành.
- [x] Bước 2.1 — Rolling median/quantile baseline, residual và MAD anomaly score đã hoàn thành.
- [x] Bước 2.2a — Candidate A linear và Candidate B rolling-quantile đã hoàn thành; volume giữ seasonal baseline hiện có.
- [x] Bước 2.3a — Multi-model consensus, fail-closed LOW_CONFIDENCE và model-vote persistence đã hoàn thành.
- [x] Bước 2.4a — Prediction interval được lưu cho node/volume forecast và alert đã hoàn thành.
- [x] Bước 2.3b — Chuẩn hóa CANDIDATE và dùng upper bound trong quyết định cảnh báo đã hoàn thành.
- [x] Bước 2.4b — Replay/backtest read-only và metrics so sánh candidate đã hoàn thành.
- [x] Bước 3.1 — Chuẩn hóa lifecycle state và tách notification state; migration `d7e8f9a0b1c2` đã chạy trên server.
- [x] Bước 3.2 — Consecutive breach/recovery hysteresis đã hoàn thành; runtime watcher đã restart và healthy.
- [x] Bước 3.3 — Dedupe theo evidence, cooldown theo severity và suppression window; migration `f1a2b3c4d5e6` đã bổ sung fingerprint notification.
- [x] Bước 3.2b — Value hysteresis trigger/recovery 90%/85% đã hoàn thành; runtime watcher đã restart và healthy.
- [x] Bước 4.1 — Outcome scoring rolling MAE/RMSE/SMAPE/bias cho node và volume; migration `a2b3c4d5e6f7` đã chạy trên server.
- [x] Bước 4.2 — Append-only operator verdict, feedback API, precision/coverage summary; migration `b7c8d9e0f1a2` đã chạy trên server.
- [x] Bước 4.4 — Reconcile remediation feedback, tách operator label khỏi telemetry truth; local `31 passed`, production-side `28 passed`.
- [x] Bước 5.1 — Forecast model registry và lifecycle metadata; migration `c0d1e2f3a456` đã chạy trên server, registry test đạt.
- [x] Bước 5.2 — Shadow evaluation read-only cho active/candidate; local affected suite `42 passed`, production worker đã verified `SHADOW_ONLY`; log đã được aggregate để không gây noise.
- [x] Bước 5.3 — Guarded promotion: đủ outcome, MAE/SMAPE streak, false-positive guard, operator approval, append-only audit và rollback; production smoke test đạt.
- [x] Bước 6.1 — Dashboard forecast detail: actual/predicted/interval/model/consensus/data quality và MAE/SMAPE đã hiển thị cho CPU/RAM và RBD volume; dashboard test đạt `10 passed`.
- [x] Bước 6.2 — Alert explanation: lý do mở, consensus, freshness/coverage/gap và lịch sử lifecycle transition đã hiển thị; regression suite đạt `48 passed` trước khi bổ sung test transition.
- [x] Bước 6.3 — Replay/backtest UI và read-only API đã hoàn thành; dashboard/replay test đạt `14 passed`, toàn bộ affected suite không tạo side effect.

### Bước tiếp theo sẽ làm

Bước tiếp theo: nối scope/feature/ADWIN/Candidate D vào runtime shadow, sau đó chạy
canary soak thực tế với operator approval; không tự bật candidate thành active.

### Nhật ký cập nhật 2026-09-21

- Đã thêm migration `m20260921forecastscope` cho explicit registry dimensions;
  backfill legacy chỉ khi parse được, không đoán scope lỗi.
- Đã thêm `shared/forecast_features.py`, Candidate D isolation, River ADWIN,
  forecast benchmark nhiều model, shadow soak gate và release security/license/
  resource scan.
- Bằng chứng: `33 passed` cho migration + model registry + drift + scope/features
  + benchmark/soak; py_compile và `git diff --check` đạt; release scan trả `PASS`.
- Còn lại: runtime integration, 3 ADWIN streams, quality dashboard nâng cao,
  24-hour live soak và operator acceptance trên cluster Ceph thật.
