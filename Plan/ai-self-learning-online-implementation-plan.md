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
- [ ] Tạo dashboard CPU/RAM/latency riêng cho learning job.

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

Tiếp theo: Bước 2.1 — tạo River adapter tối thiểu; mặc định vẫn giữ `online_learning_enabled=false`.

### Phase 1 — Data contract và quality gate

#### Bước 1.1 — Chuẩn hóa sample

- [x] Chuẩn hóa identity theo `cluster`, `host`, `metric`, `timestamp`.
- [x] Chuẩn hóa đơn vị CPU, RAM, IOPS, latency và timezone.
- [x] Có kiểm tra duplicate, out-of-order, missing và stale sample.
- [x] Có coverage ratio và longest gap.

#### Bước 1.2 — Fail-closed quality gate

- [x] Chặn forecast khi coverage hoặc freshness không đạt.
- [x] Lưu quality state persistent.
- [ ] Thêm quality gate riêng cho online learner: không `learn_one` khi sample chưa đạt quality.
- [ ] Tách rõ `DATA_QUALITY`, `NO_LABEL`, `DRIFT`, `READY_TO_LEARN`.

Tiếp theo: thêm `online learner input gate` và test các trường hợp dữ liệu thiếu, trễ, lặp và nhảy thời gian.

### Phase 2 — River online learner

#### Bước 2.1 — Adapter River tối thiểu

- [ ] Tạo module `shared/online_learning.py`.
- [ ] Định nghĩa interface `predict(sample)` và `learn(sample, verified_label)`.
- [ ] Không để River phụ thuộc trực tiếp vào HTTP request hoặc Telegram.
- [ ] Dùng model nhẹ trước: online mean/quantile, linear model hoặc anomaly score.

#### Bước 2.2 — Durable state

- [ ] Lưu state theo `(cluster, host, metric, model_version)`.
- [ ] Lưu số sample đã học, thời điểm học cuối, feature schema và checksum.
- [ ] Có snapshot định kỳ và khôi phục state sau restart.
- [ ] Nếu state hỏng, reset về baseline; không tự dùng state không hợp lệ.

#### Bước 2.3 — Bounded update

- [ ] Mỗi poll chỉ xử lý batch giới hạn.
- [ ] Không query Loki quá thời lượng cấu hình.
- [ ] Timeout và circuit breaker cho learning job.
- [ ] Ghi learning latency và số sample bị bỏ qua.

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
- [ ] Chỉ chuyển outcome đủ bằng chứng thành nhãn cho River.

#### Bước 3.2 — Label policy

- [ ] `VERIFIED_SUCCESS` là nhãn tích cực.
- [ ] `VERIFIED_FAILED` và `REGRESSED` là nhãn lỗi.
- [ ] `INCONCLUSIVE`, stale và partial telemetry không được đưa vào learner.
- [ ] Mọi label phải có source, actor, evidence và timestamp.

#### Bước 3.3 — Chống feedback poisoning

- [ ] Giới hạn số label một actor có thể tạo trong một khoảng thời gian.
- [ ] Không cho một alert chưa đóng tạo label cuối cùng.
- [ ] Có audit khi sửa hoặc thu hồi label.
- [ ] Khi tỷ lệ label bất thường, tạm dừng learner.

Tiếp theo: dùng verified feedback để chạy online update trong `SHADOW_ONLY`, chưa thay đổi model active.

### Phase 4 — Concept drift và multi-model consensus

#### Bước 4.1 — Drift detection

- [ ] Theo dõi drift của baseline, residual, coverage và alert rate.
- [ ] Phân biệt drift thật với thiếu dữ liệu.
- [ ] Khi drift xảy ra, giảm confidence và yêu cầu thêm sample.
- [ ] Không tự promotion chỉ vì drift.

#### Bước 4.2 — Model ensemble nhẹ

- [x] Có baseline và candidate forecast.
- [x] Có multi-model vote và prediction interval.
- [ ] Bổ sung River model như candidate, không thay thế ngay seasonal/rolling baseline.
- [ ] Xác định tối thiểu số model đồng thuận theo metric.

#### Bước 4.3 — Quyết định alert

- [x] Có hysteresis, cooldown và dedupe.
- [ ] Hiển thị rõ model vote, confidence, interval và quality trong lý do alert.
- [ ] Alert chỉ mở khi quality đạt và consensus vượt ngưỡng.
- [ ] Khi model bất đồng, chuyển thành `LOW_CONFIDENCE` hoặc `REVIEW_REQUIRED`.

Tiếp theo: kiểm thử trên dữ liệu lịch sử trước khi cho River candidate chạy canary.

### Phase 5 — Shadow evaluation và guarded promotion

#### Bước 5.1 — Shadow evaluation

- [x] Candidate chạy song song active.
- [x] Có MAE, RMSE, SMAPE, bias và false-positive guard.
- [x] Candidate không gửi notification hoặc remediation.
- [ ] Bổ sung so sánh chất lượng trước/sau drift.

#### Bước 5.2 — Promotion policy

- [x] Có model registry và version.
- [x] Có yêu cầu số outcome tối thiểu.
- [x] Operator approval là bắt buộc.
- [x] Có rollback và append-only audit.
- [ ] Chặn promotion nếu resource budget hoặc poll latency vượt ngưỡng.

#### Bước 5.3 — Canary rollout

- [ ] Chọn một node hoặc một metric stream làm canary.
- [ ] Theo dõi ít nhất một chu kỳ đánh giá đầy đủ.
- [ ] So sánh alert volume, false positive, early detection và CPU cost.
- [ ] Chỉ mở rộng scope khi operator xác nhận.

Tiếp theo: hoàn tất dashboard và chạy canary 24–72 giờ.

### Phase 6 — Dashboard và explainability

#### Bước 6.1 — Online learning status

- [ ] Hiển thị model version, sample count, last learned time.
- [ ] Hiển thị quality gate và lý do sample bị loại.
- [ ] Hiển thị drift state và learner latency.
- [ ] Hiển thị verified feedback count.

#### Bước 6.2 — Forecast detail

- [ ] Actual, predicted và prediction interval.
- [ ] Model/algorithm/window/version.
- [ ] Confidence và consensus ratio.
- [ ] MAE/SMAPE gần nhất.

#### Bước 6.3 — Operator controls

- [ ] Pause/resume learner.
- [ ] Reset state theo host/metric.
- [ ] Promote, rollback và block candidate.
- [ ] Xem audit trail.

### Phase 7 — Benchmark và nghiệm thu

#### Bước 7.1 — Offline benchmark

- [ ] Tạo dataset Ceph đã ẩn thông tin nhạy cảm.
- [ ] Dùng NAB làm format/tham khảo scoring.
- [ ] So sánh baseline, River và detector PyOD.
- [ ] Đánh giá precision, recall, false-positive rate, detection delay và CPU cost.

#### Bước 7.2 — Production acceptance

- [ ] Không tăng soft lockup, poll timeout hoặc DB saturation.
- [ ] Không mất forecast/feedback sau restart.
- [ ] Không có promotion tự động ngoài policy.
- [ ] Rollback trong thời gian mục tiêu.
- [ ] Có tài liệu vận hành và runbook khi model học sai.

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

Bước tiếp theo sẽ làm: **Bước 2.1 — tạo River adapter tối thiểu**.
