# Cảnh báo Telegram — 4 loại độc lập (Backup / Lỗi cụm / Phần cứng / Phê duyệt)

Tài liệu này mô tả toàn bộ hệ thống Telegram của ceph-aiops: **một** Bot
Token/Chat ID dùng chung, nhưng **bốn mục bật/tắt hoàn toàn độc lập** —
Backup, Lỗi cụm (cluster health), Phần cứng (CPU/RAM node), và **Yêu cầu
phê duyệt** (mục thứ 4, khác hẳn 3 mục đầu — xem mục 6). Tắt một mục không
ảnh hưởng tới các mục còn lại.

Hướng dẫn thao tác từng bước (tạo Bot, lấy Chat ID) nằm ngay trong app tại
**Settings → Cảnh báo Telegram → "hướng dẫn kết nối Telegram từng bước"**
(`/settings/telegram/help`) — tài liệu này tập trung vào **thiết kế/hành
vi**, không lặp lại phần thao tác click-by-click đã có trong app.

Nguồn: `shared/telegram_client.py`, `shared/telegram_alerts.py`,
`worker/backup/alerting.py`, `watcher/main.py`, `watcher/node_health_monitor.py`,
`dashboard/telegram_approval_bot.py`, `dashboard/routes/actions.py`,
`dashboard/routes/settings.py`, `config/settings.py`.

## 1. Nguyên tắc thiết kế chung — 3/4 mục MỘT CHIỀU, 1 ngoại lệ có chủ đích

3 mục đầu (Backup/Lỗi cụm/Phần cứng) dùng chung một hàm gửi tin nhắn thuần
(`shared/telegram_client.py::send_telegram_message`) — gọi thẳng Telegram
Bot API `sendMessage`, không dùng `parse_mode` (tránh lỗi 400 nếu nội dung
động chứa ký tự `<`/`>`/`&`), **chỉ gửi ra, không bao giờ nhận vào**: không
có phản hồi nào được đọc lại, không có gì gõ trên Telegram chạm được tới
`worker/executor/` qua 3 mục này.

