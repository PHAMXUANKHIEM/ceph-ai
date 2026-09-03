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

`shared/ai_observability.py` chọn provider trước khi gọi. Nếu Codex lỗi rồi Claude thành công, invocation có thể bị ghi là Codex; Router fallback và Vitastor còn có thể ghi sai model. Cách xử lý đã triển khai: mọi adapter Codex/Claude/Router đánh dấu provider/model thực tế qua `ContextVar`, decorator refresh cấu hình trước budget preflight và không tính input cho lỗi xảy ra trước adapter; Vitastor có model override riêng. Hard budget vẫn reserve trước theo provider ưu tiên để fail-closed và bảo thủ.

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

## Rollout thực tế

- AI-01: đã triển khai, commit `e5fc7e30`; Watcher healthy sau restart.
- AI-02: đã triển khai, commit `0588cdd5`; Full Executor và Telegram healthy sau restart.
- AI-03: đã triển khai đầy đủ, commit `bb06ca7a` (bổ sung trên `f38698db`); mọi đường Router fallback đều ghi provider/model thực tế, cấu hình được refresh trước budget preflight, lỗi preflight không tính input billable, và Vitastor có model override riêng trên Settings. Hard budget vẫn reserve trước theo provider ưu tiên (ước tính bảo thủ), còn Dual/Single Full đã kiểm tra theo provider thực tế ở từng attempt trong commit `8aecd322`.
- AI-04: đã triển khai, commit `357305ad`; Dashboard/Worker/Watcher healthy sau restart.
- AI-05/AI-06: đã triển khai, commit `3f639475`; các test adapter/structured fallback liên quan đạt.
- AI-07: đã triển khai, commit `8aecd322`; Dual/Single Full có Budget Guard và audit invocation.
- AI-08 đến AI-11: đã triển khai, commit `d8f04c1d`; model reload, stdin, deadline và history limits đã được áp dụng.
- AI-12: migration `2b3c4d5e6f7a` đã tồn tại; kiểm tra trực tiếp DB runtime xác nhận `backup_jobs.sha256` đã có và Alembic đang ở head `4d5e6f7a8b9c`. Lỗi log trước đó là từ thời điểm schema chưa đồng bộ.
- AI-13: chưa thể kết thúc từ server này. Sau khi restart `ceph-ai_telegram-ai_1`, cả hai token vẫn nhận `getUpdates 409 Conflict`; `ps`/Podman chỉ thấy một poller local. Cần dừng poller đang chạy ở host/instance bên ngoài hoặc chuyển sang webhook trước khi xác nhận hoàn tất.

## Test sau rollout

- Nhóm core provider/chat/backup/log/volume: `232 passed`, 7 test cũ lệch chuỗi xưng hô mặc định.
- Full/Telegram/Dual: `25 passed`.
- Streaming/structured modules: `57 passed`, 1 lỗi setup do fixture DB xoá cluster.
- Reliability/CLI/Full history: `44 passed`.
- Budget/Dual/observability: `13 passed`.
- Bổ sung regression cho Router fallback, preflight budget và Vitastor CLI model: `29 passed`.
- Không có thay đổi chưa commit sau các bước triển khai; các lỗi còn lại được ghi ở trên là test fixture/state hoặc xung đột poller bên ngoài.
