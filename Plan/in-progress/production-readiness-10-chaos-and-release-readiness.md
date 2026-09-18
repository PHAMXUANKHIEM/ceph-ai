# Production Readiness 10 — Load, Chaos and Release Readiness

## Mục tiêu

Chứng minh hệ thống an toàn trong failure mode thực tế trước khi chuyển từ approval-required sang limited autopilot.

## Kịch bản bắt buộc

- RabbitMQ mất kết nối trong lúc action đang chạy.
- Worker chết sau khi chạy lệnh nhưng trước khi commit.
- Hai operator duyệt cùng một action.
- Network partition giữa Dashboard/Worker/Ceph/MON.
- SSH timeout trong khi command ở xa vẫn tiếp tục chạy.
- PostgreSQL restart/failover giữa transaction.
- Restart toàn bộ stack cùng lúc và host reboot.
- AI provider timeout, rate-limit, trả JSON sai hoặc hết quota.
- Post-check thất bại, rollback thất bại hoặc action chỉ thành công một phần.
- Nhiều cluster tạo incident đồng thời; kiểm tra scope, queue và lease.
- Duplicate Telegram poller/watcher và stale lock.
- Snapshot stale, collector chết, disk cache đầy và log disk gần đầy.

## Việc cần làm

- Xây failure-injection harness cho broker, DB, SSH, provider, worker và Ceph mock.
- Chạy load test với nhiều cluster, nhiều incident và nhiều browser session.
- Đo p95/p99 cho dashboard, collector, queue, action và post-check.
- Diễn tập backup restore, action rollback, kill switch và disable autopilot.
- Chạy staging soak test tối thiểu một chu kỳ maintenance đầy đủ.
- Ghi incident report, evidence, residual risk và quyết định promotion.

## Definition of Done

- Mọi scenario có expected state machine và kết quả thực tế.
- Không có action chạy trùng, mất audit, báo thành công giả hoặc bypass approval.
- Kill switch hoạt động và được kiểm chứng từ operator path.
- Rollback/restore drill đạt tiêu chí RTO/RPO đã công bố.
- Chỉ sau khi gate này đạt mới xem xét LIMITED_AUTOPILOT cho một allowlist SAFE trên một cluster staging/canary.