**Ngoại lệ có chủ đích, không phải sơ suất:** mục thứ 4 ("Yêu cầu phê
duyệt qua Telegram", `dashboard/telegram_approval_bot.py`) là nơi DUY NHẤT
trong toàn bộ codebase đọc phản hồi đến từ Telegram (long-polling
`getUpdates`) — vì chính tính năng đó YÊU CẦU làm vậy (bấm nút Duyệt/Từ
chối). Đây là quyết định tính năng rõ ràng, được xác nhận riêng với vận
hành viên trước khi triển khai, có công tắc bật/tắt RIÊNG (mặc định TẮT),
và dù bật hay tắt, khi callback đến từ mục này nó cũng CHỈ gọi lại đúng
logic Duyệt/Từ chối `dashboard/routes/actions.py` đã có sẵn cho nút trên
Dashboard — không có đường nào mới để thực thi thứ gì đó chưa từng thực
thi được trước đây. Xem mục 6 để hiểu đầy đủ trước khi bật.

Mọi lần gửi đều **best-effort** — một lỗi gửi (token sai, chưa thêm bot vào
nhóm, mất mạng) chỉ được log rồi bỏ qua, không bao giờ làm hỏng lần
backup/quét sức khoẻ/quét CPU-RAM/duyệt đã kích hoạt nó.

## 2. Một cấu hình, bốn công tắc

Cả 4 mục dùng **chung một cặp Bot Token/Chat ID** — cấu hình một lần tại
Settings → **Cảnh báo Telegram** (chỉ admin thấy/sửa), 4 checkbox độc lập:

| Công tắc (`config/settings.py`) | Đọc bởi | Chạy trong tiến trình | Chỉ thông báo hay có thể hành động? |
|---|---|---|---|
| `telegram_alerts_enabled` | `worker/backup/alerting.py::send_alert` | **Worker** | Chỉ thông báo |
| `telegram_incident_alerts_enabled` | `shared/telegram_alerts.py::send_incident_alert`, gọi từ `watcher/main.py` | **Watcher** | Chỉ thông báo |
| `telegram_node_alerts_enabled` | `shared/telegram_alerts.py::send_node_alert`, gọi từ `watcher/node_health_monitor.py` | **Watcher** | Chỉ thông báo |
| `telegram_approval_requests_enabled` | `dashboard/telegram_approval_bot.py` | **Dashboard** | **Duyệt/Từ chối được thật** |

Vì cấu hình này được đọc bởi **ba tiến trình khác nhau** (Worker, Watcher,
và chính Dashboard), form Lưu trên Settings **khởi động lại cả Worker lẫn
Watcher** — nhưng KHÔNG cần khởi động lại Dashboard cho mục thứ 4: nó chạy
ngay trong tiến trình Dashboard đang xử lý request Lưu đó, nên 2 luồng nền
của nó (xem mục 6) đọc `settings` mới nhất ngay ở lượt lặp kế tiếp.

Bật một công tắc mà Bot Token/Chat ID còn trống → bị từ chối ngay khi Lưu
("Cần điền đủ Bot token và Chat ID trước khi bật bất kỳ loại cảnh báo
Telegram nào") — áp dụng chung cho cả 4 công tắc.

Nút **"Gửi thử"** gửi một tin nhắn xác nhận bằng cấu hình **đã lưu** (không
phải giá trị chưa lưu trên form) — chỉ xác nhận Bot Token/Chat ID hoạt
động, không phụ thuộc công tắc nào đang bật/tắt.

## 3. Cảnh báo Backup (`telegram_alerts_enabled`)

Đã có từ trước (xem [ceph-backup.md](./ceph-backup.md), mục 7.2b) —
`worker/backup/alerting.py::send_alert()` là điểm gửi cảnh báo backup duy
nhất, tự động phủ:

- Backup thất bại (`FAILED`) hoặc quá hạn RPO 24h/chưa từng chạy
- RestoreDrill thất bại (`critical`)
- Bất thường (anomaly) mức `critical` do AI phát hiện (duration/size lệch
  bất thường so với lịch sử)
- BackupDigest tổng hợp hàng ngày (mức `info`)

Gửi song song với webhook chung đã có sẵn (`backup_alert_webhook_url`) —
hai kênh độc lập, không thay thế nhau.

## 4. Cảnh báo lỗi cụm (`telegram_incident_alerts_enabled`)

Nguồn: `watcher/main.py::build_and_publish_incident` (điểm DUY NHẤT tạo
`Incident` cho một vấn đề Ceph **thật**, phát hiện qua `ceph health
detail`).

### 4.1. Khi nào gửi

Gửi **một tin nhắn cho mỗi check** ngay khi `Incident` tương ứng được ghi
vào DB — cùng độ chi tiết với cách Incident được tạo (một cụm có 2 vấn đề
cùng lúc, vd `MON_CLOCK_SKEW` + `OSD_DOWN`, nhận **2 tin nhắn riêng**, không
gộp thành một). Chỉ gửi khi **transition VÀO** `HEALTH_WARN`/`HEALTH_ERR`
(khớp đúng lúc `build_and_publish_incident` tự nó được gọi) — không gửi lại
mỗi lần poll trong khi vấn đề vẫn còn treo, và không gửi gì khi cụm hồi
phục về `HEALTH_OK` (việc đóng Incident do `_resolve_recovered_incidents`
xử lý riêng, không kèm cảnh báo Telegram — xem mục 6 vì sao).

### 4.2. Định dạng tin nhắn

```
🔴 HEALTH_ERR Cụm Ceph: OSD_DOWN
osd.3 (root=default,host=node2) is down
```

(🟡 cho `HEALTH_WARN`, 🔴 cho `HEALTH_ERR`) — phần thân là `log_excerpt` đã
thu thập cho đúng Incident đó (cùng nội dung đang hiển thị trên Dashboard),
**cắt bớt ở 800 ký tự** nếu quá dài (một số check như slow-ops/PG dump có
thể dài tới vài KB, không cần thiết trong một tin nhắn điện thoại và gần
chạm giới hạn 4096 ký tự của Telegram).

### 4.3. Phạm vi — chỉ vấn đề Ceph THẬT

Chỉ áp dụng cho `ceph_code` không có tiền tố (mã check thật của Ceph, vd
`MON_DOWN`, `OSD_DOWN`, `PG_DEGRADED`) — **không** bao gồm các họ Incident
tổng hợp khác đã có sẵn tiền tố riêng (`VOLUME_SATURATED:`,
`DEVICE_HEALTH_EVACUATE:`, `NODE_RESOURCE_HIGH:`, `CHAT_REQUEST`,
`CLUSTER_UPGRADE`) — những họ đó có vòng đời tạo/đóng riêng, gọi
`send_incident_alert` sẽ sai ngữ cảnh (không phải một check `ceph health
detail` thật).

## 5. Cảnh báo phần cứng (`telegram_node_alerts_enabled`)

Module mới hoàn toàn: `watcher/node_health_monitor.py` — trước tính năng
này, ceph-aiops **không có bất kỳ cơ chế cảnh báo CPU/RAM nào** (trang
Nodes chỉ truy vấn trực tiếp, hiển thị số liệu tức thời, không lưu lịch sử,
không có ngưỡng).

### 5.1. Cách quét

- Quét **mọi node đã cấu hình** (MON+MGR+OSD+RGW, gộp trùng —
  `shared/cluster_nodes.py::configured_nodes()`), lấy CPU%/RAM% qua
  `watcher/node_metrics.py::collect_node_metrics()` (SSH, đọc `/proc/stat`
  + `/proc/meminfo` 2 lần cách nhau ~1s để tính %).
- Chạy trên **nhịp quét RIÊNG, chậm hơn nhiều** so với health-check chính
  (`node_health_scan_interval_seconds`, **mặc định 15 phút**) — vì mỗi lần
  quét cần một round-trip SSH THẬT SỰ trên MỖI node; chạy dày như
  health-check (15 giây/lần) sẽ tạo tải SSH đáng kể không cần thiết. Cùng
  cách thiết kế với `device_health_scan_interval_seconds` đã có từ trước
  (quét dự đoán ổ đĩa hỏng, mặc định 1 giờ) — không có UI để chỉnh, sửa
  trực tiếp giá trị mặc định trong `config/settings.py` nếu cần đổi.
- Một node lỗi SSH ở một lượt quét chỉ bỏ qua đúng node đó (log cảnh báo),
  không chặn việc quét các node còn lại.

### 5.2. Ngưỡng cảnh báo

| Hằng số (`watcher/node_health_monitor.py`) | Giá trị mặc định | Ý nghĩa |
|---|---|---|
| `CPU_ALERT_THRESHOLD_PERCENT` | 90% | CPU node ≥ ngưỡng này coi là cao |
| `MEM_ALERT_THRESHOLD_PERCENT` | 90% | RAM node ≥ ngưỡng này coi là cao |
| `CONSECUTIVE_SCANS_REQUIRED` | 2 | Phải cao liên tiếp bấy nhiêu lượt quét mới báo động |

CPU **hoặc** RAM vượt ngưỡng (không cần cả hai) đã tính là "cao" ở một lượt
quét; phải giữ trạng thái "cao" **2 lượt quét liên tiếp** (~30 phút ở nhịp
mặc định) mới thực sự tạo Incident — tránh báo động vì một đợt tăng tải
ngắn hạn bình thường (backup export, scrub, ...). Một lượt quét "bình
thường" xen giữa sẽ **reset chuỗi về 0**, không cộng dồn qua các đợt tăng
tải rời rạc.

Đây là **ngưỡng tuyệt đối cố định trong code**, không có UI/`.env` để chỉnh
— cùng quy ước với các ngưỡng heuristic khác trong Watcher (vd
`watcher/volume_monitor.py`'s `NEAR_PEAK_RATIO`). Muốn đổi, sửa trực tiếp
2 hằng số trên trong `watcher/node_health_monitor.py`.

### 5.3. Vòng đời Incident + hành động

Cùng khuôn mẫu với `watcher/volume_monitor.py` (Volume Saturation) và
`watcher/device_health_monitor.py` (dự đoán ổ đĩa hỏng): mỗi host bị gắn cờ
tạo một `Incident` (`ceph_code = "NODE_RESOURCE_HIGH:<host>"`,
`PENDING_APPROVAL`) + một `Action(action_id="investigate_manually")` —
**không có remediation tự động** (không có lệnh nào để "tự sửa" một node
đang quá tải), vận hành viên tự điều tra. Không tạo trùng nếu Incident cho
đúng host đó đang mở; tự chuyển `RESOLVED` khi host không còn bị gắn cờ ở
lượt quét sau.

**Chỉ gửi Telegram khi Incident MỚI được tạo** — không gửi lại ở mỗi lượt
quét trong lúc node vẫn đang cao, và không gửi gì khi tự phục hồi (cùng
posture "một thông báo cho một vấn đề mới" như mục 4.1).

### 5.4. Định dạng tin nhắn

```
🟠 Phần cứng node 10.0.0.5
Node 10.0.0.5 có CPU 95.2% / RAM 40.1% cao bất thường, lặp lại 2 lần quét
liên tiếp (ngưỡng 90% CPU hoặc 90% RAM) — có thể node đang quá tải hoặc có
tiến trình bất thường, không phải lỗi cấu hình có thể tự sửa.
```

## 6. Yêu cầu phê duyệt qua Telegram (`telegram_approval_requests_enabled`)

Mục thứ 4, khác hẳn 3 mục trên: đây là nơi Telegram thực sự **hành động
được** thay vì chỉ thông báo. Toàn bộ logic nằm trong
`dashboard/telegram_approval_bot.py`, chạy dưới dạng **2 luồng nền
(daemon thread)** khởi động một lần duy nhất khi Dashboard start
(`dashboard/app.py`'s lifespan → `telegram_approval_bot.start()`).

### 6.1. Vì sao đặt trong tiến trình Dashboard, không phải Worker/Watcher

Logic Duyệt/Từ chối thật sự cần 2 kiểm tra loại-trừ-lẫn-nhau (không được
duyệt hành động khác khi đang có nâng cấp cụm/cài patch dở dang) — 2 hàm
kiểm tra đó (`dashboard/routes/upgrade.py::is_cluster_upgrade_pending_or_approved`/
`is_cluster_upgrade_physically_running`, `dashboard/routes/patch.py::
is_patch_install_pending_or_approved`) vốn đã nằm trong `dashboard/routes/`
từ trước (dùng chung với việc ẩn/hiện nút "Duyệt" trên trang chủ). Watcher
và Worker **không bao giờ được phép import từ `dashboard/`** (nguyên tắc
phân lớp AD-3 trong toàn bộ codebase) — nên đặt tính năng này trong chính
Dashboard là cách duy nhất tái dùng các kiểm tra đó mà không phải di dời
chúng sang nơi khác.

### 6.2. Hai luồng nền làm gì

**Luồng 1 — `_notify_loop`** (chu kỳ ngắn, mặc định 10 giây —
`telegram_approval_scan_interval_seconds`, không có UI để chỉnh): quét
bảng `actions` tìm mọi dòng `status=PENDING_APPROVAL` và
`telegram_notified_at IS NULL` (chưa từng gửi lên Telegram) —
**bất kể nguồn gốc**: AI chẩn đoán sự cố thật, backup, phần cứng,
DeviceHealth (dự đoán ổ đĩa hỏng), hay chính vận hành viên tự bấm "Đề
xuất" trên Dashboard (Deploy Cluster, Restore volume, ...) — tất cả đều là
một dòng `Action(status=PENDING_APPROVAL)`, quét không phân biệt. Với mỗi
dòng mới, gửi một tin nhắn kèm 2 nút bấm (`send_telegram_message_with_keyboard`),
rồi ghi lại `telegram_message_id`/`telegram_notified_at` lên chính dòng đó
— đảm bảo không bao giờ gửi trùng, kể cả sau khi Dashboard restart (trạng
thái nằm trong DB, không phải bộ nhớ).

**Luồng 2 — `_listen_loop`** (Telegram Bot API `getUpdates`, long-polling
tới 30 giây/lần gọi): khi nhận được một `callback_query` (một lượt bấm
nút), sau khi qua kiểm tra CHAT_ID (mục 6.3), gọi thẳng
`dashboard/routes/actions.py::approve_action_core`/`reject_action_core` —
**đúng hàm** nút HTML "Duyệt"/"Từ chối" trên Dashboard gọi (refactor ra
2026-08-05 để dùng chung, không có bản sao thứ hai dễ lệch nhau), rồi sửa
lại tin nhắn gốc (xoá 2 nút, thêm dòng kết quả) và trả lời lượt bấm (toast
nhỏ "Đã duyệt"/"Đã từ chối"/lỗi nếu có).

### 6.3. TRUST MODEL — đọc kỹ trước khi bật

Quyền hạn ở đây hoàn toàn dựa vào **một điều kiện duy nhất: tin nhắn bấm
nút có đến từ đúng `settings.telegram_chat_id` đã cấu hình hay không** —
CÙNG danh tính đã quyết định "có gửi cảnh báo tới đây hay không" cho cả 3
mục kia. Không có khái niệm "tài khoản Telegram này ứng với vận hành viên
Dashboard nào" ở bất kỳ đâu trong codebase — nhật ký audit ghi lại
`actor="telegram:<username-hoặc-id>"`, không phải tên đăng nhập Dashboard
thật.

- Nếu Chat ID là **chat riêng 1-1** với một vận hành viên duy nhất: mức độ
  tin cậy tương đương chính tài khoản Telegram của người đó — ai chiếm được
  tài khoản Telegram đó cũng duyệt được, giống hệt ai có mật khẩu Dashboard
  cũng duyệt được.
- Nếu Chat ID là **một nhóm (group)** nhiều người: **MỌI thành viên trong
  nhóm đó** đều duyệt/từ chối được bất kỳ hành động RISKY nào đang chờ —
  kể cả một hành động họ không hề đề xuất, không hề biết ngữ cảnh đầy đủ
  (tin nhắn có gửi kèm `rationale`/lệnh xem trước, nhưng không đầy đủ bằng
  xem trực tiếp trên Dashboard).
- Một `callback_query` đến từ chat_id KHÁC bị **từ chối thẳng, ghi log
  cảnh báo**, không có gì được thực thi, và người bấm chỉ nhận toast
  "Không có quyền".

### 6.4. Idempotent / an toàn khi bấm trùng

- Bấm 2 lần (Telegram gửi callback trùng, hoặc vận hành viên bấm 2 nút
  gần nhau): lần thứ 2 luôn thấy `Action.status` đã khác
  `PENDING_APPROVAL`, trả về outcome `ALREADY_HANDLED` — không có gì bị
  thực thi lại lần thứ hai.
- Duyệt trên Dashboard trong lúc nút Telegram cho ĐÚNG Action đó vẫn còn
  hiển thị: nhấn nút Telegram sau đó cũng chỉ nhận `ALREADY_HANDLED`, tin
  nhắn được sửa lại phản ánh đúng thực tế, không có xung đột dữ liệu.
- Đang có nâng cấp cụm/cài patch dở dang mà bấm Duyệt cho một hành động
  KHÁC qua Telegram: nhận đúng thông báo từ chối (`ActionConflictError`)
  y hệt khi bấm trên Dashboard, `Action` giữ nguyên `PENDING_APPROVAL`,
  không có gì bị duyệt nhầm.

### 6.5. Nội dung tin nhắn

```
📋 Đề xuất chờ duyệt: restart_osd_daemon
osd.3 trên node2 nghi bị treo, đề xuất khởi động lại daemon

Lệnh xem trước:
docker restart ceph-osd-B

Action ID: 3f9c1a2e-...
[✅ Duyệt]  [❌ Từ chối]
```

Sau khi bấm, tin nhắn được sửa lại (giữ nguyên nội dung gốc, xoá 2 nút,
thêm một dòng): `✅ ĐÃ DUYỆT.` / `❌ ĐÃ TỪ CHỐI.` / `✅ Đã xác nhận (không
có lệnh tự động để chạy cho mục này).` (trường hợp `investigate_manually`
— không có Command tự động, giống hệt hành vi nút HTML) / cảnh báo nếu có
lỗi.

## 7. Vì sao không có "tin nhắn khi đã phục hồi"

Cả 3 mục thông báo thuần (Backup/Lỗi cụm/Phần cứng) đều **chỉ gửi khi một
vấn đề MỚI xuất hiện**, không gửi thông báo khi vấn đề tự hết — nhất quán
trên toàn hệ thống, không phải thiếu sót riêng một chỗ. Giữ phạm vi tính
năng gọn: vận hành viên xem trạng thái "đã RESOLVED chưa" trực tiếp trên
Dashboard (Incident/Backup history) khi cần xác nhận, thay vì nhân đôi số
lượng tin nhắn Telegram cho mỗi sự kiện. (Mục 4 "Yêu cầu phê duyệt" thì
khác — bản thân nó LÀ hành động, không phải một thông báo trạng thái, nên
không áp dụng lý do này.)

## 8. Các file liên quan trong mã nguồn

| File | Vai trò |
|---|---|
| `shared/telegram_client.py` | Client Telegram Bot API dùng chung — gửi thuần (`send_telegram_message`) VÀ 4 hàm cho mục Phê duyệt (`send_telegram_message_with_keyboard`/`edit_telegram_message`/`get_telegram_updates`/`answer_telegram_callback`) |
| `shared/telegram_alerts.py` | `send_incident_alert()`/`send_node_alert()` — mỗi hàm tự kiểm tra công tắc riêng |
| `worker/backup/alerting.py` | `send_alert()` — cảnh báo Backup (đã có từ trước), gọi Telegram + webhook |
| `watcher/main.py` | `build_and_publish_incident()` gọi `send_incident_alert()`; guard `NODE_RESOURCE_HIGH_PREFIX` trong `_resolve_recovered_incidents`; nhịp quét `node_health_monitor` trong `run()` |
| `watcher/node_health_monitor.py` | Toàn bộ logic quét CPU/RAM + ngưỡng + vòng đời Incident cho cảnh báo phần cứng |
| `watcher/node_metrics.py` | Thu thập CPU%/RAM% qua SSH (dùng lại nguyên vẹn, đã có sẵn cho trang Nodes) |
| `dashboard/telegram_approval_bot.py` | Toàn bộ logic mục Phê duyệt — 2 luồng nền, trust model, idempotent |
| `dashboard/routes/actions.py` | `approve_action_core`/`reject_action_core` — logic Duyệt/Từ chối DÙNG CHUNG giữa nút HTML và nút Telegram |
| `dashboard/app.py` | `lifespan` — khởi động 2 luồng nền của `telegram_approval_bot` một lần duy nhất |
| `dashboard/routes/settings.py` | `telegram_settings_submit`/`telegram_settings_test`/`telegram_help` — form 4 công tắc, chỉ admin |
| `dashboard/templates/settings.html` | Card "Cảnh báo Telegram" — 4 checkbox độc lập + lưu ý trust model cho mục Phê duyệt |
| `dashboard/templates/telegram_help.html` | Hướng dẫn tạo Bot/lấy Chat ID từng bước, trong app |
| `config/settings.py` | 4 công tắc + `node_health_scan_interval_seconds` + `telegram_approval_scan_interval_seconds` |
| `shared/env_config.py` | `TELEGRAM_ENV_NAMES` — ánh xạ field ↔ biến `.env` |
| `shared/models.py` | `Action.telegram_message_id`/`Action.telegram_notified_at` — trạng thái "đã gửi lên Telegram chưa" |
| `alembic/versions/38df461ab6d0_*.py` | Migration thêm 2 cột trên |
| `tests/test_telegram_client.py`, `tests/test_shared_telegram_alerts.py`, `tests/test_node_health_monitor.py`, `tests/test_watcher_main.py`, `tests/test_watcher_incident_flow.py`, `tests/test_telegram_approval_bot.py`, `tests/test_dashboard_actions.py`, `tests/test_dashboard_settings.py` | Test cho từng phần |
