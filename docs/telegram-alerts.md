# Alert Telegram — 3 kênh độc lập (Backup / Lỗi cụm / Phần cứng), Phê duyệt là mặc định

Tài liệu này mô tả toàn bộ hệ thống Telegram của ceph-aiops, trang riêng
**Alert Telegram** (`/telegram-alerts`, không còn nằm trong Settings).
2026-08-06: thiết kế lại từ "1 Bot Token/Chat ID dùng chung cho 4 mục" sang
**3 kênh Telegram hoàn toàn độc lập** — Backup, Lỗi cụm (cluster health),
Phần cứng (CPU/RAM node) — mỗi kênh có **Bot Token + Chat ID riêng**. Yêu
cầu phê duyệt (Duyệt/Từ chối) không còn là 1 mục thứ 4 cần bật riêng nữa —
nó là **năng lực mặc định của MỌI kênh đã cấu hình**: hễ một kênh có đủ Bot
Token + Chat ID, kênh đó nghiễm nhiên cũng nhận được nút Duyệt/Từ chối cho
mọi Action đang chờ, không cần bật thêm gì.

Hướng dẫn thao tác từng bước (tạo Bot, lấy Chat ID) nằm ngay trong app tại
**Alert Telegram → "hướng dẫn kết nối Telegram từng bước"**
(`/telegram-alerts/help`) — mục 1 dưới đây chép lại đầy đủ để dùng làm tài
liệu tham khảo ngoài app; các mục còn lại tập trung vào **thiết kế/hành
vi**.

**2026-08-07 — Telegram giờ là kênh CHÍNH cho cả chẩn đoán lẫn phê duyệt,
không chỉ cảnh báo thô:** hai thay đổi cùng ngày. (1) Tin nhắn phê duyệt
(mục 6.6) và tin nhắn kết quả tự động xử lý MỚI (mục 7,
`send_auto_remediation_alert`) giờ mang theo `Incident.diagnosis_text` —
lời giải thích gốc rễ đầy đủ của AI, trước đây CHỈ hiện trên Dashboard,
không hề ra tới Telegram. (2) Thẻ "Chờ duyệt — Risky Action" trên Dashboard
chính (`/`) giờ chỉ còn là phương án dự phòng khi CHƯA cấu hình kênh
Telegram nào (mục 6.7) — một khi có ít nhất 1 kênh, mọi cảnh báo/chẩn
đoán/phê duyệt đều chuyển hẳn sang Telegram, Dashboard chỉ còn xem lịch sử
(Incident Feed/Audit Trail).

**2026-08-07 — Tên cụm, phân biệt khi nhiều cụm cùng gửi vào 1 chat:**
`config/settings.py::cluster_name` (mục 1.6) — 1 field dùng chung cho cả 3
kênh, chèn vào ĐẦU mọi tin nhắn (cả 3 kênh + tin phê duyệt) khi có giá trị.
Để trống (mặc định) thì tin nhắn giữ nguyên như trước, không có gì đổi.

Nguồn: `config/settings.py`, `shared/env_config.py`,
`shared/telegram_client.py`, `shared/telegram_alerts.py`,
`worker/backup/alerting.py`, `watcher/main.py`, `watcher/node_health_monitor.py`,
`worker/llm/router_client.py`, `dashboard/telegram_approval_bot.py`,
`dashboard/routes/telegram_alerts.py`, `dashboard/routes/actions.py`,
`dashboard/routes/incidents.py`.

## 1. Cấu hình & quyền truy cập (từng bước)

Chỉ tài khoản có quyền **admin** (`auth.is_admin_user`) mới thấy link
"Alert Telegram" trên thanh nav và truy cập được `/telegram-alerts` —
tài khoản thường bị chặn (403) nếu gõ thẳng URL.

### 1.1. Bước 1 — Tạo Bot qua @BotFather (lặp lại cho mỗi kênh muốn bật)

1. Mở Telegram, tìm tài khoản `@BotFather` (tick xanh, chính chủ Telegram)
   và bắt đầu chat.
2. Gõ lệnh `/newbot` và gửi.
3. BotFather hỏi **tên hiển thị** cho bot (vd: `Ceph AIOps Backup`) — gõ
   rồi gửi.
4. BotFather hỏi tiếp **username** cho bot — duy nhất trên toàn Telegram,
   bắt buộc kết thúc bằng `bot` (vd: `ceph_aiops_backup_bot`).
5. BotFather trả về dòng "Use this token to access the HTTP API", ngay
   dưới là **Bot Token** (`123456789:AAExampleTokenHere-restOfIt`) — đây
   là giá trị điền vào ô "Bot Token" của đúng kênh đang cấu hình.

Có thể tạo **3 Bot riêng** (khuyến nghị — dễ phân biệt tin nhắn theo
nguồn) hoặc **dùng lại CÙNG 1 Bot cho cả 3 kênh** và chỉ đổi Chat ID (gửi
tới 3 nhóm/chat khác nhau, hoặc gộp về 1 nơi nếu dùng chung Chat ID luôn).
Giữ kín Bot Token — ai có token này gửi được tin nhắn nhân danh bot đó, và
(vì Phê duyệt là mặc định) đọc/đáp được cả nút Duyệt/Từ chối của kênh đó.

### 1.2. Bước 2 — Lấy Chat ID cho kênh đang cấu hình

