# Production Readiness 05 — Security Baseline

## Mục tiêu

Biến các cảnh báo bảo mật thành startup gate và middleware policy bắt buộc, thay vì chỉ log cảnh báo rồi tiếp tục chạy.

## Trạng thái thực hiện — 2026-09-18

- [x] Production fail-closed nếu còn `DASHBOARD_PASSWORD_HASH` hoặc `SESSION_SECRET_KEY` mặc định.
- [x] Có regression test bảo đảm production bị từ chối với credential dev mặc định.
- [x] Bật CSRF protection thống nhất cho mọi mutation của Dashboard bằng token
      double-submit (session + cookie), form hidden field và `X-CSRF-Token` cho
      API/fetch; thiếu hoặc sai token bị từ chối trong production.
- [~] Đã hoàn tất Origin/Referer policy và Trusted Host; production bắt buộc khai báo
      `DASHBOARD_TRUSTED_HOSTS`/`DASHBOARD_ALLOWED_ORIGINS` và không tin
      `X-Forwarded-*` nếu chưa có proxy boundary được cấu hình. Trusted
      reverse-proxy boundary và header policy vẫn còn phải nghiệm thu.
- [ ] Chuyển login/API rate limit sang shared store cho multi-replica.
- [ ] Hoàn tất security regression cho session fixation, CSRF, host header và brute force.

Evidence hiện tại: `tests/test_production_readiness.py`. Gate chỉ được kích hoạt
khi `CEPH_AI_ENVIRONMENT=production`; các môi trường development/test/lab giữ hành
vi cảnh báo phục vụ local development.

## Việc cần làm

- Production fail startup nếu còn admin/admin, default password hash hoặc default session secret.
- Bật Secure, HttpOnly và cấu hình SameSite phù hợp với HTTPS.
- Thêm CSRF protection thống nhất cho mọi mutation của Dashboard.
- Xác thực Origin/Referer và CSRF token theo cùng policy cho HTTP/API mutation.
- Khai báo Trusted Host, reverse-proxy trust và security headers rõ ràng.
- Đưa login/API rate limit vào shared store khi chạy nhiều replica.
- Kiểm tra RBAC, cluster scope, tenant isolation và audit cho mọi route ghi.
- Thêm security regression cho session fixation, CSRF, host header, cookie flags, brute force và secret redaction.

## Definition of Done

- Security misconfiguration làm production startup fail.
- Mutation không có CSRF/Origin protection bị từ chối.
- Cookie/header/rate-limit tests pass trong single và multi-replica topology.
- Không có credential hoặc secret trong prompt, response, audit và log.
