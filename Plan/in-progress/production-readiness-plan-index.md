# Production Readiness Plan Index

## Mục tiêu

Đóng các khoảng trống trước khi cho phép Ceph-AI chạy autonomous remediation trên production. Mặc định giữ hệ thống ở chế độ advisory hoặc approval-required.

## Thứ tự triển khai

1. [01 — Feature exit gates](production-readiness-01-feature-exit-gates.md)
2. [02 — Deterministic test and CI](production-readiness-02-test-and-ci.md)
3. [03 — PostgreSQL and migration foundation](production-readiness-03-postgresql-and-migrations.md)
4. [04 — Executor least privilege](production-readiness-04-executor-isolation.md)
5. [05 — Security baseline](production-readiness-05-security-baseline.md)
6. [06 — Reproducible production deployment](production-readiness-06-reproducible-deployment.md)
7. [07 — Runtime ownership](production-readiness-07-runtime-ownership.md)
8. [08 — Migration rollback discipline](production-readiness-08-migration-rollback.md)
9. [09 — Time and warning hygiene](production-readiness-09-time-and-warnings.md)
10. [10 — Load, chaos and rollback drills](production-readiness-10-chaos-and-release-readiness.md)

## Release policy

- Không bật LIMITED_AUTOPILOT khi Plan 01, 02, 04, 05 hoặc 10 còn mở.
- PostgreSQL là bắt buộc cho production; SQLite chỉ dành cho development hoặc single-node lab.
- Mọi mutation AI phải đi qua policy, approval, idempotency, post-check và audit.
- Mỗi hạng mục hoàn thành phải có commit/release SHA, command kiểm thử, log kết quả và rollback procedure.
- Không đánh dấu hoàn thành chỉ vì UI hoặc API đã tồn tại; phải có evidence runtime.

## Trạng thái ban đầu

- ADVISORY: cho phép sau khi read-only evidence gate đạt.
- APPROVAL_REQUIRED: có thể pilot ở staging sau khi test gate tương ứng đạt.
- LIMITED_AUTOPILOT: chưa được phép trên production.

## Trạng thái cập nhật — 2026-09-18

- Production 03 — PostgreSQL/migration foundation: **đang triển khai**; database gate, pool bounds, backup và restore/downgrade drill đã có evidence.
- Production 05 — Security baseline: **đang triển khai**; production credential/database startup gates, CSRF và multi-replica login rate limit đã có; API rate limit, security regression và trusted proxy boundary còn mở.
- Production 01/02/04/06/07/08/09/10: **chưa nghiệm thu**; không bật LIMITED_AUTOPILOT.
