# Production Readiness 01 — Feature Exit Gates

## Mục tiêu

Đưa mọi tính năng P0/P1 từ trạng thái “đã có code/UI” sang trạng thái có đủ evidence để phát hành.

## Phạm vi

- Plan/ai-missing-features-roadmap.md
- Plan/end-to-end-feature-completion-and-deployment-plan.md
- Realtime snapshot, Block Storage, Backup/Restore, Object Storage/RGW và AI intelligence.

## Việc cần làm

- Lập ma trận từng exit gate: feature, owner, dependency, test command, runtime evidence, rollback và trạng thái.
- Hoàn tất Pha 0.2/0.3 capability matrix/preflight trước khi mở remediation.
- Đóng các gate P0/P1 trước khi bắt đầu Pha 8/9 closed-loop/autopilot.
- Kiểm tra snapshot cho từng section; không gộp “đã có snapshot” thành “đã hoàn tất realtime dashboard”.
- Chạy browser acceptance trên default, secondary, inactive, degraded và stale cluster.
- Cập nhật Plan bằng commit phát hành, không dùng ghi chú chưa commit làm bằng chứng.

## Definition of Done

- Không còn P0/P1 chưa có owner hoặc acceptance criteria.
- Mỗi feature có test unit, integration, security, failure-path và runtime smoke.
- Mọi kết luận AI có evidence, timestamp, freshness, confidence và cluster scope.
- Mọi mutation có approval, post-check, idempotency và audit.
- Exit gate được ký bằng release SHA và operator sign-off.
