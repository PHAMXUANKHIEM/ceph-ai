# Runbook học live CPU/RAM

## Chế độ hoạt động

Watcher quét node theo `NODE_HEALTH_SCAN_INTERVAL_SECONDS` (mặc định 15 phút).
Khi bật forecast, nó đọc mẫu CPU/RAM từ Loki, tạo forecast cho CPU và RAM, ghi
forecast candidate vào database, rồi đối chiếu với mẫu thực tế sau thời gian
đánh giá để cập nhật MAE và chọn cửa sổ tốt nhất.

Có hai nguồn mẫu:

- Alloy đã chạy trên các node: đặt `NODE_RESOURCE_LIVE_INGEST_ENABLED=false`.
- Chưa có Alloy: đặt `NODE_RESOURCE_LIVE_INGEST_ENABLED=true`; Watcher lấy
  `/proc` qua SSH read-only và đẩy JSON vào Loki trước khi phân tích.

## Cấu hình tối thiểu

```dotenv
DATABASE_URL=sqlite:///./ceph_aiops.db
LOG_INTEL_LOKI_URL=http://loki:3100
LOG_INTEL_LOKI_TENANT=
NODE_RESOURCE_FORECAST_ENABLED=true
NODE_RESOURCE_LIVE_INGEST_ENABLED=true
NODE_RESOURCE_FORECAST_HISTORY_DAYS=30
NODE_RESOURCE_FORECAST_HORIZON_HOURS=168
NODE_RESOURCE_FORECAST_MIN_SAMPLES=24
NODE_RESOURCE_LEARNING_EVALUATION_HOURS=24
NODE_RESOURCE_LEARNING_MIN_OUTCOMES=3
NODE_RESOURCE_LEARNING_CANDIDATE_HOURS=24,72,168,720
NODE_RESOURCE_FORECAST_ALERT_COOLDOWN_SECONDS=86400
```

Phải cấu hình thêm SSH key và danh sách `CEPH_MON_NODES`, `CEPH_OSD_NODES`
(cùng các node khác nếu cần). Nếu dùng Alloy, Alloy phải gửi stream có các
label `job="ceph-ai-node-metrics"`, `cluster`, `host` và
`metric_type="node_resource"`; mỗi dòng JSON cần có `cpu_percent` và
`mem_percent`.

## Bật sau khi cập nhật code

```bash
./.venv/bin/alembic upgrade head
./scripts/deploy/restart_services.sh
```

Kiểm tra log:

```bash
rg -n "node forecast|CPU/RAM|live CPU/RAM" /var/log/ceph-ai-watcher.log
```

Không có đủ 24 mẫu hoặc Loki trả dữ liệu stale thì forecast bị bỏ qua. Khi có
ít nhất 3 outcome cho các candidate, Watcher tự chọn cửa sổ có MAE thấp nhất;
quyết định này không thay đổi policy hay tự thực thi remediation. Forecast
đáng tin cậy chạm 90% sẽ gửi cảnh báo sớm qua kênh Telegram Phần cứng tối đa
một lần mỗi cooldown; khi forecast không còn nguy cơ, trạng thái cảnh báo được
đóng trong database.
