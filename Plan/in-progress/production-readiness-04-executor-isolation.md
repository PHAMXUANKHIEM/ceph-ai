# Production Readiness 04 — Executor Least Privilege and Isolation

## Mục tiêu

Giảm blast radius nếu Dashboard, Telegram, AI provider hoặc executor bị khai thác; không cho AI có quyền vượt quá action đã được policy duyệt.

## Trạng thái thực hiện — 2026-09-18

- [x] `full-executor` không còn chạy `privileged`; chạy bằng `aiagent` (`10001:10001`), `read_only`, `no-new-privileges` và drop toàn bộ capability.
- [x] Bỏ mount D-Bus/system bus và bỏ source/config mount ghi; `/app` và state production được mount read-only.
- [x] Provider child process dùng environment allow-list; không kế thừa Telegram token, executor token, session secret hoặc toàn bộ environment của service.
- [x] SSH key và provider account của executor được provision vào cây credential riêng, owner là `aiagent`, rồi mount read-only.
- [x] Tách deployment artifact khỏi source checkout của `full-executor`; source được đóng gói trong image và không còn bind-mount `/app`.
- [ ] Giới hạn network egress của executor theo allow-list Ceph nodes, broker và provider.
- [ ] Hoàn tất proof-of-containment thực tế cho filesystem, network, process và secret boundary trên host production.

Evidence: `tests/test_executor_isolation.py`, `tests/test_full_executor.py`,
`tests/test_resource_limits.py`; focused suite `9 passed`.

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
