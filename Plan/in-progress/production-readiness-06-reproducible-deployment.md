# Production Readiness 06 — Reproducible Production Deployment

## Mục tiêu

Biến Compose/deploy script thành quy trình có thể tái lập, quan sát và rollback, không phụ thuộc trạng thái ngầm của một host cụ thể.

## Việc cần làm

- Quyết định rõ PostgreSQL/RabbitMQ là managed dependency hay service trong stack; thêm preflight kiểm tra version, credential, TLS, vhost và permission.
- Pin image bằng digest hoặc release artifact immutable; không dùng :local cho production release.
- Tách secrets khỏi .env thường và kiểm tra permission/rotation.
- Ghi rõ mọi host mount, SSH key, CLI package và runtime dependency trong manifest.
- Bổ sung log rotation, size limit, retention và disk-space alert.
- Giữ resource limit hiện có và bổ sung validation để không deploy thiếu limit.
- Tạo SBOM, image scan, config validation và health/readiness check trước rollout.
- Tạo rollback script theo release SHA, image digest, migration revision và config version.

## Definition of Done

- Host mới có thể deploy từ release artifact theo một runbook duy nhất.
- Không có dependency ngầm hoặc đường dẫn hard-coded chưa được preflight.
- Logs không thể lấp đầy disk; secrets không nằm trong image/log.
- Có smoke test, health test và rollback test sau deploy.
