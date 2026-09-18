# Production Readiness 03 — PostgreSQL and Migration Foundation

## Mục tiêu

Loại bỏ nguy cơ production vô tình chạy SQLite trong hệ thống nhiều process và chuẩn hóa PostgreSQL connection, pool, migration job và backup trước migration.

## Trạng thái thực hiện — 2026-09-18

- [x] Có `CEPH_AI_ENVIRONMENT` để phân biệt development/test/lab/staging/production.
- [x] Production fail-closed nếu `DATABASE_URL` là SQLite; development/test/lab vẫn được phép dùng SQLite.
- [x] PostgreSQL engine có pool size, timeout, recycle và `pool_pre_ping` bounded.
- [x] Backup custom-format trước migration đã tạo và kiểm tra được bằng `pg_restore --list`.
- [x] Restore drill trên PostgreSQL 18 ephemeral container: restore 95 bảng, upgrade tới head hiện tại, downgrade một migration và upgrade lại thành công.
- [ ] Tách migration thành một job canonical có lock/timeout trong deployment pipeline.
- [ ] Ghi revision/checksum/backup identifier vào release manifest production.

Evidence: `tests/test_production_readiness.py`, `tests/test_db.py`; backup
`/var/backups/ceph-ai/ceph-ai-20260918T095529Z.dump`; current-head migration drill
`c1d2e3f4a5b7 → c8d9e0f1a2b4 → b8c9d0e1f2a4 → c8d9e0f1a2b4`. Production phải đặt
`CEPH_AI_ENVIRONMENT=production` trong env file.

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
