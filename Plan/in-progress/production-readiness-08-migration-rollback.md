# Production Readiness 08 — Migration Rollback Discipline

## Mục tiêu

Đảm bảo schema và audit/action state vẫn phục hồi được khi migration hoặc rollout thất bại, đặc biệt với số lượng migration lớn.

## Việc cần làm

- Duy trì đúng một Alembic head và kiểm tra tự động trong CI.
- Với mỗi release, ghi revision trước/sau, migration checksum và compatibility window.
- Test upgrade từ database snapshot production-like, rollback theo policy và upgrade lại.
- Xác định migration nào không downgrade được; thay bằng backup/restore procedure được duyệt.
- Chạy migration bằng job riêng, có lock, timeout và log đầy đủ.
- Backup database/config/audit trước migration; kiểm tra restore trước rollout.
- Kiểm thử migration khi Worker, Watcher và Dashboard đang đọc database.
- Định nghĩa rollback khi migration đã commit một phần hoặc service restart giữa chừng.

## Definition of Done

- Single-head check pass.
- Mỗi migration release có upgrade evidence và rollback/recovery evidence.
- Có backup restore test thành công trên database tương thích production.
- Không mất audit trail, action state hoặc approval state sau failure drill.
