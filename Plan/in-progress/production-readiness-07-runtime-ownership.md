# Production Readiness 07 — Runtime Ownership

## Mục tiêu

Chọn đúng một cơ chế quản lý runtime và chứng minh mỗi watcher, worker và Telegram poller chỉ có một owner đang hoạt động.

## Việc cần làm

- Chọn một mô hình canonical: systemd quản lý Podman stack hoặc systemd quản lý từng process; không duy trì hai mô hình song song.
- Tạo runtime manifest ghi owner, unit/container, PID, version và start time.
- Disable/remove legacy nohup, systemd units và container không thuộc owner.
- Giữ một Telegram getUpdates consumer cho mỗi bot token; kiểm tra distributed lease khi chạy nhiều replica hoặc failover.
- Giữ một remediation watcher cho mỗi cluster/runtime scope; lease phải fail-closed.
- Kiểm tra restart, deploy, rollback và host reboot không tạo process trùng.
- Thêm alert nếu phát hiện duplicate watcher, duplicate poller hoặc stale owner.

## Definition of Done

- Runtime inventory không có owner ambiguity.
- Reboot/deploy/restart drill chứng minh không có duplicate consumer.
- Telegram không phát sinh 409 Conflict trong failover test.
- Hai worker không thể cùng chạy một action nhờ distributed lease/idempotency.
