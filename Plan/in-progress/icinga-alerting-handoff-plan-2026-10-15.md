# Kế hoạch: giao phần cảnh báo cho Icinga 2, Ceph AI tập trung chẩn đoán và sửa

**Ngày lập:** 15/10/2026
**Trạng thái:** `IN-PROGRESS`. I1 đang làm (16/10/2026): node Icinga 10.3.52.190 đã cài, CS-STAGING đã được check.
**Người quyết định:** chủ dự án (ý tưởng ngày 15/10/2026: "thay thế cảnh báo lỗi bằng Icinga, đỡ các phần cho AI").

## 1. Vì sao

Ceph AI đang tự làm trọn chuỗi **phát hiện → gửi cảnh báo → nhắc lại → digest**. Chuỗi này chiếm phần lớn
code ngoài AI và hay hỏng mà không ai biết. Ví dụ, ngày 06/10/2026 phát hiện hai hàm gửi thiếu tham số đã
làm rơi toàn bộ digest hằng tuần và 1.780 cảnh báo RGW từ 28/09 đến 06/10. Icinga 2 là công cụ đã chín
cho đúng phần việc này:
- lịch trực và leo thang;
- dependency và downtime khi bảo trì;
- chống flapping;
- ack;
- gửi qua Email, Slack hoặc Telegram;
- Icinga Web cho nhiều đội cùng xem.

Mục tiêu: **Icinga lo phát hiện và cảnh báo; Ceph AI nhận sự cố từ Icinga, chẩn đoán, đề xuất và chạy hành
động có duyệt.** Ceph AI không thay Icinga cho giám sát hạ tầng chung (OpenStack, máy chủ, mạng).

## 2. Chia việc sau khi xong

| Việc | Trước | Sau |
|---|---|---|
| Check định kỳ: `ceph health detail`, dung lượng OSD/pool, OSD down, PG | Watcher / remediation-watcher | **Icinga**, plugin chạy bằng user Ceph chỉ đọc |
| Ngưỡng, flapping, downtime, dependency (node chết → không báo từng OSD) | Ceph AI, một phần | **Icinga** |
| Gửi, nhắc lại, leo thang, ack | `shared/telegram_alerts.py`, `telegram_outbox.py`, trang Alert Telegram | **Icinga** |
| Mở incident, thu bằng chứng, chẩn đoán, đề xuất, duyệt, thực thi | Ceph AI | **Ceph AI** (giữ nguyên) |
| Dự báo dung lượng, phân tích log, slow ops, CRUSH, thay OSD | Ceph AI | **Ceph AI**. Kết quả dự báo đẩy sang Icinga thành một check |
| Chat với AI và duyệt hành động qua Telegram | Ceph AI | **Ceph AI** (giữ nguyên) |

## 3. Thiết kế tích hợp

### 3.1 Icinga → Ceph AI (sự cố vào)
- Trên Icinga: một `NotificationCommand` tên `ceph-ai-webhook`. Lệnh này gọi
  `POST https://<ceph-ai>/api/icinga/events`, kèm header `Authorization: Bearer <token>`.
  - Body JSON gồm: `host`, `service`, `state` (OK/WARNING/CRITICAL/UNKNOWN), `state_type` (HARD/SOFT),
    `notification_type` (PROBLEM/RECOVERY/ACKNOWLEDGEMENT/DOWNTIMESTART/…), `output`, `long_output`,
    `last_state_change` và `icinga_event_id`.
  - Chỉ gán command này cho các service Ceph, qua `assign where "ceph" in service.groups`.
- Trên Ceph AI, xử lý từng loại sự kiện:
  - xác thực token so sánh hằng thời gian, giới hạn tần suất, chỉ nhận JSON có schema;
  - map `host` sang cụm qua bảng cấu hình: tên host Icinga → `Cluster.id`;
  - **PROBLEM** (HARD): mở incident hoặc dùng lại incident đang mở theo `dedupe_key = icinga:<host>!<service>`.
    Sau đó Ceph AI **tự chạy `ceph health detail --format json`** để lấy mã lỗi cụ thể (OSD_DOWN,
    PG_DEGRADED…), chứ không dựa vào chuỗi output của plugin;
  - **RECOVERY**: đóng incident, hoặc chuyển sang chờ xác minh nếu đang có action;
  - **ACKNOWLEDGEMENT**: ghi sự kiện. Ceph AI không còn tự nhắc lại;
  - **DOWNTIMESTART / DOWNTIMEEND**: tạm ngừng đề xuất tự động cho cụm đó trong thời gian bảo trì;
  - SOFT state bị bỏ qua. Sự kiện trùng `icinga_event_id` cũng bị bỏ qua (idempotent).
- Webhook chỉ **mở incident**, không bao giờ chạy hành động. Hành động vẫn đi qua policy và duyệt như hiện nay.

### 3.2 Ceph AI → Icinga (kết quả chẩn đoán ra)
- User API Icinga riêng, chỉ có quyền `actions/add-comment` và `actions/process-check-result`
  (`ApiUser` với `permissions` giới hạn). Mật khẩu để trong file 0600, không để trong `.env` nếu chưa có
  khoá tương ứng trong image.
