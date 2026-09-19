# Production Readiness 03 — PostgreSQL and Migration Foundation

## Mục tiêu

Loại bỏ nguy cơ production vô tình chạy SQLite trong hệ thống nhiều process và chuẩn hóa PostgreSQL connection, pool, migration job và backup trước migration.

## Trạng thái thực hiện — 2026-09-18

- [x] Có `CEPH_AI_ENVIRONMENT` để phân biệt development/test/lab/staging/production.
- [x] Production fail-closed nếu `DATABASE_URL` là SQLite; development/test/lab vẫn được phép dùng SQLite.
- [x] PostgreSQL engine có pool size, timeout, recycle và `pool_pre_ping` bounded.
- [x] Có migration runner canonical `scripts/deploy/run_migrations.sh`: backup trước migration, production SQLite gate và chạy `alembic upgrade head` một lần.
- [x] Alembic PostgreSQL dùng `pg_try_advisory_lock` để fail-closed khi có migration job cạnh tranh.
- [x] Backup custom-format trước migration đã tạo và kiểm tra được bằng `pg_restore --list`.
- [x] Restore drill trên PostgreSQL 18 ephemeral container: restore 95 bảng, upgrade tới head hiện tại, downgrade một migration và upgrade lại thành công.
- [x] Tách migration thành một job canonical trong deployment pipeline; PostgreSQL dùng advisory lock fail-closed.
- [x] Migration runner tự ghi revision trước/sau, head, checksum migration, commit SHA và backup path vào release artifact JSON `0600`.

Evidence: `tests/test_production_readiness.py`, `tests/test_db.py`; backup
`/var/backups/ceph-ai/ceph-ai-20260918T095529Z.dump`; current-head migration drill
`c1d2e3f4a5b7 → c8d9e0f1a2b4 → b8c9d0e1f2a4 → c8d9e0f1a2b4`. Production phải đặt
`CEPH_AI_ENVIRONMENT=production` trong env file. Release artifact mặc định nằm tại
`/var/lib/ceph-ai/release-artifacts/migration-latest.json`; artifact không chứa
connection string hoặc secret.

## Việc cần làm

- Fail startup ở production nếu DATABASE_URL không phải PostgreSQL.
- Chỉ cho phép SQLite khi có cờ development/test rõ ràng.
- Kiểm tra pool size, timeout, recycle và retry phù hợp với số service/worker.
- Tạo migration job một lần; không để mọi container tự chạy migration cạnh tranh.
- Backup database trước migration và kiểm tra restore của backup.
- Kiểm tra một Alembic head, upgrade trên database tạm, rollback theo policy và upgrade lại.
- Kiểm thử nhiều process cùng ghi audit/action/learning state.
- Ghi database backend, migration revision, checksum, commit SHA và backup identifier vào release artifact.

## Definition of Done

- Production không thể khởi động với SQLite hoặc credential DB mặc định.
- PostgreSQL upgrade/rollback/upgrade lại pass trong CI và staging.
- Migration job idempotent, có timeout, log và lock rõ ràng.
- Backup trước migration được xác nhận có thể restore.
- Release artifact chứa revision trước/sau, head, checksum migration, commit SHA và backup path; file được ghi atomic với mode `0600`.
