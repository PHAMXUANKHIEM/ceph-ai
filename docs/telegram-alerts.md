# Cảnh báo Telegram — 3 loại độc lập (Backup / Lỗi cụm / Phần cứng)

Tài liệu này mô tả toàn bộ hệ thống cảnh báo qua Telegram của ceph-aiops:
**một** Bot Token/Chat ID dùng chung, nhưng **ba loại cảnh báo bật/tắt hoàn
toàn độc lập** — Backup, Lỗi cụm (cluster health), và Phần cứng (CPU/RAM
node). Tắt một loại không ảnh hưởng tới hai loại còn lại.

Hướng dẫn thao tác từng bước (tạo Bot, lấy Chat ID) nằm ngay trong app tại
**Settings → Cảnh báo Telegram → "hướng dẫn kết nối Telegram từng bước"**
(`/settings/telegram/help`) — tài liệu này tập trung vào **thiết kế/hành
vi**, không lặp lại phần thao tác click-by-click đã có trong app.

Nguồn: `shared/telegram_client.py`, `shared/telegram_alerts.py`,
`worker/backup/alerting.py`, `watcher/main.py`, `watcher/node_health_monitor.py`,
`dashboard/routes/settings.py`, `config/settings.py`.

## 1. Nguyên tắc thiết kế chung — MỘT CHIỀU, không có gì thực thi được từ Telegram

Cả 3 loại cảnh báo dùng chung một client gửi tin nhắn
(`shared/telegram_client.py::send_telegram_message`) — gọi thẳng Telegram
Bot API `sendMessage`, không dùng `parse_mode` (tránh lỗi 400 nếu nội dung
động chứa ký tự `<`/`>`/`&`).

**Cố tình chỉ gửi ra, không bao giờ nhận vào:** ceph-aiops không chạy bot
lắng nghe tin nhắn (không long-polling, không webhook nhận). Một tin nhắn
gửi từ hệ thống này thuần tuý mang tính thông báo — không có phản hồi nào
được đọc lại, không có lệnh nào gõ trên Telegram có thể chạm tới
`worker/executor/`. **Mọi hành động khắc phục vẫn luôn phải đi qua đúng quy
trình đề xuất → duyệt trên Dashboard** (`dashboard/routes/actions.py`),
không có ngoại lệ, dù bật bao nhiêu loại cảnh báo.

Mọi lần gửi đều **best-effort** — một lỗi gửi (token sai, chưa thêm bot vào
nhóm, mất mạng) chỉ được log rồi bỏ qua, không bao giờ làm hỏng lần
backup/quét sức khoẻ/quét CPU-RAM đã kích hoạt nó.

## 2. Một cấu hình, ba công tắc

Cả 3 loại dùng **chung một cặp Bot Token/Chat ID** — cấu hình một lần tại
Settings → **Cảnh báo Telegram** (chỉ admin thấy/sửa), 3 checkbox độc lập:

| Công tắc (`config/settings.py`) | Đọc bởi | Chạy trong tiến trình |
|---|---|---|
| `telegram_alerts_enabled` | `worker/backup/alerting.py::send_alert` | **Worker** |
| `telegram_incident_alerts_enabled` | `shared/telegram_alerts.py::send_incident_alert`, gọi từ `watcher/main.py` | **Watcher** |
| `telegram_node_alerts_enabled` | `shared/telegram_alerts.py::send_node_alert`, gọi từ `watcher/node_health_monitor.py` | **Watcher** |

Vì cấu hình này được đọc bởi **cả hai tiến trình dài hạn** (Worker lẫn
Watcher, mỗi tiến trình chỉ đọc `.env` một lần lúc khởi động), form Lưu
trên Settings **khởi động lại cả Worker lẫn Watcher** — khác với hầu hết
form khác trên trang Settings chỉ cần khởi động lại một trong hai.

Bật một công tắc mà Bot Token/Chat ID còn trống → bị từ chối ngay khi Lưu
("Cần điền đủ Bot token và Chat ID trước khi bật bất kỳ loại cảnh báo
Telegram nào") — áp dụng chung cho cả 3 công tắc, không riêng công tắc
Backup.

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

## 6. Vì sao không có "tin nhắn khi đã phục hồi"

Cả 3 loại cảnh báo (Backup/Lỗi cụm/Phần cứng) đều **chỉ gửi khi một vấn đề
MỚI xuất hiện**, không gửi thông báo khi vấn đề tự hết — nhất quán trên
toàn hệ thống, không phải thiếu sót riêng một chỗ. Giữ phạm vi tính năng
gọn: vận hành viên xem trạng thái "đã RESOLVED chưa" trực tiếp trên
Dashboard (Incident/Backup history) khi cần xác nhận, thay vì nhân đôi số
lượng tin nhắn Telegram cho mỗi sự kiện.

## 7. Các file liên quan trong mã nguồn

| File | Vai trò |
|---|---|
| `shared/telegram_client.py` | Client Telegram Bot API dùng chung — `send_telegram_message()`, gửi-một-chiều |
| `shared/telegram_alerts.py` | `send_incident_alert()`/`send_node_alert()` — mỗi hàm tự kiểm tra công tắc riêng |
| `worker/backup/alerting.py` | `send_alert()` — cảnh báo Backup (đã có từ trước), gọi Telegram + webhook |
| `watcher/main.py` | `build_and_publish_incident()` gọi `send_incident_alert()`; guard `NODE_RESOURCE_HIGH_PREFIX` trong `_resolve_recovered_incidents`; nhịp quét `node_health_monitor` trong `run()` |
| `watcher/node_health_monitor.py` | Toàn bộ logic quét CPU/RAM + ngưỡng + vòng đời Incident cho cảnh báo phần cứng |
| `watcher/node_metrics.py` | Thu thập CPU%/RAM% qua SSH (dùng lại nguyên vẹn, đã có sẵn cho trang Nodes) |
| `dashboard/routes/settings.py` | `telegram_settings_submit`/`telegram_settings_test`/`telegram_help` — form 3 công tắc, chỉ admin |
| `dashboard/templates/settings.html` | Card "Cảnh báo Telegram" — 3 checkbox độc lập |
| `dashboard/templates/telegram_help.html` | Hướng dẫn tạo Bot/lấy Chat ID từng bước, trong app |
| `config/settings.py` | 3 công tắc + `node_health_scan_interval_seconds` |
| `shared/env_config.py` | `TELEGRAM_ENV_NAMES` — ánh xạ field ↔ biến `.env` |
| `tests/test_shared_telegram_alerts.py`, `tests/test_node_health_monitor.py`, `tests/test_watcher_main.py`, `tests/test_watcher_incident_flow.py`, `tests/test_dashboard_settings.py` | Test cho từng phần |
