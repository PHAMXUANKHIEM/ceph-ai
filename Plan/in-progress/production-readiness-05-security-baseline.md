# Production Readiness 05 — Security Baseline

## Mục tiêu

Biến các cảnh báo bảo mật thành startup gate và middleware policy bắt buộc, thay vì chỉ log cảnh báo rồi tiếp tục chạy.

## Trạng thái thực hiện — 2026-09-18

- [x] Production fail-closed nếu còn `DASHBOARD_PASSWORD_HASH` hoặc `SESSION_SECRET_KEY` mặc định.
- [x] Có regression test bảo đảm production bị từ chối với credential dev mặc định.
- [x] Bật CSRF protection thống nhất cho mọi mutation của Dashboard bằng token
      double-submit (session + cookie), form hidden field và `X-CSRF-Token` cho
      API/fetch; thiếu hoặc sai token bị từ chối trong production.
- [x] Đã hoàn tất Origin/Referer policy, Trusted Host và reverse-proxy boundary;
      production bắt buộc khai báo `DASHBOARD_TRUSTED_HOSTS`/
      `DASHBOARD_ALLOWED_ORIGINS`, chỉ tin `X-Forwarded-*` từ
      `DASHBOARD_TRUSTED_PROXY_IPS`, còn client trực tiếp gửi forwarded header
      sẽ bị từ chối.
- [x] Login và API rate limit đã chuyển sang shared PostgreSQL store cho
      multi-replica; failed-login/API request state được khóa theo row và không
      còn phụ thuộc process memory.
- [x] Security regression cho session fixation, CSRF, host header và brute force
      đã có test coverage.

Evidence hiện tại: `tests/test_production_readiness.py`. Gate chỉ được kích hoạt
khi `CEPH_AI_ENVIRONMENT=production`; các môi trường development/test/lab giữ hành
vi cảnh báo phục vụ local development.

Evidence bổ sung — 2026-09-19:

- Migration `f1b2c3d4e5f6` tạo `auth_login_rate_limits`, đã upgrade trên PostgreSQL
  production-like bằng `scripts/deploy/run_migrations.sh`; backup được tạo trước
  migration và release metadata ghi revision trước/sau.
- `tests/test_dashboard_auth.py`: 22 passed, bao gồm lockout và persistence của
  failed-login state trong database.
- Dashboard container đã restart và health check chuyển sang healthy.
- Migration `f2c3d4e5f6a7` tạo `api_rate_limits`; API production mặc định giới hạn
  120 request/phút/client, store lỗi thì fail-closed 503 và vượt ngưỡng trả 429.
- Security/rate-limit regression suite: `35 passed`.
- Reverse-proxy/header regression suite: `38 passed`; production response có
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` và HSTS khi
  request dùng HTTPS.
- Bổ sung `security_audit_events` và middleware audit tập trung cho mọi POST/
  PUT/PATCH/DELETE; chỉ lưu actor, method, path, status và request-id, không lưu
  body/query secret. Runtime đã xác nhận `POST /logout` tạo event PostgreSQL.
- Route inventory regression xác nhận mọi mutation route (ngoại trừ login/
  logout/product-select public) có login/admin guard; security suite đạt
  `40 passed`.

## Việc cần làm

- Production fail startup nếu còn admin/admin, default password hash hoặc default session secret.
- Bật Secure, HttpOnly và cấu hình SameSite phù hợp với HTTPS.
- Thêm CSRF protection thống nhất cho mọi mutation của Dashboard.
- Xác thực Origin/Referer và CSRF token theo cùng policy cho HTTP/API mutation.
- Khai báo Trusted Host, reverse-proxy trust và security headers rõ ràng. Khi
  không có proxy, để `DASHBOARD_TRUSTED_PROXY_IPS` trống; khi có proxy, chỉ
  khai báo IP/CIDR của proxy tại biến này.
- Đưa login/API rate limit vào shared store khi chạy nhiều replica.
- [~] RBAC guard và audit tập trung đã có regression; cluster scope/tenant
  isolation cần tiếp tục nghiệm thu theo từng nhóm route.
- Thêm security regression cho session fixation, CSRF, host header, cookie flags, brute force và secret redaction.

## Definition of Done

- Security misconfiguration làm production startup fail.
- Mutation không có CSRF/Origin protection bị từ chối.
- Cookie/header/rate-limit tests pass trong single và multi-replica topology.
- Không có credential hoặc secret trong prompt, response, audit và log.