**Cách A — Chat riêng với bot:** bấm Start/gửi 1 tin bất kỳ cho bot, rồi
mở `https://api.telegram.org/bot<TOKEN>/getUpdates`, tìm
`"chat":{"id": ...}` — số dương (vd `987654321`).

**Cách B — Nhóm/kênh:** thêm bot vào nhóm/kênh (kênh/channel phải thêm bot
làm **quản trị viên**), gửi 1 tin bất kỳ trong đó, rồi mở cùng địa chỉ
`getUpdates` — số **âm** (vd `-1001234567890`).

Không thấy gì trong `getUpdates`? Gửi thêm 1 tin rồi tải lại — thường do
chưa có tin nhắn nào sau khi thêm bot.

### 1.3. Bước 3 — Cấu hình trên `/telegram-alerts`

1. Vào **Alert Telegram** trên thanh nav (chỉ admin thấy).
2. Ở đúng card của kênh đang cấu hình (Cảnh báo Backup / Cảnh báo lỗi cụm
   / Cảnh báo phần cứng), dán Bot Token + Chat ID, bấm **Lưu**. Bỏ trống ô
   Bot Token khi Lưu = giữ nguyên token đã lưu (không phải xoá).
3. Kênh được coi là "đã cấu hình" ngay khi cả 2 ô đều có giá trị. Duyệt/Từ
   chối cũng áp dụng ngay cho kênh này, không cần bật thêm gì (xem mục 6).
4. Lặp lại cho từng kênh còn lại muốn bật — độc lập hoàn toàn.
5. Bấm **"Gửi thử"** ở đúng card — gửi bằng cấu hình **đã lưu** (không phải
   giá trị chưa lưu trên form), xác nhận Bot Token/Chat ID hoạt động. Gửi
   thử hoạt động cả khi kênh đang TẮT (mục 1.3b).

Lưu 1 kênh chỉ khởi động lại **đúng** tiến trình đọc kênh đó — Backup →
Worker; Lỗi cụm/Phần cứng → Watcher (không đụng tiến trình còn lại, khác
thiết kế cũ luôn restart cả Worker lẫn Watcher dù chỉ đổi 1 mục). Riêng
Duyệt/Từ chối không cần khởi động lại gì — 2 thread nền
(`dashboard/telegram_approval_bot.py`) đọc `settings` mới nhất ngay ở lượt
lặp kế tiếp, chạy sẵn trong tiến trình Dashboard.

### 1.3b. Bật/Tắt riêng từng kênh (không mất Bot Token/Chat ID)

2026-08-07: mỗi card có nút **"Tắt kênh này"/"Bật kênh này"** ở đầu card,
tách biệt hoàn toàn với form Lưu Bot Token/Chat ID — bấm Tắt chỉ lật cờ
`telegram_{backup,incident,node}_enabled` (mặc định `True`), KHÔNG đụng gì
tới token/chat id đã lưu, nên bật lại là hoạt động ngay, không cần dán lại
Chat ID. Nhãn trạng thái ngay cạnh tên kênh cho biết: **"Chưa cấu hình"**
(chưa có token/chat id) / **"Đang bật"** / **"Đã tắt"**.

Một kênh chỉ thực sự hoạt động khi **VỪA đã cấu hình VỪA đang bật** — tắt
một kênh dừng cả cảnh báo thường LẪN việc kênh đó nhận tin Duyệt/Từ chối
(mục 6.1), giống hệt như xoá token/chat id, chỉ khác là bật lại không cần
gõ lại gì. Cùng cơ chế khởi động lại đúng tiến trình như bước Lưu ở trên
(Backup → Worker; Lỗi cụm/Phần cứng → Watcher).

### 1.4. Cấu hình thay thế qua `.env`

9 field ánh xạ trực tiếp sang biến `.env` (`shared/env_config.py`):

| Kênh | Field (`config/settings.py`) | Biến `.env` |
|---|---|---|
| Backup | `telegram_backup_bot_token` / `telegram_backup_chat_id` / `telegram_backup_enabled` | `TELEGRAM_BACKUP_BOT_TOKEN` / `TELEGRAM_BACKUP_CHAT_ID` / `TELEGRAM_BACKUP_ENABLED` |
| Lỗi cụm | `telegram_incident_bot_token` / `telegram_incident_chat_id` / `telegram_incident_enabled` | `TELEGRAM_INCIDENT_BOT_TOKEN` / `TELEGRAM_INCIDENT_CHAT_ID` / `TELEGRAM_INCIDENT_ENABLED` |
| Phần cứng | `telegram_node_bot_token` / `telegram_node_chat_id` / `telegram_node_enabled` | `TELEGRAM_NODE_BOT_TOKEN` / `TELEGRAM_NODE_CHAT_ID` / `TELEGRAM_NODE_ENABLED` |

`*_enabled` nhận giá trị chuỗi `true`/`false` (cùng convention với
`ROUTER_ENABLED`), mặc định `true` nếu không set.

Sửa tay `.env` cho kết quả cấu hình tương đương với sửa qua UI, chỉ khác:
sửa tay **không** tự kích hoạt khởi động lại Worker/Watcher (pydantic-settings
chỉ đọc `.env` một lần lúc tiến trình khởi động) — phải tự restart tiến
trình liên quan sau khi sửa tay; qua UI thì bước Lưu đã tự làm việc đó.

### 1.5. Xử lý sự cố thường gặp

- **`getUpdates` trả về rỗng** — chưa gửi tin nhắn nào cho bot/nhóm sau
  khi thêm bot; gửi thêm 1 tin rồi tải lại.
