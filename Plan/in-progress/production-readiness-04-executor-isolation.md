# Production Readiness 04 — Executor Least Privilege and Isolation

## Mục tiêu

Giảm blast radius nếu Dashboard, Telegram, AI provider hoặc executor bị khai thác; không cho AI có quyền vượt quá action đã được policy duyệt.

## Việc cần làm

- Loại bỏ privileged: true khỏi full-executor hoặc tách executor sang host/VM riêng.
- Chạy service bằng user không phải root, với filesystem read-only mặc định.
- Tách source workspace khỏi deployment artifact; dùng immutable image để deploy.
- Tách SSH key/certificate theo cluster và capability; hạn chế user, host và command.
- Đưa command allowlist, argument validation và capability gate vào service độc lập.
- Không mount D-Bus/systemd nếu không có use case bắt buộc; nếu bắt buộc phải có proxy allowlist.
- Giới hạn network egress của executor tới Ceph nodes, broker và provider cần thiết.
- Kiểm thử prompt injection, path traversal, command injection, workspace escape và secret exfiltration.

## Definition of Done

- Executor production chạy non-root và không cần privileged mode, hoặc có risk exception được operator phê duyệt.
- Không có shell tự do, mount source ghi trực tiếp hoặc SSH key dùng chung toàn hệ thống.
- Mọi action được allowlist, audit và post-check.
- Có proof-of-containment test cho filesystem, network, process và secret boundary.
