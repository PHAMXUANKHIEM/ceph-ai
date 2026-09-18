# Production Readiness 09 — Timezone and Warning Hygiene

## Mục tiêu

Chuẩn hóa timestamp về timezone-aware UTC và giảm warning kỹ thuật để tránh lỗi TTL, cooldown, stale-data, forecast và migration khi nâng Python/SQLAlchemy.

## Việc cần làm

- Tìm và thay toàn bộ datetime.utcnow() trong production code bằng helper UTC timezone-aware thống nhất.
- Chuẩn hóa model/database column, serialization API, log và frontend timestamp.
- Xác định rõ timestamp nào là UTC, timezone hiển thị nào do browser/operator chọn.
- Thêm regression cho DST, naive/aware comparison, TTL, cooldown, retention và snapshot age.
- Chạy warnings-as-errors cho warning thuộc production code; lập waiver cho dependency warning chưa sửa được.
- Theo dõi warning count theo release và đặt budget không tăng.

## Definition of Done

- Không còn naive timestamp trong production path được kiểm soát.
- TTL/cooldown/stale-data tests pass ở timezone UTC và non-UTC.
- Warning report có owner, nguyên nhân và trạng thái xử lý.
- Không dùng warning suppression để che lỗi logic thời gian.
