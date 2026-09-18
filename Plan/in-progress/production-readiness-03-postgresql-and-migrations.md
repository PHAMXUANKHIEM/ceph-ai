# Production Readiness 03 — PostgreSQL and Migration Foundation

## Mục tiêu

Loại bỏ nguy cơ production vô tình chạy SQLite trong hệ thống nhiều process và chuẩn hóa PostgreSQL connection, pool, migration job và backup trước migration.

## Việc cần làm

- Fail startup ở production nếu DATABASE_URL không phải PostgreSQL.
- Chỉ cho phép SQLite khi có cờ development/test rõ ràng.
- Kiểm tra pool size, timeout, recycle và retry phù hợp với số service/worker.
- Tạo migration job một lần; không để mọi container tự chạy migration cạnh tranh.
- Backup database trước migration và kiểm tra restore của backup.
- Kiểm tra một Alembic head, upgrade trên database tạm, rollback theo policy và upgrade lại.
- Kiểm thử nhiều process cùng ghi audit/action/learning state.
- Ghi database backend, migration revision và backup identifier vào release manifest.

## Definition of Done

- Production không thể khởi động với SQLite hoặc credential DB mặc định.
- PostgreSQL upgrade/rollback/upgrade lại pass trong CI và staging.
- Migration job idempotent, có timeout, log và lock rõ ràng.
- Backup trước migration được xác nhận có thể restore.
- Release manifest chứa revision, checksum migration và kết quả kiểm thử.