- **"Gửi thử" báo lỗi "chat not found"** — Chat ID sai, hoặc bot chưa từng
  nhận tin nhắn nào trong đúng chat/nhóm đó (lặp lại mục 1.2).
- **"Gửi thử" báo lỗi "bot was blocked"/"kicked"** — bot bị chặn/xoá khỏi
  nhóm; thêm lại rồi thử lại.
- **"Gửi thử" báo lỗi "Unauthorized"** — Bot Token sai hoặc đã bị thu hồi
  (`/revoke` với BotFather); tạo token mới.
- **Đã cấu hình 1 kênh nhưng vẫn không thấy nút Duyệt/Từ chối** — kiểm tra
  đúng Chat ID đã lưu (không phải giá trị gõ nhầm trên form chưa Lưu);
  thread quét (`telegram_approval_scan_interval_seconds`, mặc định 10s)
  cần tối đa 1 chu kỳ để nhận Action mới hoặc kênh mới cấu hình.

### 1.6. Tên cụm — phân biệt khi nhiều cụm cùng gửi vào 1 chat

`config/settings.py::cluster_name` — MỘT field dùng chung cho cả 3 kênh
(không phải per-channel, vì một tiến trình ceph-aiops luôn chỉ theo dõi
đúng 1 cụm, nên chỉ có đúng 1 tên cần phân biệt). Hữu ích khi vận hành
**nhiều cụm Ceph, mỗi cụm 1 bộ ceph-aiops riêng**, nhưng cả mấy bộ đó lại
trỏ Bot Token/Chat ID về CÙNG một chat Telegram (gộp cảnh báo về 1 chỗ) —
không có tên cụm thì không phân biệt được tin nhắn nào của cụm nào.

Điền ở card **"Tên cụm"** trên `/telegram-alerts` (vd `cluster-hcm-01`),
bấm **Lưu** — chèn tự động vào ĐẦU mọi tin nhắn của cả 3 kênh bên trên, kể
cả tin nhắn phê duyệt (mục 6.6):

```
📍 Cụm: cluster-hcm-01
🔴 HEALTH_ERR Cụm Ceph: OSD_DOWN
osd.3 (root=default,host=node2) is down
```

Để trống (mặc định) = tin nhắn giữ nguyên như trước, không có gì đổi — an
toàn cho triển khai chỉ có 1 cụm. Khác với Bot Token/Chat ID của từng kênh
(chỉ restart ĐÚNG 1 tiến trình đọc kênh đó) — giá trị này được đọc bởi CẢ
Watcher (`shared/telegram_alerts.py`) LẪN Worker (`worker/backup/
alerting.py`), nên Lưu sẽ restart **cả hai**. `dashboard/
telegram_approval_bot.py` (tin phê duyệt) chạy ngay trong tiến trình
Dashboard nên áp dụng tức thì, không cần restart.

.env tương ứng: biến `CLUSTER_NAME` — sửa tay cũng phải tự restart Watcher
VÀ Worker (như mục 1.4 ở trên), không tự động như qua UI.

## 2. Nguyên tắc thiết kế chung

Cả 3 kênh (Backup/Lỗi cụm/Phần cứng) dùng chung một hàm gửi tin nhắn thuần
(`shared/telegram_client.py::send_telegram_message`) — gọi thẳng Telegram
Bot API `sendMessage`, không dùng `parse_mode` (tránh lỗi 400 nếu nội dung
động chứa ký tự `<`/`>`/`&`). Mọi lần gửi đều **best-effort** — lỗi gửi
(token sai, chưa thêm bot vào nhóm, mất mạng) chỉ log rồi bỏ qua, không bao
giờ làm hỏng lần backup/quét sức khoẻ/quét CPU-RAM đã kích hoạt nó.

**Phê duyệt (mục 6) không còn là "ngoại lệ của 1 mục riêng"** như thiết kế
cũ — nó là năng lực song song, tự động áp dụng lên CHÍNH các kênh này ngay
khi chúng được cấu hình. `dashboard/telegram_approval_bot.py` là nơi DUY
NHẤT trong codebase đọc phản hồi đến từ Telegram (long-polling
`getUpdates`); một callback đến từ đó CHỈ gọi lại đúng logic Duyệt/Từ chối
`dashboard/routes/actions.py` đã có sẵn cho nút trên Dashboard — không có
đường nào mới để thực thi thứ gì đó chưa từng thực thi được trước đây.

## 3. Cảnh báo Backup

Đã có từ trước (xem [ceph-backup.md](./ceph-backup.md), mục 7.2b) —
`worker/backup/alerting.py::send_alert()` là điểm gửi cảnh báo backup duy
nhất, tự động phủ:

- Backup thất bại (`FAILED`) hoặc quá hạn RPO 24h/chưa từng chạy
- RestoreDrill thất bại (`critical`)
- Bất thường (anomaly) mức `critical` do AI phát hiện (duration/size lệch
  bất thường so với lịch sử)
- BackupDigest tổng hợp hàng ngày (mức `info`)

Gửi song song với webhook chung đã có sẵn (`backup_alert_webhook_url`) —
hai kênh độc lập, không thay thế nhau. Dùng `telegram_backup_bot_token`/
`telegram_backup_chat_id` — kênh này không đụng gì tới Lỗi cụm/Phần cứng.

## 4. Cảnh báo lỗi cụm

