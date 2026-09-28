# Baseline Self-learning & Predictive Alerting — 2026-09-18

## Phạm vi và nguồn dữ liệu

- Server: 10.3.55.213
- Cluster: CS-LAB
- Database: PostgreSQL được cấu hình bởi ứng dụng; truy vấn read-only qua SQLAlchemy.
- Snapshot Loki: 2026-09-18 04:06:37 UTC.
- Không ghi, sửa hoặc xóa dữ liệu production.

## Tổng quan runtime

| Hạng mục | Giá trị |
|---|---:|
| Cluster | CS-LAB |
| Ceph execution mode | cephadm |
| Autopilot | Bật |
| Node resource forecast runs | 15,896 |
| Node runs evaluated / pending | 14,744 / 1,152 |
| Volume forecast runs | 2,001 |
| Volume runs evaluated / pending | 1,956 / 45 |
| Capacity samples | 7,793 |
| Capacity series | 39 |

## CPU/RAM forecast

- Có 6 host, 2 metric và 4 training windows: 24h, 72h, 168h, 720h.
- Có 48 model states; 12 model được chọn, tương ứng một model cho mỗi host/metric.
- CPU: 7,948 runs; 7,372 evaluated; MAE trung bình 17.120 percentage points; max absolute error 94.700.
- RAM: 7,948 runs; 7,372 evaluated; MAE trung bình 14.378 percentage points; max absolute error 69.700.
- Tất cả 5 forecast alert hiện tại đều RESOLVED; không có alert OPEN.
- Model được chọn: CPU dùng 72h/168h/720h tùy host; RAM hiện cả 6 host dùng 720h.
- Loki freshness tại snapshot: 6/6 host đều có 1,917–2,368 samples; sample mới nhất trễ 919–937 giây.
- Ngưỡng stale hiện tại là 1,800 giây. Tất cả stream dưới ngưỡng nhưng đang trễ khoảng 15 phút; cần quality gate phân biệt “OK nhưng trễ” với dữ liệu realtime.

## Volume forecast

- Có 2,001 runs; 1,956 evaluated và 45 pending.
- IOPS: percentage error trung bình 49.044%.
- Read latency: percentage error trung bình 13.432%.
- Write latency: percentage error trung bình 60.564%.
- Early forecast: SAFE 639, WARNING 117, LOW_CONFIDENCE 539, NO_THRESHOLD 415.
- Kết luận: write latency và IOPS chưa đủ tin cậy để dùng làm nguồn auto-alert độc lập.

## Log learning

- 107 learning samples; 0 eligible; 107 UNVERIFIED.
- 101 sample ở INSUFFICIENT_EVIDENCE; 6 sample ở CANDIDATE.
- 17 fault stats; verified = 0, success = 0, failure = 0, average trust = 0.
- Tất cả stats bị chặn vì log learning đang audit-only và shadow commissioning chưa bật.

## Remediation feedback

- 2,212 remediation cases.
- Operator-labeled: 0; verified success: 1; verified failed: 0; inconclusive: 1; shadowed: 57.
- Phân bố chính: 1,882 PROPOSED/RISKY, 323 REJECTED/RISKY, 4 EXECUTION_FAILED/RISKY.
- Kết luận: feedback loop tồn tại nhưng chưa đủ label để self-learning có ý nghĩa thống kê.

## Log ingest quality

| Status | Số lần | Failed hosts | Lines |
|---|---:|---:|---:|
| OK | 503 | 0 | 200,222 |
| PARTIAL | 1,115 | 3,369 | 7,202,725 |
| FAILED | 137 | 482 | 0 |

Tỷ lệ PARTIAL/FAILED cao là lý do phải hoàn thành data-quality gate trước khi thêm model mới.

## Quyết định kỹ thuật sau baseline

1. Ưu tiên quality metadata và stale/gap classification.
2. Không promote volume model hiện tại thành nguồn auto-remediation.
3. Không bật log-learning promotion khi chưa có verified outcome.
4. Giữ linear/seasonal model hiện tại làm baseline.
5. Bước implementation tiếp theo là Bước 0.2: định nghĩa data contract, sau đó Phase 1 tạo quality helper.