- Khi chẩn đoán xong: `add-comment` vào service đó, kèm tóm tắt nguyên nhân, đề xuất và link duyệt trên
  Dashboard. Người trực thấy chẩn đoán ngay trong Icinga Web và trong thông báo tiếp theo.
- Passive check `ceph-ai:<cụm>:forecast`: ngày đầy dự báo của pool hoặc OSD gần nhất. Ngưỡng
  WARNING < 30 ngày, CRITICAL < 7 ngày; perfdata là số ngày.
- Passive check `ceph-ai:heartbeat`: Ceph AI gửi 5 phút một lần. Icinga dùng `check_freshness` để báo khi
  Ceph AI ngừng gửi. Đây là phần **Icinga giám sát Ceph AI**, việc mà Ceph AI hiện tự làm qua selfcheck.

### 3.3 Check Ceph trên Icinga
- Dùng plugin có sẵn (ITL `ceph` / `check_ceph`) hoặc một plugin nhỏ trong repo
  (`scripts/icinga/check_ceph_ai_health`): đọc `ceph health detail --format json` và `ceph df`, trả về
  OK/WARN/CRIT kèm perfdata.
- **User Ceph chỉ đọc** cho Icinga, ví dụ `client.icinga` với quyền `mon 'allow r'` và `mgr 'allow r'`.
  Không dùng `client.admin`. Tạo user là việc trên cụm, cần operator duyệt.
- Mỗi cụm (CS-LAB, CS-STAGING, sau đó cụm của khách) là một host Icinga, với các service:
  - `ceph-health`;
  - `ceph-capacity`;
  - `ceph-osd`;
  - `ceph-pg`;
  - `ceph-mon-quorum`.
- Repo có mẫu cấu hình `deploy/icinga/ceph-ai.conf`, gồm host template, service, notification command và
  ApiUser, để operator áp dụng lên `icingamaster-v2`.

## 4. Các bước

### I1 — Nhận sự cố từ Icinga, chạy song song (không tắt gì)
- [x] Node Icinga **10.3.52.190** (`rnd-khiempx-icinga`, Ubuntu 24.04, 2 CPU / 3.8 GB): Icinga 2.16 + API, IcingaDB +
  Redis, MariaDB, Icinga Web 2 + IcingaDB Web (`http://10.3.52.190/icingaweb2`, user `admin`, mật khẩu trong
  `/root/.config/icinga-setup/` trên node). Cài bằng `deploy/icinga/install-icinga.sh` (chạy lại được).
- [x] Check Ceph: `deploy/icinga/check_ceph_cluster` (health/capacity/osd/pg/mon) với user Ceph chỉ đọc `client.icinga`
  (`mon 'allow r'`, `mgr 'allow r'`, đã tạo trên CS-STAGING và CS-LAB 16/10). Áp bằng `configure-ceph-ai.sh`.
  CS-STAGING chạy từ 16/10 (lượt đầu: HEALTH_OK, 3/3 OSD, 241/241 PG, quorum 3/3).
- [ ] **CS-LAB:** node Icinga không tới được mạng MON `10.20.1.0/24` (không có route). Chờ operator chọn: mở route
  (khuyên), `check_by_ssh` qua node CS-LAB, hoặc Icinga agent.
- [x] Route `POST /api/icinga/events` (token Bearer, schema chặt, idempotent theo `event_id`, ghi
  `/var/lib/ceph-ai/icinga-events.jsonl`, gắn với incident đang mở cùng mã; không mở incident, không chạy action).
- [ ] Bật webhook: token `/var/lib/ceph-ai/config/icinga-webhook.token` trên máy chính + `webhook.env` trên node
  Icinga, sau khi bản có route được deploy.
- [ ] Telegram cho Icinga: chờ operator chọn bot/chat (`/etc/icinga2/ceph-ai/telegram.env`).
- [ ] Thử: gây lỗi trên CS-STAGING (Failure Lab hoặc dừng một OSD) → Icinga PROBLEM → bản ghi có incident liên kết;
  RECOVERY ghi lại.

### I2 — Ghi chẩn đoán ngược về Icinga
- [ ] Client API Icinga (TLS, có xác minh CA của Icinga), `add-comment` sau khi chẩn đoán. Lỗi API không
  được làm hỏng luồng chẩn đoán: thử lại qua outbox.
- [ ] Passive check heartbeat và forecast.

### I3 — Đối chiếu 1–2 tuần (song song)
- [ ] Báo cáo hằng ngày: sự cố Icinga thấy mà Watcher không thấy, và ngược lại; độ trễ phát hiện của
  mỗi bên.
- [ ] Bên nào bỏ sót thì sửa check Icinga hoặc plugin, không sửa ngược Watcher.
- [ ] Tiêu chí qua I4: 7 ngày liên tiếp không có sự cố nào chỉ Watcher thấy.

