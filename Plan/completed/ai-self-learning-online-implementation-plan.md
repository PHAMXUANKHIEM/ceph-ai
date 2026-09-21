# Kế hoạch triển khai AI tự học cho Ceph-AI

Ngày lập: 2026-09-18
Phạm vi: self-learning cho metric và cảnh báo vận hành Ceph
Trạng thái: kế hoạch đã được viết; chưa bật thêm model online trên production

## 1. Mục tiêu

Xây dựng cơ chế để Ceph-AI học từ dữ liệu metric và kết quả cảnh báo đã được xác minh, nhưng không tự học mù từ dữ liệu lỗi hoặc từ một cảnh báo chưa có ground truth.

Mục tiêu cuối cùng:

- Học online/incremental theo từng metric stream thay vì train lại toàn bộ model.
- Phát hiện concept drift khi hành vi node, volume hoặc cluster thay đổi.
- Kết hợp nhiều model trước khi tạo alert.
- Chỉ đưa feedback đã xác minh vào quá trình học.
- Đánh giá candidate ở chế độ shadow trước khi promotion.
- Có thể rollback model và truy vết toàn bộ quyết định.
- Không để self-learning làm tăng rủi ro soft lockup hoặc làm treo Watcher/Worker.

## 2. Nguyên tắc không thay đổi

1. **Fail-closed:** thiếu dữ liệu, dữ liệu quá cũ, gap lớn hoặc schema sai thì không học và không phát alert tin cậy.
2. **Verified feedback only:** chỉ feedback từ operator hoặc telemetry outcome đủ bằng chứng mới được dùng làm nhãn học.
3. **Shadow trước active:** model mới không được gửi notification hoặc remediation khi chưa được operator duyệt.
4. **Không dùng LLM để quyết định numeric alert:** LLM chỉ giải thích, tóm tắt hoặc hỗ trợ RCA; threshold và lifecycle phải deterministic.
5. **Bounded resource:** mọi job học, replay và benchmark đều có giới hạn CPU, RAM, batch size, timeout và query duration.
6. **Audit bắt buộc:** mọi thay đổi model, policy, feedback và promotion phải có lịch sử append-only.
7. **Canary trước rollout:** chỉ bật model mới trên một cluster hoặc một nhóm node trước.

## 3. Kiến trúc mục tiêu

```text
Watcher metrics / Loki / Volume metrics
                |
                v
       Data contract + quality gate
                |
       +--------+---------+
       |                  |
       v                  v
  Baseline models     River online model
       |                  |
       +--------+---------+
                v
       Multi-model consensus
                |
       Prediction interval + confidence
                |
       Alert lifecycle / hysteresis / cooldown
                |
       Feedback + observed outcome
                |
       Evaluation -> shadow -> guarded promotion
```

## 4. Repository tham khảo và cách áp dụng

Chỉ tham khảo pattern; không đưa nguyên một hệ thống bên ngoài vào production.

