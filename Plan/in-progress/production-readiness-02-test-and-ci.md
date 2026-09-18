# Production Readiness 02 — Deterministic Test and CI

## Mục tiêu

Tạo release gate lặp lại được trong container CI chuẩn, không phụ thuộc proxy, RabbitMQ có sẵn trên host, credential developer hoặc trạng thái checkout.

## Việc cần làm

- Pin Python, dependency, Node và system packages trong CI image.
- Chạy test với proxy/network environment đã được làm sạch và ghi rõ.
- Dùng RabbitMQ temporary vhost/container cho integration test; không dùng queue production chung.
- Mock hoặc disable AI trong backup failure-path; test provider riêng.
- Sửa test persistent cache để chỉ đọc file JSON, không coi file lock là payload.
- Chạy full suite hai lần liên tiếp trên cùng image để bắt flake.
- Phân loại failure thành code bug, test bug, environment gap hoặc flaky.
- Đặt warning budget và xuất JUnit/test summary vào release artifact.

## Definition of Done

- Full release suite pass hai lần liên tiếp trong CI container.
- Không phụ thuộc proxy, broker ngoài hoặc secret máy developer.
- Test cache lock, RabbitMQ và backup failure-path pass trong môi trường cô lập.
- Mọi failure làm release gate fail cho tới khi có phân loại và waiver được duyệt.
- Số test, failure, deselected và warning được ghi cùng commit SHA.