Nguồn: `watcher/main.py::build_and_publish_incident` (điểm DUY NHẤT tạo
`Incident` cho một vấn đề Ceph **thật**, phát hiện qua `ceph health
detail`). Dùng `telegram_incident_bot_token`/`telegram_incident_chat_id`.

### 4.1. Khi nào gửi

Gửi **một tin nhắn cho mỗi check** ngay khi `Incident` tương ứng được ghi
vào DB — cùng độ chi tiết với cách Incident được tạo (một cụm có 2 vấn đề
cùng lúc, vd `MON_CLOCK_SKEW` + `OSD_DOWN`, nhận **2 tin nhắn riêng**, không
gộp thành một). Chỉ gửi khi **transition VÀO** `HEALTH_WARN`/`HEALTH_ERR`
— không gửi lại mỗi lần poll trong khi vấn đề vẫn còn treo, và không gửi gì
khi cụm hồi phục về `HEALTH_OK` (xem mục 8 vì sao).

### 4.2. Định dạng tin nhắn

```
🔴 HEALTH_ERR Cụm Ceph: OSD_DOWN
osd.3 (root=default,host=node2) is down
```

(🟡 cho `HEALTH_WARN`, 🔴 cho `HEALTH_ERR`) — phần thân là `log_excerpt` đã
thu thập cho đúng Incident đó, **cắt bớt ở 800 ký tự** nếu quá dài.

### 4.3. Phạm vi — chỉ vấn đề Ceph THẬT

Chỉ áp dụng cho `ceph_code` không có tiền tố (mã check thật của Ceph, vd
`MON_DOWN`, `OSD_DOWN`, `PG_DEGRADED`) — **không** bao gồm các họ Incident
tổng hợp khác đã có sẵn tiền tố riêng (`VOLUME_SATURATED:`,
`DEVICE_HEALTH_EVACUATE:`, `NODE_RESOURCE_HIGH:`, `CHAT_REQUEST`,
`CLUSTER_UPGRADE`) — những họ đó có vòng đời tạo/đóng riêng.

## 5. Cảnh báo phần cứng

Kênh này gộp 2 module độc lập, cùng dùng chung `telegram_node_bot_token`/
`telegram_node_chat_id`: `watcher/node_health_monitor.py` (CPU/RAM node,
mục 5.1-5.4 dưới đây) và `watcher/osd_latency_monitor.py` (OSD/đĩa chậm bất
thường, mục 5.5) — cùng là "phần cứng/tài nguyên vật lý xuống cấp", không
tách kênh riêng cho từng loại (xem mục 4 vì sao chỉ có đúng 3 kênh).

### 5.1. Cách quét

- Quét **mọi node đã cấu hình** (MON+MGR+OSD+RGW, gộp trùng —
  `shared/cluster_nodes.py::configured_nodes()`), lấy CPU%/RAM% qua
  `watcher/node_metrics.py::collect_node_metrics()`.
- Chạy trên **nhịp quét RIÊNG, chậm hơn nhiều** so với health-check chính
  (`node_health_scan_interval_seconds`, **mặc định 15 phút**).
- Một node lỗi SSH ở một lượt quét chỉ bỏ qua đúng node đó, không chặn
  việc quét các node còn lại.

### 5.2. Ngưỡng cảnh báo

| Hằng số (`watcher/node_health_monitor.py`) | Giá trị mặc định | Ý nghĩa |
|---|---|---|
| `CPU_ALERT_THRESHOLD_PERCENT` | 90% | CPU node ≥ ngưỡng này coi là cao |
| `MEM_ALERT_THRESHOLD_PERCENT` | 90% | RAM node ≥ ngưỡng này coi là cao |
| `CONSECUTIVE_SCANS_REQUIRED` | 2 | Phải cao liên tiếp bấy nhiêu lượt quét mới báo động |

CPU **hoặc** RAM vượt ngưỡng đã tính là "cao" ở một lượt quét; phải giữ
trạng thái "cao" **2 lượt quét liên tiếp** mới thực sự tạo Incident. Ngưỡng
cố định trong code, không có UI/`.env` để chỉnh.

### 5.3. Vòng đời Incident + hành động

Mỗi host bị gắn cờ tạo một `Incident` (`ceph_code =
"NODE_RESOURCE_HIGH:<host>"`, `PENDING_APPROVAL`) + một
`Action(action_id="investigate_manually")` — không có remediation tự
động. **Chỉ gửi Telegram khi Incident MỚI được tạo** — không gửi lại ở mỗi
lượt quét, không gửi gì khi tự phục hồi.

### 5.4. Định dạng tin nhắn

```
🟠 Phần cứng node 10.0.0.5
Node 10.0.0.5 có CPU 95.2% / RAM 40.1% cao bất thường, lặp lại 2 lần quét
liên tiếp (ngưỡng 90% CPU hoặc 90% RAM) — có thể node đang quá tải hoặc có
tiến trình bất thường, không phải lỗi cấu hình có thể tự sửa.
```

### 5.5. OSD Latency Outlier — `watcher/osd_latency_monitor.py`

2026-08-07: phát hiện một OSD cụ thể có commit latency cao bất thường **so
với chính các OSD khác trong cùng cụm tại thời điểm quét** — không phải
`ceph health detail` (OSD vẫn `up`/`in` bình thường, không kích hoạt
HEALTH_WARN/ERR nào) và không phải CPU/RAM node — một ổ đĩa/OSD chạy chậm
dần vẫn kéo tail latency (p99/p999) toàn cụm xuống vì mọi I/O đi qua PG có
OSD đó đều bị nghẽn theo.

