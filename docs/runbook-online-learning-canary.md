# Runbook: Online-learning canary và predictive alerting

Runbook này áp dụng cho canary `CS-LAB / 10.20.1.153 / cpu` trên
`10.3.55.213`. Canary chỉ chạy `SHADOW_ONLY`; không được tự thay active model,
policy remediation hoặc notification.

## Trạng thái an toàn mặc định

- `ONLINE_LEARNING_ENABLED=true` chỉ cho phép pipeline quan sát trong scope.
- `ONLINE_LEARNING_MODE=SHADOW_ONLY` chỉ cho phép cập nhật shadow state sau khi
  sample vượt quality gate và có verified label.
- `ONLINE_LEARNING_KILL_SWITCH=false` là trạng thái bình thường; đặt `true`
  ngay khi có dấu hiệu bất thường.
- `ONLINE_LEARNING_CANARY_ENABLED=true` phải đi kèm đủ cluster ID, host và
  metric. Scope thiếu hoặc không khớp phải fail-closed.
- Không chuyển sang `ACTIVE` khi chưa đủ 24–72 giờ evidence và operator
  approval.

## Kiểm tra nhanh

```bash
ssh root@10.3.55.213
podman ps --format '{{.Names}} {{.Status}}' \
  | grep -E 'ceph-ai_(watcher|worker|dashboard-web)'
podman exec ceph-ai_watcher_1 env PYTHONPATH=/app python -c \
  'from shared.db import SessionLocal; from shared.learning_runtime import evaluate; \
   s=SessionLocal(); print(evaluate(s, "<cluster-id>", host="10.20.1.153", metric="cpu").as_dict()); s.close()'
```

Trong `/ai-learning`, kiểm tra đồng thời:

- runtime gate, watcher heartbeat và control `RUNNING/PAUSED`;
- learner telemetry: cycle, processed, applied, failed, elapsed và CPU time;
- canary evidence 72 giờ: forecast quality, data-quality, alert lifecycle,
  false positive, pending outcome, early detection và resource cost;
- model registry: candidate chỉ ở `SHADOW`, không có promotion tự động.

`NO_LABEL`, `DATA_QUALITY`, `DRIFT` hoặc `PAUSED` là kết quả fail-closed hợp lệ;
không được đổi thành active update để làm cho dashboard có số đẹp hơn.

## Pause một stream

Pause đúng `cluster_id + host + metric` từ Operator controls hoặc endpoint admin.
Luôn ghi lý do cụ thể. Sau khi kiểm tra, resume và xác nhận audit có cả `PAUSE`
và `RESUME`, control cuối là `RUNNING`.

```bash
# Không ghi secret vào shell history. Dùng session admin đã xác thực trên dashboard.
POST /api/ai-learning/learner/pause
POST /api/ai-learning/learner/resume
```

## Kill switch và rollback khẩn cấp

Khi có soft lockup, poll timeout, DB saturation, CPU/RSS tăng bất thường hoặc
learner ghi lỗi liên tiếp:

1. Bật `ONLINE_LEARNING_KILL_SWITCH=true` trong file runtime
   `/var/lib/ceph-ai/config/.env`.
2. Recreate watcher và worker để nạp environment mới:
   `podman-compose up -d --force-recreate watcher worker`.
3. Xác nhận runtime trả `KILL_SWITCH`, `can_observe=false`, `can_update_active=false`.
4. Không xóa audit/state trước khi điều tra; reset state chỉ qua Operator controls
   với confirmation `RESET`.
5. Nếu cần rollback code, khôi phục file backup cùng timestamp rồi recreate đúng
   service; kiểm tra cả migration head và schema backup trước khi thay đổi DB.

Sau rollback, xác nhận forecast/feedback vẫn còn, heartbeat healthy và không có
promotion ngoài policy.

## Điều kiện promotion

Chỉ operator mới được approve promotion. Candidate phải đạt đủ outcome, chuỗi
evaluation liên tiếp, MAE/SMAPE/false-positive, drift evidence, poll latency và
resource budget. Thiếu evidence, runtime không healthy hoặc candidate đang drift
đều phải bị chặn và ghi `PROMOTION_BLOCKED`.

## Bằng chứng nghiệm thu

Ghi lại timestamp, scope, checksum code/plan, service health, runtime decision,
cycle telemetry và canary report sau mỗi lần review. Không đánh dấu false positive
khi forecast horizon chưa kết thúc; trạng thái đó phải là `pending_outcome`.
