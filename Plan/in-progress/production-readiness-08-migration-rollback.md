# Production Readiness 08 — Migration Rollback Discipline

## Mục tiêu

Đảm bảo schema và audit/action state vẫn phục hồi được khi migration hoặc rollout thất bại, đặc biệt với số lượng migration lớn.

## Trạng thái thực hiện — 2026-09-18

- [x] Repository có một Alembic head hiện tại: `c8d9e0f1a2b4`.
- [x] Restore database snapshot production-like và chạy upgrade tới head.
- [x] Chạy downgrade một migration rồi upgrade lại tới head trên PostgreSQL 18 ephemeral database.
- [x] Backup custom-format và restore listing đã được xác minh trước drill.
- [ ] Ghi checksum/revision/compatibility window vào release manifest tự động.
- [ ] Diễn tập worker/dashboard đang đọc database trong lúc migration và xử lý partial failure.

Evidence: current-head drill `c1d2e3f4a5b7 → c8d9e0f1a2b4 →
b8c9d0e1f2a4 → c8d9e0f1a2b4`; migration head check bằng `alembic heads`.

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