**Cách quét**: `ceph osd perf` (1 lệnh JSON nhẹ qua MON, không SSH) lấy
`commit_latency_ms` mọi OSD đang `up`, tính **trung vị của chính cụm đó
ngay trong lượt quét** làm baseline — không lưu lịch sử/bảng metrics riêng,
không so với ngưỡng ms cố định (cụm lai HDD/SSD/NVMe có baseline rất khác
nhau, ngưỡng tuyệt đối sẽ sai). Chạy trên nhịp quét riêng, NHANH hơn nhiều
so với CPU/RAM node (`osd_latency_scan_interval_seconds`, **mặc định 60
giây**) vì đây chỉ là 1 JSON-RPC rẻ, và spike latency thường thoáng qua hơn
nhiều so với xu hướng CPU/RAM tăng dần.

| Hằng số (`watcher/osd_latency_monitor.py`) | Giá trị mặc định | Ý nghĩa |
|---|---|---|
| `OUTLIER_LATENCY_RATIO` | 3.0 | Latency ≥ bấy nhiêu lần trung vị cụm coi là cao |
| `MIN_ABSOLUTE_LATENCY_MS` | 5.0 ms | Ngưỡng sàn tuyệt đối — tránh báo động giả khi cả cụm đều cực nhanh |
| `MIN_UP_OSDS_FOR_COMPARISON` | 4 | Cụm ít hơn số OSD `up` này thì bỏ qua (trung vị không ổn định) |
| `CONSECUTIVE_SCANS_REQUIRED` | 2 | Phải cao liên tiếp bấy nhiêu lượt quét mới báo động |

Mỗi OSD bị gắn cờ tạo một `Incident` (`ceph_code =
"OSD_LATENCY_HIGH:<osd_id>"`, `PENDING_APPROVAL`) + một
`Action(action_id="investigate_manually")` — không có remediation tự
động (không rõ nguyên nhân: ổ hỏng dần, backfill/scrub đang chạy, noisy
neighbor...). Tự resolve khi OSD rớt khỏi danh sách lệch (latency về bình
thường, hoặc OSD không còn `up`). **Chỉ gửi Telegram khi Incident MỚI được
tạo**, cùng nguyên tắc như mục 5.1-5.4.

```
🟠 OSD chậm bất thường: osd.7 (node2)
osd.7 (node2) có commit_latency 42.0ms, cao gấp 4.2x so với trung vị cụm
(10.0ms), lặp lại 2 lần quét liên tiếp — có thể ổ đĩa/OSD này đang chậm
bất thường, ảnh hưởng tail latency toàn cụm, không phải lỗi cấu hình có
thể tự sửa.
```

## 6. Yêu cầu phê duyệt qua Telegram — mặc định của mọi kênh đã cấu hình