### I4 — Tắt dần cảnh báo của Ceph AI (mỗi bước một PR, đảo ngược được bằng cấu hình)
- [ ] Cờ `ALERTING_OWNER=ceph-ai|icinga` cho từng cụm. Khi là `icinga`, Ceph AI không gửi cảnh báo, nhắc
  lại hay digest cho cụm đó. Tắt lần lượt CS-STAGING trước, rồi CS-LAB.
- [ ] Remediation-watcher không tự mở incident health-check cho cụm `icinga`; Watcher vẫn thu snapshot và log
  cho AI.
- [ ] Bỏ ngưỡng dung lượng cố định của Ceph AI cho cụm `icinga`; chỉ giữ dự báo.

### I5 — Dọn code (sau ít nhất 2 tuần ổn định ở I4)
Các phần có thể xoá hoặc thu gọn (số dòng ở `origin/main` ngày 15/10/2026):

| File | Dòng | Sau I5 |
|---|---|---|
| `shared/telegram_alerts.py` | 1807 | Xoá các sender cảnh báo; giữ phần Telegram cho chat và duyệt |
| `shared/telegram_outbox.py` | 831 | Thu gọn: chỉ còn tin duyệt hành động và trả lời chat |
| `dashboard/routes/telegram_alerts.py` + 3 template + 2 static | 966 + … | Xoá trang Alert Telegram |
| `watcher/rgw_alerting.py` | 695 | Chuyển các luật RGW thành check Icinga; giữ phần thu bằng chứng cho AI |
| `worker/ai_ops_digest.py` | 214 | Xoá, hoặc chuyển thành báo cáo trên Dashboard |
| `worker/backup/alerting.py`, `worker/backup/digest.py` | 308 + 114 | Thay bằng passive check `ceph-ai:backup` |
| `shared/notification_channels.py` | 88 | Xoá (Icinga lo Email/Slack) |
| `shared/vault_alerts.py` | 32 | Chuyển thành passive check |
| `shared/alert_lifecycle.py`, `shared/predictive_alert_lifecycle.py`, `dashboard/alert_center.py` | 36 + 57 + 83 | Xem lại: Alert Center có thể chỉ còn hiện sự cố đến từ Icinga |

**Giữ nguyên:**
- `shared/incident_outbox.py`: chuyển incident sang Worker qua RabbitMQ, không phải cảnh báo;
- luồng chẩn đoán, policy, duyệt, thực thi;
- dự báo, phân tích log;
- Telegram chat và duyệt.

## 5. Rủi ro và cách xử lý

| Rủi ro | Xử lý |
|---|---|
| Icinga chết thì không còn ai cảnh báo Ceph | Heartbeat hai chiều: Icinga thấy Ceph AI im lặng (`check_freshness`). Ceph AI giữ **một** cảnh báo khẩn qua Telegram khi không nhận được sự kiện hay check nào từ Icinga quá 30 phút |
| Plugin Icinga thô hơn Watcher (chỉ thấy chuỗi HEALTH_WARN) | Ceph AI tự lấy `ceph health detail` khi nhận sự cố; I3 đo sự cố bị bỏ sót trước khi tắt Watcher |
| Webhook bị lạm dụng để mở incident giả | Token riêng, chỉ từ IP của icingamaster, schema chặt, giới hạn tần suất; webhook không bao giờ chạy hành động |
| Mất lịch sử cảnh báo trong Ceph AI | Incident vẫn được ghi (nguồn `icinga`); lịch sử thông báo nằm ở Icinga (IDO/IcingaDB) |
| Khách tự cài (R8) có thể không có Icinga | `ALERTING_OWNER` mặc định là `ceph-ai`; chỉ code cảnh báo **ngừng được dùng** mới xoá ở I5. Trước I5 phải quyết định khách có cần cảnh báo tích hợp hay không |

## 6. Thông tin cần từ operator trước I1
- [ ] Máy `icingamaster-v2`: IP, bản Icinga 2 (và Icinga Web 2 / IcingaDB). API cổng 5665 đã bật chưa.
  Chứng chỉ CA của Icinga.
- [ ] Đường mạng: Icinga gọi được Dashboard Ceph AI (10.3.55.213:8000, hoặc qua reverse proxy HTTPS)
  không; Ceph AI gọi được API Icinga cổng 5665 không.
- [ ] Icinga đã có check Ceph cho CS-LAB và CS-STAGING chưa. Nếu có thì dùng plugin nào, user Ceph nào.
- [ ] Ai áp dụng cấu hình lên Icinga. Cấu hình quản lý bằng file hay qua Director.
- [ ] Kênh thông báo mong muốn trên Icinga (Telegram nhóm nào, email, lịch trực).

## 7. Nhật ký

| Ngày | Việc | Kết quả |
|---|---|---|
| 15/10/2026 | Lập kế hoạch theo ý tưởng của chủ dự án | Chờ thông tin Icinga (mục 6) |
| 16/10/2026 | I1: cài node Icinga 10.3.52.190, check CS-STAGING, route webhook | CS-LAB chờ route mạng; Telegram chờ chọn chat |
