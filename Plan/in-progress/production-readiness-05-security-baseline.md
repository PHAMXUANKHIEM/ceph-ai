# Production Readiness 05 — Security Baseline

## Mục tiêu

Biến các cảnh báo bảo mật thành startup gate và middleware policy bắt buộc, thay vì chỉ log cảnh báo rồi tiếp tục chạy.

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