Khác 3 mục trên: đây là nơi Telegram thực sự **hành động được**. Toàn bộ
logic nằm trong `dashboard/telegram_approval_bot.py`, chạy dưới dạng
thread nền khởi động một lần duy nhất khi Dashboard start
(`dashboard/app.py`'s lifespan → `telegram_approval_bot.start()`).

### 6.1. Broadcast tới TẤT CẢ kênh đang cấu hình

Khi một `Action` chuyển sang `PENDING_APPROVAL`, luồng quét
`_notify_pending_actions` (chu kỳ `telegram_approval_scan_interval_seconds`,
mặc định 10s) gửi kèm nút "✅ Duyệt"/"❌ Từ chối" tới **MỌI kênh** hiện có
đủ Bot Token + Chat ID (1, 2 hoặc cả 3) — không phân biệt Action đó phát
sinh từ đâu (backup, sự cố cụm, phần cứng, hay đề xuất thủ công/AI khác
như Deploy/Upgrade/Patch/Restore Cluster). Duyệt/Từ chối ở BẤT KỲ kênh nào
trong số đó đều xử lý được toàn bộ.

Trạng thái "đã gửi kênh nào chưa" lưu trên `Action.telegram_message_ids`
(JSON `{channel_key: message_id}`, `shared/models.py`) — một kênh gửi lỗi
ở lượt trước tự được thử lại lượt sau (không chặn các kênh khác); một kênh
MỚI được cấu hình sau khi Action đã tồn tại vẫn tự nhận được broadcast ở
lượt quét kế tiếp, không cần Action mới hay khởi động lại gì.

### 6.2. Vì sao đặt trong tiến trình Dashboard, không phải Worker/Watcher

Logic Duyệt/Từ chối thật sự cần 2 kiểm tra loại-trừ-lẫn-nhau (không được
duyệt hành động khác khi đang có nâng cấp cụm/cài patch dở dang) —
`dashboard/routes/upgrade.py::is_cluster_upgrade_pending_or_approved`/
`is_cluster_upgrade_physically_running`, `dashboard/routes/patch.py::
is_patch_install_pending_or_approved` — vốn đã nằm trong `dashboard/routes/`
từ trước. Watcher và Worker **không bao giờ được phép import từ
`dashboard/`** (nguyên tắc phân lớp AD-3) — nên đặt tính năng này trong
chính Dashboard là cách duy nhất tái dùng các kiểm tra đó.

### 6.3. Lắng nghe callback — gom theo BOT TOKEN, không theo kênh

Telegram Bot API `getUpdates` (long-polling tới 30s/lần gọi) và cơ chế ack
`offset` của nó gắn với **1 bot token**, không phải 1 chat — nếu 2 kênh
dùng chung 1 Bot Token (khác Chat ID), chỉ được có **đúng 1** vòng
long-poll cho token đó (2 vòng poll song song trên cùng token sẽ giành
offset của nhau, mất/trùng update).

`_listen_supervisor_loop` (chu kỳ 5s) tính tập bot token đang được ít nhất
1 kênh sử dụng; với token mới → spawn 1 thread `_listen_loop_for_token`;
với token không còn kênh nào dùng → dừng thread đó (dừng hẳn sau vòng
long-poll hiện tại, tối đa ~30s sau). Khi nhận `callback_query` (một lượt
bấm nút), sau khi qua kiểm tra chat_id (mục 6.4), gọi thẳng
`dashboard/routes/actions.py::approve_action_core`/`reject_action_core` —
**đúng hàm** nút HTML "Duyệt"/"Từ chối" trên Dashboard gọi — rồi sửa lại
tin nhắn gốc (xoá 2 nút, thêm dòng kết quả) và trả lời lượt bấm.

### 6.4. TRUST MODEL — đọc kỹ trước khi điền Chat ID nào

Quyền hạn dựa vào **một điều kiện duy nhất: tin nhắn bấm nút có đến từ MỘT
TRONG SỐ các Chat ID đang được cấu hình ở bất kỳ kênh nào hay không** —
không phân biệt bấm từ kênh Backup, Lỗi cụm hay Phần cứng, tất cả đều
được coi ngang quyền để Duyệt/Từ chối BẤT KỲ Action nào đang chờ, không
riêng gì loại cảnh báo của đúng kênh đó. Không có khái niệm "tài khoản
Telegram này ứng với vận hành viên Dashboard nào" — nhật ký audit ghi lại
`actor="telegram:<username-hoặc-id>"`, không phải tên đăng nhập Dashboard
thật.

- Nếu một Chat ID là **chat riêng 1-1**: mức độ tin cậy tương đương chính
  tài khoản Telegram của người đó.
- Nếu một Chat ID là **nhóm nhiều người**: MỌI thành viên trong nhóm đó
  đều duyệt/từ chối được bất kỳ hành động RISKY nào đang chờ.
- Cấu hình cả 3 kênh nghĩa là tối đa 3 chat (hoặc ít hơn, nếu dùng chung
  Chat ID cho nhiều kênh) đều mang quyền này — không có cách "chỉ thông
  báo, không phê duyệt" cho một kênh cụ thể.
- Một `callback_query` đến từ chat_id KHÔNG nằm trong 3 kênh đã cấu hình
  bị **từ chối thẳng, ghi log cảnh báo**, người bấm chỉ nhận toast "Không
  có quyền".

### 6.5. Idempotent / an toàn khi bấm trùng

- Bấm 2 lần (ở cùng kênh hoặc 2 kênh khác nhau cho cùng 1 Action): lần thứ
  2 luôn thấy `Action.status` đã khác `PENDING_APPROVAL`, trả về outcome
  `ALREADY_HANDLED` — không có gì bị thực thi lại lần thứ hai.
- Duyệt trên Dashboard trong lúc nút Telegram (ở kênh nào đó) vẫn còn
  hiển thị: nhấn nút Telegram sau đó cũng chỉ nhận `ALREADY_HANDLED`, tin
  nhắn được sửa lại phản ánh đúng thực tế. Các kênh KHÁC mà Action này
  cũng được broadcast tới sẽ giữ nguyên nút cho tới khi ai đó bấm vào —
  lúc đó cũng nhận đúng `ALREADY_HANDLED` (không chủ động đồng bộ sửa cả 3
  bản tin cùng lúc khi quyết định đến từ Dashboard hoặc từ 1 kênh khác —
  giữ thiết kế đơn giản, tránh vòng import ngược
  `dashboard/routes/actions.py` → `telegram_approval_bot.py`).
- Đang có nâng cấp cụm/cài patch dở dang mà bấm Duyệt cho một hành động
  KHÁC qua Telegram: nhận đúng thông báo từ chối (`ActionConflictError`)
  y hệt khi bấm trên Dashboard, `Action` giữ nguyên `PENDING_APPROVAL`.

### 6.6. Nội dung tin nhắn

```
📋 Đề xuất chờ duyệt: restart_osd_daemon
Chẩn đoán: osd.3 (host node2) đã down và không tự lên lại sau 3 lần watcher poll liên tiếp, log cho thấy tiến trình bị treo chứ không phải bị OOM-kill hay crash — nghi daemon bị treo/deadlock.
Lý do chọn hành động: osd.3 trên node2 nghi bị treo, đề xuất khởi động lại daemon

Lệnh xem trước:
docker restart ceph-osd-B

Action ID: 3f9c1a2e-...
[✅ Duyệt]  [❌ Từ chối]
```

2026-08-07: dòng `Chẩn đoán:` (nếu có) là `Incident.diagnosis_text` — lời
giải thích gốc rễ đầy đủ của router (`worker/llm/router_client.py::
diagnose_incident`), CÙNG trường mà thẻ "Chờ duyệt" trên Dashboard vẫn ưu
tiên hiển thị. Trước ngày này, tin Telegram chỉ có dòng `Lý do chọn hành
động:` (`Action.rationale` — lý do CHỌN action_id, thường ngắn hơn nhiều)
— đây là lý do gửi lên Telegram "có LLM nhưng không thấy giải pháp": chẩn
đoán thật sự chỉ hiện trên Dashboard, chưa bao giờ ra tới Telegram. Dòng
`Lý do chọn hành động:` chỉ hiện thêm khi khác nội dung với `Chẩn đoán:`
(action không có diagnosis, vd envelope cũ) để tránh lặp lại y hệt.

Cùng nội dung được gửi giống hệt tới mọi kênh Action này broadcast tới —
không tuỳ biến theo kênh. Sau khi bấm ở bất kỳ kênh nào, bản tin của kênh
đó được sửa lại: `✅ ĐÃ DUYỆT.` / `❌ ĐÃ TỪ CHỐI.` / `✅ Đã xác nhận (không
có lệnh tự động để chạy cho mục này).` (trường hợp `investigate_manually`)
/ `⚠️ Đã được xử lý từ trước (có thể qua Dashboard hoặc kênh khác).`

### 6.7. Dashboard's own "Chờ duyệt" card — chỉ còn là phương án dự phòng

2026-08-07: trang Dashboard chính (`/`) chỉ còn hiển thị thẻ "Chờ duyệt —
Risky Action" (và nút Duyệt/Từ chối trực tiếp trên trang) khi **KHÔNG có
kênh Telegram nào đã cấu hình** (`dashboard/telegram_approval_bot.py::
has_configured_channel()` trả `False`) — vì mục 6.1 ở trên đã broadcast
đúng thẻ này (kèm nút bấm) tới mọi kênh đã cấu hình rồi, hiển thị lại lần
nữa trên Dashboard là dư thừa một khi Telegram đã làm việc đó. Ngay khi có
ít nhất 1 kênh đủ Bot Token + Chat ID, thẻ này biến mất khỏi trang chủ
(tab "Chờ duyệt" cũng không còn), tab mặc định đổi từ "Chờ duyệt" sang
"Incident Feed", và thẻ đó có 1 dòng ghi chú trỏ sang `/telegram-alerts`.
**Không xoá** route `/actions/{id}/approve|reject` hay logic
`approve_action_core`/`reject_action_core` — cả hai vẫn là những gì nút
Telegram gọi thẳng tới (mục 6.3); chỉ ẩn UI HTML khi có phương án khác.
Đây là lựa chọn có chủ đích để KHÔNG tái diễn lỗi đã từng xảy ra hồi
2026-07-23 (xem `tests/test_dashboard_actions.py::
test_index_shows_pending_action_card`'s docstring): thẻ này từng bị xoá
hẳn một lần với giả định "Chat-with-AI tự confirm là đủ", nhưng lúc đó
CHƯA có Telegram approval — kết quả là một RISKY Action confirm qua Chat
kẹt vĩnh viễn ở `PENDING_APPROVAL`, không có đường nào duyệt/từ chối. Bây
giờ Telegram approval đã tồn tại và thay thế đúng vai trò đó, nên ẩn thẻ
Dashboard là an toàn — NHƯNG chỉ khi Telegram thực sự đã cấu hình; nếu ai
đó xoá hết 3 cặp Bot Token/Chat ID sau này, thẻ tự động hiện lại ngay lần
tải trang kế tiếp, không cần thao tác gì thêm.

## 7. Vì sao không có "tin nhắn khi đã phục hồi" — và tin nhắn kết quả tự động xử lý

Cả 3 kênh thông báo thuần (Backup/Lỗi cụm/Phần cứng) đều **chỉ gửi khi một
vấn đề MỚI xuất hiện**, không gửi thông báo khi vấn đề tự hết — nhất quán
trên toàn hệ thống. Vận hành viên xem trạng thái "đã RESOLVED chưa" trực
tiếp trên Dashboard khi cần xác nhận. (Mục 6 "Yêu cầu phê duyệt" thì khác
— bản thân nó LÀ hành động, không phải một thông báo trạng thái.)

**2026-08-07 — `send_auto_remediation_alert()` (`shared/telegram_alerts.py`),
gọi từ `worker/llm/router_client.py::_record_execution_result`**: đây LÀ
một thông báo kết quả, cố ý khác nguyên tắc "chỉ gửi khi vấn đề mới xuất
hiện" ở trên, vì lý do riêng — `send_incident_alert()` (mục 4) gửi ngay
lúc `Incident` vừa được tạo, TRƯỚC KHI router kịp chẩn đoán bất cứ điều gì
(chẩn đoán chạy bất đồng bộ qua RabbitMQ, sau đó), nên tin nhắn đầu tiên
đó không bao giờ có thể chứa "giải pháp". Với mọi Action được xếp loại
`SAFE` (tự động chạy, không cần duyệt), sau khi thực thi xong (thành công
hay thất bại) gửi thêm đúng 1 tin lên kênh Lỗi cụm:

```
✅ Đã tự động xử lý: OSD_DOWN
Chẩn đoán: osd.3 (host node2) đã down và không tự lên lại sau 3 lần watcher poll liên tiếp...
Hành động: khởi động lại daemon osd.3
Lệnh đã chạy:
docker restart ceph-osd-B
```

(`❌ Tự động xử lý thất bại` khi `succeeded=False`.) Dùng lại kênh
`telegram_incident_bot_token`/`telegram_incident_chat_id` (không thêm kênh
thứ 4) — cùng cách `send_osd_latency_alert` dùng lại kênh Phần cứng. Chạy
SAU khi session DB đã commit/đóng (không giữ kết nối DB trong lúc gọi
mạng, cùng nguyên tắc `watcher/main.py::build_and_publish_incident` đã có)
và best-effort như mọi hàm khác trong `shared/telegram_alerts.py` — một
Action SAFE đã thực thi xong không bao giờ bị coi là thất bại chỉ vì gửi
Telegram lỗi. Không áp dụng cho Action RISKY (những Action đó đã có tin
nhắn phê duyệt riêng ở mục 6, và một khi được duyệt/từ chối, kết quả hiện
ngay trên chính tin nhắn đó qua `_OUTCOME_EDIT_SUFFIX` — không cần thêm
tin thứ 2).

## 8. Các file liên quan trong mã nguồn

| File | Vai trò |
|---|---|
| `config/settings.py` | 9 field cấu hình theo kênh (`telegram_{backup,incident,node}_{bot_token,chat_id,enabled}`) + `telegram_approval_scan_interval_seconds` + `cluster_name` (mục 1.6, dùng chung cả 3 kênh) |
| `shared/env_config.py` | `TELEGRAM_BACKUP_ENV_NAMES`/`TELEGRAM_INCIDENT_ENV_NAMES`/`TELEGRAM_NODE_ENV_NAMES` — ánh xạ field ↔ biến `.env`, mỗi kênh 1 dict (`CLUSTER_NAME_ENV_NAME` của `cluster_name` khai báo cục bộ trong `dashboard/routes/telegram_alerts.py`, không ở đây) |
| `shared/telegram_client.py` | Client Telegram Bot API dùng chung — gửi thuần (`send_telegram_message`) VÀ 4 hàm cho Phê duyệt (`send_telegram_message_with_keyboard`/`edit_telegram_message`/`get_telegram_updates`/`answer_telegram_callback`) |
| `shared/telegram_alerts.py` | `send_incident_alert()`/`send_node_alert()`/`send_osd_latency_alert()`/`send_auto_remediation_alert()` — mỗi hàm dùng đúng cặp token/chat_id của kênh mình (`send_osd_latency_alert` + `send_node_alert` chia sẻ kênh Phần cứng; `send_auto_remediation_alert` dùng lại kênh Lỗi cụm); `_with_cluster_prefix()` chèn `cluster_name` (mục 1.6) vào mọi tin qua `_send()` |
| `worker/backup/alerting.py` | `send_alert()` → `_send_telegram_alert()` dùng cặp token/chat_id kênh Backup, tự chèn `cluster_name` (bản sao logic riêng, không import `shared/telegram_alerts.py` qua ranh giới worker/watcher) |
| `watcher/main.py` | `build_and_publish_incident()` gọi `send_incident_alert()`; nhịp quét `node_health_monitor`/`osd_latency_monitor` trong `run()` |
| `watcher/node_health_monitor.py` | Toàn bộ logic quét CPU/RAM + ngưỡng + vòng đời Incident cho cảnh báo phần cứng (node) |
| `watcher/osd_latency_monitor.py` | Toàn bộ logic quét `ceph osd perf` + so trung vị cụm + vòng đời Incident cho OSD latency outlier |
| `worker/llm/router_client.py` | `_record_execution_result()` gọi `send_auto_remediation_alert()` sau khi một Action SAFE thực thi xong (mục 7) |
| `dashboard/telegram_approval_bot.py` | Broadcast tới mọi kênh đã cấu hình (kèm `Incident.diagnosis_text`, mục 6.6, và `cluster_name` nếu có, mục 1.6) + listener gom theo bot token + trust model + idempotent + `has_configured_channel()` (mục 6.7) |
| `dashboard/routes/actions.py` | `approve_action_core`/`reject_action_core` — logic Duyệt/Từ chối DÙNG CHUNG giữa nút HTML và nút Telegram |
| `dashboard/routes/incidents.py` | `index()` — ẩn thẻ "Chờ duyệt" trên Dashboard khi `has_configured_channel()` trả `True` (mục 6.7) |
| `dashboard/routes/telegram_alerts.py` | Router trang "Alert Telegram" — 3 card kênh (mỗi kênh 1 route Lưu + 1 route Bật/Tắt, mục 1.3b + 1 route Gửi thử) + 1 card/route Lưu `cluster_name` (mục 1.6, restart cả Watcher lẫn Worker), route hướng dẫn |
| `dashboard/templates/telegram_alerts.html` | Trang chính — card "Tên cụm" + 3 card Backup/Lỗi cụm/Phần cứng, mỗi card có nút Bật/Tắt riêng + nhãn trạng thái |
| `dashboard/templates/telegram_alerts_help.html` | Hướng dẫn tạo Bot/lấy Chat ID từng bước, trong app |
| `dashboard/app.py` | `lifespan` — khởi động thread nền của `telegram_approval_bot`; đăng ký `telegram_alerts.router` |
| `shared/models.py` | `Action.telegram_message_ids` (JSON `{channel_key: message_id}`) / `Action.telegram_notified_at` |
| `alembic/versions/6b1f3a9d7e2c_*.py` | Migration đổi `telegram_message_id` (Integer) → `telegram_message_ids` (Text/JSON) |
| `tests/test_telegram_client.py`, `tests/test_shared_telegram_alerts.py`, `tests/test_node_health_monitor.py`, `tests/test_osd_latency_monitor.py`, `tests/test_watcher_incident_flow.py`, `tests/test_telegram_approval_bot.py`, `tests/test_backup_alerting.py`, `tests/test_dashboard_telegram_alerts.py` | Test cho từng phần |