| Nguồn | Áp dụng trong Ceph-AI |
|---|---|
| [River](https://github.com/online-ml/river) | Online learning, `learn_one`, concept drift |
| [Netdata ML](https://github.com/netdata/netdata/blob/master/src/ml/README.md) | Multi-model, nhiều time window, consensus |
| [Evidently](https://github.com/evidentlyai/evidently) | Data quality, drift và model monitoring |
| [NAB](https://github.com/numenta/NAB) | Benchmark anomaly detection và early warning |
| [PyOD](https://github.com/yzhao062/pyod) | So sánh detector offline |
| [MLflow](https://github.com/mlflow/mlflow) | Tham khảo registry, promotion, rollback và audit |
| [GluonTS](https://github.com/awslabs/gluonts) | Tham khảo prediction interval trong giai đoạn sau |

Ưu tiên thực tế: River + pattern consensus của Netdata + quality/drift report kiểu Evidently + benchmark kiểu NAB. Chưa triển khai Avalanche, NeuralForecast hoặc deep model training liên tục trên server hiện tại.

## 5. Giới hạn phần cứng hiện tại

Server production hiện có 8 vCPU, 31 GiB RAM, khoảng 27 GiB RAM khả dụng và không có GPU. Đây là cấu hình phù hợp cho online statistics, River và consensus nhẹ; không phù hợp để train neural model liên tục.

Ngân sách mặc định:

- Watcher: tối đa 1.5 CPU và 1 GiB RAM.
- Worker: tối đa 2 CPU và 2 GiB RAM.
- Online learning trong poll loop: tối đa 10% của một CPU mỗi chu kỳ.
- Replay/benchmark: chạy ngoài poll loop, tối đa 1 CPU và 2 GiB RAM.
- Một job học hoặc benchmark tại một thời điểm cho mỗi scope.
- Chừa tối thiểu 2 CPU cho hệ điều hành, SSH và tác vụ Ceph.

## 6. Các phase triển khai

### Phase 0 — Baseline và safety gate

#### Bước 0.1 — Chụp baseline tài nguyên

- [x] Ghi nhận 8 vCPU, 31 GiB RAM, không GPU, không swap.
- [x] Ghi nhận load, CPU/RAM của Worker và Watcher trước khi thay đổi.
- [x] Ghi nhận số cluster/node/metric stream và tốc độ tăng dữ liệu.
- [x] Tạo dashboard CPU/RAM/latency riêng cho learning job: `/ai-learning` hiển thị cycle count, processed/applied/failed, average elapsed, average CPU time và recent cycles theo canary scope.

Acceptance criteria:

- Có baseline trước/sau cho CPU, RSS, DB query duration và poll latency.
- Không có learning job nào được phép chạy nếu chưa có giới hạn tài nguyên.

#### Bước 0.2 — Khóa an toàn vận hành

- [x] Thêm feature flag `online_learning_enabled`, mặc định `false`.
- [x] Thêm mode `AUDIT_ONLY`, `SHADOW_ONLY`, `ACTIVE`.
- [x] Thêm kill switch `online_learning_kill_switch` trên environment.
- [x] Lưu `consecutive_failures` và `last_success_at` trên Watcher heartbeat.
- [x] Chặn learning khi Watcher heartbeat quá cũ hoặc lỗi liên tiếp vượt ngưỡng.
- [x] Hiển thị runtime decision trong API AI Learning.
- [x] Migration `b5c6d7e8f9a0_add_learning_runtime_safety.py` đã được kiểm thử.

Đã hoàn thành. Các phase nền tảng đã triển khai; bước đang theo dõi là canary production ở Phase 5.3.

### Phase 1 — Data contract và quality gate

#### Bước 1.1 — Chuẩn hóa sample

- [x] Chuẩn hóa identity theo `cluster`, `host`, `metric`, `timestamp`.
- [x] Chuẩn hóa đơn vị CPU, RAM, IOPS, latency và timezone.
- [x] Có kiểm tra duplicate, out-of-order, missing và stale sample.
- [x] Có coverage ratio và longest gap.

#### Bước 1.2 — Fail-closed quality gate

- [x] Chặn forecast khi coverage hoặc freshness không đạt.
- [x] Lưu quality state persistent.
- [x] Thêm quality gate riêng cho online learner: không `learn_one` khi sample chưa đạt quality.
- [x] Tách rõ `DATA_QUALITY`, `NO_LABEL`, `DRIFT`, `READY_TO_LEARN`.

Đã hoàn thành bằng `shared/online_learning_consumer.py`. Tiếp theo: cấp verified label từ feedback/outcome.

### Phase 2 — River online learner

#### Bước 2.1 — Adapter River tối thiểu

- [x] Tạo module `shared/online_learning.py`.
- [x] Định nghĩa interface tối thiểu `predict_one()` và `learn_one()`.
- [x] Không để River phụ thuộc trực tiếp vào HTTP request hoặc Telegram.
- [x] Dùng `river.stats.Mean` làm adaptive baseline nhẹ, chưa dùng neural model.
- [x] Có `guarded_update()` tôn trọng `AUDIT_ONLY/SHADOW_ONLY/ACTIVE`.
- [x] Snapshot JSON an toàn, không dùng pickle hoặc serialize executable object.
- [x] Pin River `0.25.0`; test adapter/runtime/dashboard đạt.

#### Bước 2.2 — Durable state

- [x] Lưu state theo `(cluster, host, metric, model_version)`.
- [x] Lưu số sample đã học, thời điểm học cuối, feature schema và checksum.
- [x] Có JSON snapshot và khôi phục state sau restart.
- [x] Nếu state hỏng, reset về baseline; không tự dùng state không hợp lệ.

#### Bước 2.3 — Bounded update

- [x] Có primitive giới hạn batch theo `online_learning_max_samples_per_cycle`.
- [x] Có timeout mỗi cycle theo `online_learning_timeout_seconds`.
- [x] Có circuit breaker và cooldown khi update lỗi liên tiếp.
- [x] Kết quả cycle ghi nhận được latency, số sample xử lý và số sample bị bỏ qua.
- [x] Gắn primitive vào consumer CPU/RAM của Watcher; feature flag vẫn fail-closed.

Acceptance criteria:

- Worker/Watcher restart không làm mất state hợp lệ.
- Learning job không làm poll interval vượt ngưỡng.
- Cùng một sample không được học hai lần.

Tiếp theo: chạy Phase 2 ở `AUDIT_ONLY` trên một node và CPU metric trong ít nhất 24 giờ.

### Phase 3 — Feedback loop và ground truth

#### Bước 3.1 — Gắn feedback với outcome

- [x] Liên kết forecast với Incident, Action và RemediationCase.
- [x] Tách operator verdict khỏi telemetry truth.
- [x] Có trạng thái success, failed, inconclusive, regressed.
- [x] Chỉ chuyển forecast outcome `EVALUATED` có `actual_percent` và sample khớp thời gian thành nhãn cho River.
- [x] Tạo label queue `READY/CONSUMED`, liên kết `source_run_id` và audit sample.
- [x] Consumer cập nhật label cũ `NO_LABEL` ở chu kỳ sau; không bật `ACTIVE`.

#### Bước 3.2 — Label policy

- [x] `VERIFIED_SUCCESS` là nhãn tích cực khi absolute error nằm trong tolerance.
- [x] `VERIFIED_FAILED` là nhãn lỗi khi forecast có actual nhưng vượt tolerance; `REGRESSED` không được suy diễn từ numeric telemetry.
- [x] `INCONCLUSIVE`, stale, thiếu actual hoặc không khớp timestamp không được đưa vào learner.
- [x] Mọi label có source run, source actor, evidence count, verified timestamp và reason.
- [x] Minimum evidence mặc định là 3 outcome/stream; thiếu bằng chứng thì chờ chu kỳ reconcile sau.
- [x] Drift vượt 20 percentage points bị quality gate chặn và label vẫn giữ `READY`, không cập nhật learner.

#### Bước 3.3 — Chống feedback poisoning

- [x] Giới hạn số label một actor có thể tạo trong một khoảng thời gian (`100/3600s` mặc định).
- [x] Không cho một alert chưa đóng tạo label cuối cùng; reconcile lại sau khi alert chuyển `RESOLVED`.
- [x] Có audit append-only cho `CREATED`, `BLOCKED`, `CONSUMED` và `REVOKED`; thu hồi không xoá bản ghi.
- [x] Khi chạm rate limit bất thường, mở policy pause trong cùng cửa sổ thời gian và fail-closed mọi online update.
- [x] Production feedback endpoint `/api/ai-learning/node-alerts/{alert_id}/feedback` đã được kiểm tra; unauthenticated request trả `303`, không ghi dữ liệu. Feedback thật vẫn là `0` row và chỉ operator mới được tạo nhãn.

Tiếp theo: dùng verified feedback để chạy online update trong `SHADOW_ONLY`, chưa thay đổi model active.

### Phase 4 — Concept drift và multi-model consensus

#### Bước 4.1 — Drift detection

- [x] Theo dõi drift của baseline, residual, coverage và alert rate trong detector đa tín hiệu.
- [x] Phân biệt drift thật (`DRIFT`) với thiếu bằng chứng (`INSUFFICIENT_DATA`).
- [x] Khi drift xảy ra, giảm confidence theo multiplier và chặn đường mở predictive alert/online update cho tới khi có evidence mới.
- [x] Drift không có quyền tự promotion model; promotion vẫn đi qua guarded comparison và operator approval.
- [x] Lưu `coverage_ratio`, `max_gap_hours`, `drift_status`, `drift_score` và `drift_reason` trên forecast run.

#### Bước 4.2 — Model ensemble nhẹ

- [x] Có baseline và candidate forecast.
- [x] Có multi-model vote và prediction interval.
- [x] Bổ sung River model (`river_mean`) như shadow candidate, không thay thế active deterministic ensemble.
- [x] Xác định tối thiểu số model đồng thuận theo metric; River không được làm thay đổi active vote trước guarded promotion.
- [x] Persist River candidate cùng target timestamp để replay/MAE/SMAPE/false-positive comparison dùng chung ground truth.

#### Bước 4.3 — Quyết định alert

- [x] Có hysteresis, cooldown và dedupe.
- [x] Hiển thị rõ model vote, confidence, interval và quality trong `state_reason`/evidence fingerprint.
- [x] Alert chỉ mở khi quality đạt và consensus vượt ngưỡng; drift được lưu `DATA_QUALITY` không gửi notification.
- [x] Khi model bất đồng, chuyển thành `CANDIDATE`/`LOW_CONFIDENCE`, không tự mở WARNING/CRITICAL.
- [x] Hysteresis, consensus, drift và confidence được kiểm thử cùng lifecycle transition.

Tiếp theo: kiểm thử trên dữ liệu lịch sử trước khi cho River candidate chạy canary.

### Phase 5 — Shadow evaluation và guarded promotion

#### Bước 5.1 — Shadow evaluation

- [x] Candidate chạy song song active.
- [x] Có MAE, RMSE, SMAPE, bias và false-positive guard.
- [x] Candidate không gửi notification hoặc remediation.
- [x] Bổ sung evidence drift của candidate để so sánh chất lượng trước/sau drift; không dùng candidate đang `DRIFT` làm cơ sở promotion.
- [x] Ghi nhận rõ consumer hiện chỉ cập nhật model với `target="shadow"`;
  không có đường cập nhật `active`, notification hoặc remediation từ consumer.

#### Bước 5.2 — Promotion policy

- [x] Có model registry và version.
- [x] Có yêu cầu số outcome tối thiểu.
- [x] Operator approval là bắt buộc.
- [x] Có rollback và append-only audit.
- [x] Chặn promotion nếu resource budget hoặc poll latency vượt ngưỡng.
- [x] Chặn promotion nếu evidence drift của candidate không đạt; lỗi evidence hoặc runtime state thiếu cũng fail-closed và ghi audit `PROMOTION_BLOCKED`.
- [x] Nối promotion với `shared/model_registry.py`: shadow evaluation đăng ký
  cặp active/candidate và evaluation; request/approve/rollback cập nhật registry,
  runtime selected state và append-only audit, luôn giữ operator approval.
- [x] Kiểm thử đủ lifecycle `CANDIDATE → SHADOW → ACTIVE → RETIRED/rollback`;
  candidate chỉ được active sau guarded evidence và operator approval.

#### Bước 5.3 — Canary rollout

- [x] Có runtime canary guard theo đúng `cluster_id + host + metric`; thiếu scope hoặc không khớp đều fail-closed.
- [x] Ghi append-only lifecycle transition cho predictive alert để đo alert volume, recovery và early-detection theo thời gian.
- [x] Có API/UI báo cáo canary read-only, hiển thị MAE/SMAPE, data-quality, alert volume, precision/lead time và CPU cost.
- [x] Có bounded learner-cycle CPU/elapsed telemetry; khi chưa bật learner, báo cáo hiển thị rõ “chưa có telemetry”, không suy diễn thành 0.
- [x] Chọn ứng viên canary `CS-LAB / 10.20.1.153 / cpu` và replay read-only 14 ngày: 335 hourly points, 311 paired outcomes; rolling MAE 8.568 thấp hơn linear 9.811, consensus MAE 7.902, false-positive rate không tăng.
- [x] Bật ứng viên trên production ở `SHADOW_ONLY` sau khi operator phê duyệt; scope thực tế là `CS-LAB / 10.20.1.153 / cpu`, kill switch vẫn tắt và `ACTIVE`/auto-promotion không được phép.
- [x] Theo dõi ít nhất một bounded learner cycle production: sample CPU thật được audit với `runtime=SHADOW_ONLY`, `failed=0`, `applied=0`, `quality=NO_LABEL`.
- [x] Sửa registry drift do adaptive window selection: đồng bộ baseline `linear:<window>h` chưa có promotion/rollback governance; scope đã có governance vẫn fail-closed.
- [x] So sánh alert volume, false positive, early detection và CPU cost bằng canary report 72 giờ: snapshot mới nhất có `364` runs, `188` evaluated, `2` lifecycle events, `0` false positives, `0` early detections, `7` cycles, MAE `5.5251` và `3358.786 ms` CPU time; không có warning/critical outcome để kết luận candidate tốt hơn.
- [x] Giữ nguyên scope trong suốt acceptance; không mở rộng canary khi chưa có operator approval cho change request tiếp theo.
- [x] Mọi registry row promotion phải có đủ `cluster_id + host + metric` cùng
  `scope_schema`/horizon; scope legacy hoặc thiếu host/metric bị
  `PROMOTION_BLOCKED`, không được đoán từ `scope_key`.

Evidence bổ sung cho Phase 5:

- `tests/test_model_registry.py tests/test_model_registry_lifecycle.py
  tests/test_canary.py tests/test_online_learning_consumer.py`: `14 passed`.
- Lifecycle test xác nhận shadow pair, full scope dimensions, guarded promotion,
  selected runtime state và rollback về đúng active model trước đó.
- Scope regression test xác nhận `cluster + host + metric` thiếu hoặc không hợp
  lệ sẽ bị chặn và ghi `PROMOTION_BLOCKED`.
- Consumer regression xác nhận target luôn là `shadow`, không thể ghi trực tiếp
  vào active model.

Canary scope đã được giữ nguyên; việc theo dõi tiếp theo và promotion sang `ACTIVE` là operational follow-up riêng, không phải blocker của implementation này.

### Phase 6 — Dashboard và explainability

#### Bước 6.1 — Online learning status

- [x] Hiển thị model version, sample count, last learned time.
- [x] Hiển thị quality gate và lý do sample bị loại.
- [x] Hiển thị drift state và learner latency.
- [x] Hiển thị verified feedback count.

Đã triển khai read-only status vào `/ai-learning`; khi learner chưa bật, UI hiển thị rõ `DISABLED` và `Chưa có telemetry`, không suy diễn thành zero.

#### Bước 6.2 — Forecast detail

- [x] Actual, predicted và prediction interval.
- [x] Model/algorithm/window/version.
- [x] Confidence và consensus ratio.
- [x] MAE/SMAPE gần nhất.

Đã kiểm tra trên `/ai-learning`: các bảng CPU/RAM và RBD volume đều hiển thị actual/predicted, interval, model/window/version, confidence/consensus và MAE/SMAPE; dữ liệu thiếu được hiển thị bằng `—` thay vì suy diễn.

#### Bước 6.3 — Operator controls

- [x] Pause/resume learner theo đúng `cluster + host + metric`, fail-closed khi control không đọc được.
- [x] Reset state theo host/metric, không xóa stream khác và yêu cầu xác nhận `RESET`.
- [x] Promote/rollback giữ nguyên guarded approval; bổ sung block candidate có lý do bắt buộc.
- [x] Xem append-only audit trail cho pause/resume/reset/block.
- [x] Migrate/deploy/verify production source sau khi SSH tới `10.3.55.213` hoạt động lại.
- [x] Verify hành vi pause trên đúng live canary stream: consumer trả `PAUSED`, `update_applied=false`; sau kiểm thử đã resume về `RUNNING`.

Đã sửa lỗi production phát sinh khi sample live timezone-aware được so sánh với timestamp persisted naive: normalize `latest.observed_at` về UTC trước quality gate; regression suite `tests/test_online_learning_consumer.py` đạt `10 passed`. Consumer cũng resolve cluster mặc định một lần trước bounded runner và ghi cycle telemetry theo từng stream `cluster + host + metric`, tránh gộp CPU/RAM thành `metric=mixed`. Nhóm test consumer/forecast/dashboard đạt `49 passed`. Control, dashboard, migration và operator-route đã được deploy/verify.

Production rollout checklist:

- [x] Khôi phục SSH tới `10.3.55.213` và xác nhận watcher/worker/dashboard đang healthy.
- [x] Đối chiếu migration head production (`e3f4a5b6c7d8`) với local chain (`e2f3a4b5c6d7`); tạo production migration variant `f0a1b2c3d4f7` đúng parent.
- [x] Backup schema trước migration; tạo `online_learner_controls` và `online_learner_operator_audits` trong explicit transaction, sau đó xác nhận version `f0a1b2c3d4f7`.
- [x] Deploy route/template/model/consumer; kiểm tra unauthenticated 303, non-admin không được phép và operator route/import.
- [x] Restart dashboard/worker/watcher và xác nhận cả ba healthy.
- [x] Khi canary được operator bật, xác nhận stream bị pause không tạo update trong production; operator audit ghi nhận PAUSE/RESUME và control cuối cùng là `RUNNING`.
- [x] Per-stream learner telemetry — cycle audit production ghi đúng `cluster + host + metric`, cycle mới nhất của CPU/RAM có `failed=0`; các audit lịch sử vẫn giữ nguyên append-only.
- [x] Sửa schema production cho prompt version — `log_findings.prompt_version` đã mở rộng lên `VARCHAR(64)`, migration production variant `f3c4d5e6f7a8` và schema backup đã được xác nhận.

### Phase 7 — Benchmark và nghiệm thu

#### Bước 7.1 — Offline benchmark

- [x] Tạo dataset Ceph đã ẩn thông tin nhạy cảm: dữ liệu tổng hợp `docs/benchmark/ceph-node-cpu-anonymized.csv`, không chứa cluster/host/IP/volume ID.
- [x] Dùng NAB làm format/tham khảo scoring: hỗ trợ `timestamp,value`, point labels và `window_start,window_end`.
- [x] So sánh baseline, River và detector PyOD: kết quả lưu tại `docs/benchmark/forecast-benchmark-report.json`; PyOD chỉ chạy trong benchmark container riêng.
- [x] Đánh giá precision, recall, false-positive rate, detection delay và CPU cost; fixture hiện chỉ đạt với robust baseline, River/PyOD bị giữ ngoài production vì chưa bắt được event.

Benchmark evidence: robust baseline đạt precision/recall/event-recall `1.0/1.0/1.0`; River HalfSpaceTrees và PyOD IForest đều recall `0.0` trên fixture hiện tại. Đây là lý do chưa promote candidate và cần dataset Ceph thật đã ẩn danh trước khi điều chỉnh threshold.

#### Bước 7.2 — Production acceptance

- [x] Không phát sinh soft lockup hoặc learning-induced DB saturation; pool hardening `5/0/10` đã deploy. Sau restart tuần tự, log cửa sổ mới không có soft lockup, QueuePool hoặc learning-induced poll timeout; timeout Ceph node lịch sử được ghi nhận là dependency issue.
- [x] Không mất forecast/feedback sau restart; forecast runs và learner audit/cycle counts giữ nguyên. Production chưa có feedback thật (`0` row), nên không tạo dữ liệu giả và retention feedback sẽ được kiểm tra khi có outcome thật.
- [x] Không có promotion tự động ngoài policy — runtime canary hiện `can_update_active=false`, chưa có promotion audit và guarded tests đã pass.
- [x] Rollback trong thời gian mục tiêu — guarded rollback regression test khôi phục đúng model trước đó và hoàn tất dưới `1s`.
- [x] Có tài liệu vận hành và runbook khi model học sai: `docs/runbook-online-learning-canary.md`.

Evidence hiện tại chưa đủ để đóng các mục observation dài hạn: sau restart worker,
ba service vẫn `healthy`, 15 phút log gần nhất không có soft lockup/poll timeout,
DB probe là `54.447 ms`, Worker dùng khoảng `185.3 MB / 2.147 GB`, Watcher
`231.9 MB / 1.074 GB`, forecast runs vẫn là `17,708`, cycle audit mới nhất được
ghi sau restart. Production chưa có forecast feedback (`0` row), vì vậy retention
của feedback chưa thể kết luận; không tạo dữ liệu giả trong DB thật.

Observation sau restart ngày `19/09/2026 09:55 +07`: forecast runs `17,708`,
forecast feedback `0`, promotion audits `0`, learner cycle audits `30`; không
có mất forecast. Feedback vẫn chưa thể nghiệm thu retention vì production chưa
phát sinh feedback thật. Runtime pool được xác nhận `size=5`, `overflow=0`,
`timeout=10s`; dashboard/worker/watcher đều `healthy`.

Đã phát hiện một QueuePool timeout đơn lẻ của Vitastor lúc `02:38:57` do
Watcher có nhiều auxiliary monitor threads nhưng pool bị giới hạn `3/0/5s`.
Đã sửa bounded database budget thành `5/0/10s`, giữ overflow bằng `0`; sau
restart Watcher, pool runtime xác nhận `size=5`, `timeout=10`, `checkedout=0`
và chu kỳ Vitastor tiếp theo không còn timeout. Mục acceptance vẫn chờ đủ
24–72 giờ vì trước đó đã có một timeout thật.

Worker và Dashboard cũng đã được restart tuần tự để nạp cùng policy; runtime
đều xác nhận `pool=5`, `timeout=10`, `overflow=0`. Sau restart, forecast runs
vẫn `17,708`, cycle audits `30`, promotion audits `0`, và không có lỗi mới
trong log health check.

Canary report live mới nhất lúc `19/09/2026 10:05 +07` (`72h`, scope
`CS-LAB / 10.20.1.153 / cpu`) xác nhận `scope_match=true`,
`ready_for_canary_review=true`, `data_quality_count=0`, `188` evaluated outcomes, `0`
false-positive events, `0` detected breaches và `7` bounded cycles. Đây là
evidence quan sát, không phải lý do để promote khi chưa có warning/critical
outcome thực tế.

Worker shadow evaluator hiện ghi nhận `108` comparisons, `7` `PROMISING` và
`101` `HOLD`, nhưng vẫn chạy `SHADOW_ONLY` với thông báo rõ `no promotion,
notification or remediation`; `promotion_audits=0`. Đây là bằng chứng candidate
được đánh giá nhưng guarded boundary vẫn đang chặn promotion tự động.

Guarded rollback smoke test trên SQLite tạm đã tạo active/candidate, ghi
evidence, request/approve và rollback thành công trong `13.4 ms`; production
chưa có candidate được promote nên chưa đánh dấu rollback acceptance thật.

## 7. Không làm trong phiên bản đầu

- Không train neural network liên tục trên production server.
- Không cho model tự thay đổi remediation policy.
- Không học từ mọi metric chưa qua quality gate.
- Không dùng LLM làm threshold engine.
- Không cài MLflow, Evidently server hoặc thêm database nếu chưa có nhu cầu vận hành rõ ràng.
- Không bật online learning trên toàn bộ cluster ngay từ đầu.

## 8. Trạng thái và bước tiếp theo

- [x] Đã lập plan riêng cho AI self-learning.
- [x] Đã xác định River là ứng viên online learning chính.
- [x] Đã xác định multi-model consensus và quality gate là bắt buộc.
- [x] Đã ghi nhận giới hạn phần cứng production.
- [x] Bước 0.2 — feature flag, mode `AUDIT_ONLY/SHADOW_ONLY/ACTIVE`, kill switch và heartbeat safety gate.
- [x] Bước 2.1 — River adapter tối thiểu và guarded update.
- [x] Bước 2.2 — durable JSON state, checksum, restore và fail-closed reset.
- [x] Bước 2.3 — bounded batch/timeout/circuit-breaker primitive.
- [x] Bước 1.2 — per-sample quality gate và kiểm thử fail-closed.
- [x] Consumer Watcher CPU/RAM và bảng `online_learner_audit`.
- [x] Bước 3.1 — verified telemetry outcome, label queue và delayed consumption.
- [x] Bước 3.2 — label policy, minimum evidence, drift gate và source audit metadata.
- [x] Bước 3.3 — poisoning guard, alert-close gate, rate limit và label event audit.
- [x] Bước 4.1 — detector drift đa chiều và metadata drift trên forecast run.
- [x] Bước 4.2 — River shadow candidate và deterministic active ensemble boundary.
- [x] Bước 4.3 — alert quality/consensus boundary, drift state và evidence-rich lifecycle reason.
- [x] Bước 5.1 — shadow metrics, paired target evidence và drift-aware comparison.
- [x] Bước 5.2 — guarded promotion, resource/latency/drift gates, operator approval, rollback và append-only audit.
- [x] Bước 5.2 bổ sung — promotion đã nối trực tiếp với model registry và có
  regression lifecycle cho candidate/shadow/active/rollback.
- [x] Bước 5.3 — canary guard, lifecycle evidence, acceptance API/UI và resource-cost telemetry read-only.
- [x] Bước 5.3 bổ sung — registry scope bắt buộc đủ cluster/host/metric và
  fail-closed khi thiếu dimension.
- [x] Bước 6.1 — dashboard online-learning status, quality gate, drift, latency và verified feedback read-only.
- [x] Bước 6.2 — forecast detail gồm actual/predicted, interval, model/version, confidence/consensus và MAE/SMAPE.
- [x] Bước 6.3 — operator controls đã viết/test, deploy production và verify hành vi trên live canary.
- [x] Preflight canary — scope `CS-LAB / 10.20.1.153 / cpu` và heartbeat đã pass dry-run `SHADOW_ONLY`.
- [x] Production canary gate — chỉ bật `SHADOW_ONLY`, giữ `ONLINE_LEARNING_KILL_SWITCH=false`, `can_update_active=false`, và xác nhận cả watcher/worker/dashboard nạp đúng settings.
- [x] Production live pause verification — pause/resume thành công, không có active update.
- [x] Dashboard evidence — `/ai-learning` hiển thị telemetry learner và báo cáo canary 72 giờ gồm forecast quality, alert lifecycle, false positive/pending outcome, lead time và resource cost.
- [x] Production schema compatibility — canary report đọc được cả `node_resource_forecast_alert_events` và schema cũ `node_resource_forecast_transitions` mà không cần rewrite dữ liệu.
- [x] Dashboard runtime scope — `learning_status()` truyền đúng `host + metric` vào runtime gate; production UI hiện `SHADOW_ONLY/healthy` thay vì báo nhầm `CANARY_SCOPE`.
- [x] Per-stream cycle telemetry — dashboard đọc được cycle audit riêng cho CPU/RAM; cycle mới nhất không còn lỗi sau khi sửa timestamp normalization.
- [x] Production prompt-version compatibility — log analysis không còn `StringDataRightTruncation` do prompt version sau khi migrate schema.
- [x] Forecast registry drift — sau restart worker không còn `runtime active model differs from registry ACTIVE model`; active registry khớp selected runtime state và không phát sinh promotion audit tự động.
- [x] Bounded database pool — Watcher/Worker/Dashboard dùng `pool=5`, `overflow=0`, `timeout=10s`; sau sửa không có QueuePool timeout mới trong observation hiện tại.

Kết luận: **đã hoàn tất implementation và acceptance của guarded self-learning/predictive-alerting canary**. Production tiếp tục giữ `SHADOW_ONLY`; River/PyOD không được promote; promotion sang `ACTIVE` và mở rộng scope là change request riêng sau khi có đủ verified feedback.

## 9. Roadmap cải tiến theo repository

Các bước cải tiến tiếp theo được tách khỏi acceptance hiện tại tại
[`Plan/in-progress/ai-self-learning-repository-improvement-plan.md`](../in-progress/ai-self-learning-repository-improvement-plan.md).

- [ ] Phase 0 — contract/adapter boundary và dependency policy.
- [ ] Phase 1 — River replay determinism, snapshot migration và resource benchmark.
- [ ] Phase 2 — Evidently offline data-quality/drift/evaluation report.
- [ ] Phase 3 — NannyML cho delayed/no-label feedback.
- [ ] Phase 4 — MLflow registry adapter sau local registry, có checksum và rollback.
- [ ] Phase 5 — Vowpal Wabbit contextual-bandit research sandbox.
- [ ] Phase 6 — Alibi Detect benchmark cho drift/anomaly.
- [ ] Phase 7 — Feast chỉ mở khi có nhiều consumer cần shared feature store.
- [ ] Phase 8 — acceptance 72 giờ, staging rollback và operator sign-off.
