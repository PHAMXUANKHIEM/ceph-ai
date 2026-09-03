# AI Logic Audit and Rollout Plan

Ngày kiểm tra: 2026-09-03  
Phạm vi: `dashboard`, `worker`, `watcher`, `full-executor`, Telegram, Dual AI/Single Full, backup AI, Log Intelligence, postmortem, runbook, Vitastor, upgrade summary và capability seed.

## Trạng thái baseline

- Server: `10.3.55.213`
- Repository: `/root/ceph-ai`
- Baseline commit: `04943ec9 fix(backup): validate restore drill against source checksum`
- Working tree lúc audit: sạch.
- Cấu hình hiện tại: Codex và Claude cùng bật; Chat ưu tiên Codex; Dual Planner/Implementer dùng Codex; Router đang tắt.

## Luồng provider hiện tại

### Chat Ceph

`dashboard/chat_client.py` thử theo thứ tự Codex → Claude → Router. Tool được server validate; proposal không tự thực thi và cần xác nhận riêng.

### Incident, Log Intelligence, Backup AI

Các luồng này cũng ưu tiên Codex, fallback Claude rồi mới Router. Kết quả có structured output và được validate ở nhiều điểm.

### Dual AI và Single Full

- Dual AI dùng Planner → Implementer/Reviewer.
- Web Dashboard chỉ đọc.
- Telegram Dual được phép ghi vào workspace cô lập và chạy unprivileged.
- Single Full chạy trong executor riêng, có token, lock và xác nhận Telegram; đây là đường dẫn có quyền cao.

## Findings và thứ tự xử lý

### AI-01 — Codex app-server dùng sai event loop — P1

`shared/codex_app_server.py` giữ singleton chứa `asyncio.Queue`, subprocess và task. `watcher/log_analysis.py` gọi `asyncio.run()` cho mỗi lần phân tích. Sau lần đầu, queue/process bị ràng buộc với event loop cũ.

Evidence runtime:

```text
RuntimeError: <Queue ...> is bound to a different event loop
RuntimeError: Event loop is closed
```

Đã thấy 4 lỗi trong 6 giờ. Cách xử lý: mỗi process phải dùng một event loop bền vững cho Codex, hoặc không chia sẻ client async giữa các loop. Không dùng singleton async qua các lần `asyncio.run()`.

### AI-02 — Full Executor làm mất nguyên nhân lỗi — P1

`worker/full_executor.py` chuyển mọi `DualAIChatError` thành HTTP 422. `dashboard/telegram_chat.py` lại biến mọi `httpx.HTTPError` thành “executor không phản hồi”.

Kết quả: lỗi quota, auth, busy, policy 403, lỗi provider và lỗi nội bộ bị hiển thị như nhau. Cách xử lý: dùng response lỗi có `code`, `message`, `provider`, `retryable`; Telegram giữ nguyên detail an toàn và phân loại status.

### AI-03 — Fallback ghi sai provider/budget — P1

`shared/ai_observability.py` chọn provider trước khi gọi. Nếu Codex lỗi rồi Claude thành công, invocation vẫn có thể được ghi là Codex. Cách xử lý: reservation/telemetry phải gắn với provider thực tế; mỗi attempt cần metadata riêng hoặc chỉ reserve sau khi provider đã được chọn.

### AI-04 — Router flag bị bỏ qua trong fallback — P1

Một số module kiểm tra có API key thay vì `router_enabled`. Khi Router bị tắt nhưng key còn tồn tại, hệ thống vẫn có thể gọi Router. Cách xử lý: mọi fallback Router phải yêu cầu đồng thời `router_enabled`, key, base URL và model.

### AI-05 — Dùng non-streaming cho router SSE — P1

Các module còn dùng `chat.completions.create()` trong khi router triển khai thực tế trả SSE và các luồng chính đã phải dùng `.stream()`:

- `shared/incident_postmortem.py`
- `shared/remediation_runbook.py`
- `shared/capability_seed.py`
- `dashboard/routes/vitastor_chat.py`

Cách xử lý: chuẩn hóa qua một adapter OpenAI-compatible duy nhất, có timeout và usage handling thống nhất.

### AI-06 — JSON lỗi không fallback đúng — P1

Ở postmortem, runbook và Log Intelligence, `json.loads()` nằm ngoài `try` của Claude. JSON lỗi có thể thoát thẳng, không thử provider tiếp theo. Cách xử lý: parse và validate nằm trong cùng attempt handler; lỗi schema phải chuyển sang fallback hoặc lỗi có phân loại.

### AI-07 — Dual/Single Full chưa vào Budget Guard chung — P1

`dashboard/dual_ai_chat.py` gọi CLI trực tiếp nhưng không đi qua `observe_ai_call()`/`check_ai_budget()`. Cách xử lý: mọi lần gọi model phải có reservation, usage, cost và audit; Full mode chỉ được bypass sandbox, không được bypass budget/audit.

### AI-08 — Model/effort reload không đầy đủ — P2

Runtime reload hiện chỉ cập nhật hai cờ enable. Thay đổi model/effort được ghi vào `.env` nhưng process Worker có thể vẫn giữ giá trị cũ. Cách xử lý: truyền đủ env khi restart hoặc có cơ chế reload versioned settings.

### AI-09 — Claude prompt truyền qua argv — P2

`shared/claude_cli.py` truyền toàn bộ prompt vào command-line. Rủi ro: lộ nội dung qua process listing và lỗi `Argument list too long`. Cách xử lý: truyền prompt qua stdin nếu CLI hỗ trợ; giới hạn kích thước prompt và history.

### AI-10 — Timeout Codex là timeout từng notification — P2

`run_turn()` chờ từng notification với cùng timeout, không có deadline tổng. Cách xử lý: dùng deadline monotonic tổng cho toàn bộ turn.

### AI-11 — Full Executor nhận history không giới hạn — P2

Pydantic model chỉ giới hạn prompt, không giới hạn số phần tử, role hoặc kích thước từng phần tử của `history`. Cách xử lý: giới hạn số message, role hợp lệ và tổng ký tự trước khi tạo task.

### AI-12 — Lỗi hạ tầng ảnh hưởng AI backup/UI — P2

Dashboard runtime đang có:

```text
column backup_jobs.sha256 does not exist
```

Đây là vấn đề migration, không phải model logic, nhưng làm trang liên quan backup trả 503 và cần xử lý riêng.

### AI-13 — Telegram polling conflict — P2

Runtime có lỗi `getUpdates Conflict: terminated by other getUpdates request`. Cần bảo đảm mỗi bot token chỉ có một polling owner.

## Tiêu chí nghiệm thu từng mục

1. Có test unit/regression cho lỗi đã sửa.
2. Test focused pass.
3. Commit riêng cho từng mục hoặc nhóm nhỏ có cùng nguyên nhân.
4. Triển khai/restart đúng service liên quan.
5. Kiểm tra health, log và rollback point sau triển khai.
6. Không triển khai Full/AI quyền cao nếu chưa xác nhận audit và policy vẫn fail-closed.

## Thứ tự rollout

1. AI-01 event loop.
2. AI-02 lỗi Full Executor.
3. AI-03/AI-04 provider, budget và Router flag.
4. AI-05/AI-06 adapter streaming và JSON fallback.
5. AI-07 budget/audit cho Dual và Single Full.
6. AI-08/AI-09/AI-10/AI-11 reliability và input limits.
7. Migration backup và Telegram polling conflict.

