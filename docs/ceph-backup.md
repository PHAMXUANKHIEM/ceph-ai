# Ceph Backup & Disaster Recovery trong ceph-aiops

Tài liệu này mô tả toàn bộ hệ thống Backup & DR của ceph-aiops (Epic 9):
sao lưu RBD image + metadata cụm, lưu trữ 2 bản sao độc lập, chính sách giữ
bản (retention), giám sát/cảnh báo bằng AI, và 2 luồng khôi phục (khôi phục
một volume đè lên production, và khôi phục toàn bộ cụm sau thảm hoạ).

Xem thêm **[runbook-dr.md](./runbook-dr.md)** — hướng dẫn thao tác từng
bước khi cần khôi phục cả cụm sau khi cụm cũ đã sập hoàn toàn.

Nguồn: `worker/backup/` (toàn bộ), `dashboard/routes/backups.py`,
`dashboard/routes/restore_cluster.py`, `worker/policy/backup_policy.yaml`,
`worker/policy/action_policy.yaml`, `shared/models.py`
(`BackupJob`/`BackupAnomaly`/`BackupDigestLog`), `config/settings.py`.

## 1. Kiến trúc tổng quan

Giống `worker/executor/cluster_deploy.py` (dựng/xoá/chuyển đổi cụm) và
`worker/executor/volume_perf.py` (đo hiệu năng), Backup là **một orchestrator
riêng** (`worker/backup/engine.py`), không đi qua vòng lặp lệnh theo host
chung — vì logic của nó (stream export qua SSH, upload lên nhiều đích, kiểm
tra checksum, quét retention) không phải kiểu "một lệnh trên nhiều host".

Điểm khác biệt lớn nhất so với 2 family kia: phần lớn hành động Backup
**không cần vận hành viên bấm duyệt** — chúng tự chạy theo lịch cron
(`worker/backup/scheduler.py`, chạy như coroutine thứ 3 trong
`worker/main.py`, dùng chung DB/engine với ứng dụng qua
`APScheduler.SQLAlchemyJobStore`, nên lịch không mất khi Worker restart).
Chỉ 2 hành động thật sự nguy hiểm (ghi đè dữ liệu thật) mới luôn cần duyệt
thủ công.

```
worker/backup/
├── engine.py         Orchestrator chính — dispatch theo action_id
├── scheduler.py       Lịch cron (APScheduler) — tự tạo Action đã APPROVED
├── metadata.py         Backup monmap/osdmap/crushmap/auth/config
├── restore.py           Restore full+diff chain dùng chung (Story 9.7)
├── restore_drill.py    Diễn tập khôi phục định kỳ (chỉ full, ra scratch)
├── anomaly.py            Phát hiện bất thường (thống kê, không AI)
├── ai_analysis.py       Phân tích nguyên nhân lỗi/bất thường bằng AI
├── alerting.py            Cảnh báo qua webhook
├── digest.py                Tổng hợp định kỳ (AI tóm tắt)
├── policy_config.py     Đọc worker/policy/backup_policy.yaml
└── storage/
    ├── base.py            Protocol BackupStorageBackend (interface chung)
    ├── ssh_backend.py    Đích SFTP (site 2 tự quản lý)
    ├── s3_backend.py      Đích S3/MinIO (hỗ trợ Object Lock — immutable)
    └── factory.py           Nơi DUY NHẤT rẽ nhánh theo transport
```

## 2. Sao lưu RBD Image (`rbd_backup_run`)

### 2.1. Full vs Incremental — quyết định tự động

Mỗi lần chạy (theo lịch hoặc thủ công), `engine.py::_run_rbd_backup` tự
quyết định kiểu bản backup:

- **Full** nếu: chưa từng có bản full nào thành công cho `(pool, image)`
  này, **hoặc** bản full gần nhất đã cũ hơn `full_refresh_every_n_days`
  (cấu hình per-image trong `backup_policy.yaml`, mặc định không giới hạn —
  chuỗi incremental có thể dài vô hạn nếu không đặt).
- **Incremental** nếu ngược lại — dựa trên (`base_job`) là bản full gần nhất.

Cơ chế **idempotent**: nếu đã có một `BackupJob` cho đúng `(pool, image)`
này đang `RUNNING` và còn "tươi" (mới tạo trong vòng
`STALE_RUNNING_TIMEOUT_SECONDS = 3600` giây), lần trigger mới **bỏ qua**,
không chạy chồng lên. Nếu bản `RUNNING` đó đã quá 1 giờ — coi như Worker đã
crash giữa chừng, tự đánh dấu `FAILED` ("Presumed crashed") rồi mới bắt đầu
job mới.

### 2.2. Quy trình export

1. `rbd snap create {pool}/{image}@backup-<timestamp>` — chụp snapshot nhất
   quán tại một thời điểm.
2. `rbd info` lấy kích thước image (dùng để tính % / ETA khi upload).
3. Tuỳ loại:
   - Full: `rbd export {pool}/{image}@<snap> -`
   - Incremental: `rbd export-diff {pool}/{image}@<snap> --from-snap <snap-của-full-gốc> -`
4. Lệnh chạy qua một **phiên SSH thô** (`paramiko`, không qua
   `worker/executor/ssh_executor.py::execute_command` — hàm đó buffer toàn
   bộ stdout vào RAM, không phù hợp với một export RBD có thể nhiều GB).
   Dữ liệu được **stream theo chunk 4MiB**, vừa ghi ra file tạm cục bộ trên
   Worker, vừa tính **SHA256** và cập nhật tiến độ (%, tốc độ MB/s, ETA)
   theo thời gian thực — ghi tiến độ tối đa mỗi 3 giây một lần (không spam
   DB ở tần suất cao hơn cần thiết).
5. File tạm sau đó được **upload lần lượt tới từng đích đã cấu hình** (xem
   mục 4) — mỗi đích nhận đúng cùng file, mỗi lần upload xong đều gọi
   `backend.verify()` để xác nhận kích thước + SHA256 khớp; **thất bại
   verify() coi như thất bại cả job**, không âm thầm bỏ qua.
6. Mỗi đích thành công tạo **một dòng `BackupJob` riêng** (cùng `run_id`,
   khác `backup_target_slot`) — một lần export sinh ra nhiều dòng nếu có
   nhiều đích, không phải một dòng "đã ghi N nơi".

### 2.3. Tại sao cả full lẫn incremental đều cần

Full là bản độc lập, khôi phục được ngay không cần gì khác. Incremental chỉ
chứa phần đổi khác so với full gốc (`base_job_id` trỏ về), nên khôi phục
một incremental **luôn cần cả chuỗi**: full gốc + mọi incremental xen giữa
theo đúng thứ tự tạo (xem mục 7).

## 3. Retention — giữ bản, xoá bản cũ

Sau **mỗi lần backup thành công**, hệ thống tự quét dọn
(`_sweep_retention_after_success`), và cũng có thể chạy thủ công/theo yêu
cầu qua `retention_sweep_delete`.

Cấu hình trong `backup_policy.yaml`:

```yaml
retention:
  keep_full_count: 3
  keep_incremental_count: 7
```

Điểm quan trọng cần hiểu đúng:

- **full và incremental được quét RIÊNG, với 2 số đếm riêng** — không phải
  một tổng chung. Một pool có thể giữ 3 bản full + 7 bản incremental cùng
  lúc, không phải "giữ 7 bản mới nhất bất kể loại".
- **Không bao giờ xoá một bản full mà một incremental ĐANG ĐƯỢC GIỮ còn phụ
  thuộc vào**, kể cả khi bản full đó đã "già" hơn `keep_full_count` theo
  tuổi (`_protected_keys` quét mọi incremental `SUCCESS` hiện có, gom
  `remote_key` của `base_job` tương ứng vào tập được bảo vệ — quét retention
  của full và incremental xảy ra trong cùng một lần gọi, nên tập bảo vệ này
  luôn phản ánh trạng thái TRƯỚC khi bất kỳ incremental nào bị xoá ở vòng
  quét đó).
- Quét theo **từng đích lưu trữ riêng** (`backend.apply_retention`) — một
  đích lỗi khi quét (vd. mất kết nối SSH) không chặn việc quét đích còn lại,
  chỉ log lỗi rồi tiếp tục.
- Mỗi object thực sự bị xoá đều được ghi vào Audit Trail
  (`EVENT_BACKUP_RETENTION_DELETE`).
- Ở tầng storage backend, `apply_retention()` sắp xếp object theo
  `created_at` giảm dần, loại các key nằm trong `protected_keys`, rồi xoá
  mọi thứ còn lại vượt quá `keep_count`.

## 4. Metadata cụm (`backup_metadata_run`)

Sao lưu **5 artifact** — nhẹ hơn nhiều so với RBD export (vài KB–MB, không
cần stream theo chunk như mục 2):

| Artifact | Lệnh |
|---|---|
| `monmap.bin` | `ceph mon getmap -o -` |
| `osdmap.bin` | `ceph osd getmap -o -` |
| `crushmap.bin` | `ceph osd getcrushmap -o -` |
| `auth_export.txt` | `ceph auth export` |
| `config_dump.json` | `ceph config dump` |

Cả 5 artifact của một lần chạy được lưu chung dưới một prefix
`metadata/<timestamp>/`, upload lên **mọi đích đã cấu hình** — mỗi đích một
dòng `BackupJob(job_type="metadata")` với `remote_key` là prefix đó (không
phải một key file đơn lẻ như RBD). Chạy theo lịch riêng, **thường xuyên hơn**
RBD backup (mặc định mỗi 6 giờ so với 1 lần/ngày) vì chi phí rẻ.

Đây chính là nguồn dữ liệu bước 11 của luồng khôi phục toàn cụm (mục 7.2 và
runbook-dr.md) dùng để khôi phục auth/CRUSH map/monmap.

## 5. Lưu trữ — 2 đích độc lập, có thể immutable

Kiến trúc cố tình dùng **2 slot cố định `a`/`b`** khai báo qua biến môi
trường (`config/settings.py`), **không phải bảng DB động** — quyết định này
xuất phát từ việc `Settings` trong dự án cấm field ngoài danh sách khai báo
sẵn (`extra="forbid"`), nên một scheme "N đích tuỳ ý" sẽ phá vỡ ràng buộc
đó; 2 slot cố định là đủ cho yêu cầu "ít nhất 2 bản sao ở 2 nơi khác nhau".

Mỗi slot chọn **1 trong 2 loại transport**, qua `worker/backup/storage/
factory.py::get_backend` (nơi DUY NHẤT trong toàn bộ hệ thống rẽ nhánh theo
transport — mọi nơi khác chỉ gọi qua Protocol `BackupStorageBackend`):

| Transport | Cách hoạt động | Immutability |
|---|---|---|
| `ssh` (`SSHStorageBackend`) | Upload qua **SFTP** tới một thư mục cố định trên host site-2. Ghi file tạm `.part` rồi `posix_rename` sang tên thật — người đọc **không bao giờ** thấy một file dở dang dưới đúng tên cuối cùng. | **Không tự enforce** — phụ thuộc cơ chế ngoài băng thông (out-of-band) trên host đích (vd. `chattr +i`). Nếu host đích đã khoá, lệnh xoá của backend sẽ thất bại và **lỗi được ném ra**, không bao giờ báo "đã xoá" giả. |
| `s3` (`S3StorageBackend`) | Upload qua `boto3` (AWS S3 thật hoặc MinIO — để trống `endpoint_url` nghĩa là AWS thật). | **S3 Object Lock** (`ObjectLockMode=COMPLIANCE`, khoá `immutable_lock_days` ngày) — CHỈ có hiệu lực nếu bucket đích được **tạo sẵn với Object Lock bật**; S3 không cho bật hồi tố. Backend không tự tạo bucket/bật Object Lock — nếu bucket chưa bật, S3 tự chối và lỗi được ném ra rõ ràng, không bị nuốt. |

Trong `backup_policy.yaml`, `backup_targets:` khai báo slot nào là bản
**immutable được chỉ định** (`immutable: true/false`) — quyết định này nằm ở
tầng policy, không phải setting của backend, và **engine luôn dùng đúng
`immutable_enabled` policy quy định** khi gọi `get_backend()` (mặc định
`False` nếu không chỉ định rõ — im lặng không bao giờ tự ý bật Object Lock
lên một bucket có thể chưa sẵn sàng).

Mọi backend (SSH lẫn S3) đều biểu diễn SHA256 dưới **cùng một định dạng**
(hex thường, không base64) — lớp gọi phía trên (`engine.py`, `restore.py`)
không cần biết đang nói chuyện với backend nào.

## 6. Lịch tự động (`worker/backup/scheduler.py`)

Chạy dựa trên `APScheduler`, đăng ký các job cron sau (đều cấu hình được
trong `backup_policy.yaml`'s `schedule:`):

| Job | Mặc định | Ghi chú |
|---|---|---|
| `rbd_backup_<pool>_<image>` (1 job/ảnh đã theo dõi) | 02:00 hàng ngày | Chỉ đăng ký cho từng entry trong `tracked_images:` |
| `backup_metadata_run` | mỗi 6 giờ (`*/6:00`) | Chỉ đăng ký nếu `metadata_cron` có cấu hình |
| `restore_drill_execute` | Thứ 2 hàng tuần, 03:00 | Chỉ đăng ký nếu `restore_drill:` đủ 4 trường (`pool`/`image`/`scratch_pool`/`scratch_image`) |
| `backup_alert_check` | mỗi 5 phút | Luôn đăng ký |
| `backup_digest_run` | 07:00 hàng ngày | Luôn đăng ký |

Khi một job cron tới hạn, `scheduler.py` **tự tạo một `Incident`+`Action`
đã ở trạng thái `APPROVED`** (không qua bước chờ duyệt — đây là hành động
**SAFE** theo policy, xem mục 9) rồi gọi thẳng
`worker/llm/router_client.py::_execute_approved_action` qua
`asyncio.to_thread` (tránh việc một export lớn, chạy hàng phút, làm nghẽn
event loop chung của Worker — vốn còn phải xử lý RabbitMQ và vòng lặp duyệt
Action khác).

## 7. Giám sát & cảnh báo (Story 9.4, 9.5)

### 7.1. RestoreDrill (`restore_drill_execute`) — chứng minh backup THẬT SỰ khôi phục được

Một backup "trông có vẻ thành công" (exit code 0, đã upload) không chứng
minh được nó **thực sự khôi phục lại được** — RestoreDrill định kỳ tự động
kiểm tra điều đó:

1. Tải bản **full** gần nhất thành công của một ảnh "canary" đã cấu hình
   (`restore_drill.pool`/`image` trong `backup_policy.yaml`) — chỉ full,
   không phục hồi cả chuỗi diff (đủ để chứng minh CƠ CHẾ phục hồi hoạt
   động; phục hồi chuỗi full+diff đầy đủ là việc của `restore.py`, dùng ở
   2 luồng khôi phục thật, mục 8).
2. `rbd import` vào một **image/pool riêng, chỉ dùng để thử**
   (`scratch_pool`/`scratch_image`) — không bao giờ đụng dữ liệu thật.
3. **Export lại** chính image scratch vừa import, băm SHA256, so với SHA256
   gốc của bản backup — khớp byte-for-byte mới coi là thành công (không chỉ
   dựa vào exit code của `rbd import`).
4. Dọn sạch image scratch sau khi xong (thành công hay thất bại đều dọn).
5. Kết quả (thành công/thất bại) được ghi thành một `BackupJob(job_type=
   "restore_drill")` riêng, và nếu thất bại sẽ gửi cảnh báo `critical` ngay
   lập tức.

Nếu `restore_drill:` để trống trong policy (mặc định — chưa có workload lab
thật) thì job này **không được đăng ký**, tương tự cách `tracked_images`
rỗng không đăng ký job backup nào.

### 7.2. Cảnh báo lỗi/quá hạn (`worker/backup/alerting.py`)

Job `backup_alert_check` (mỗi 5 phút) kiểm tra **từng ảnh đã theo dõi** +
metadata cụm, cảnh báo `warning` nếu:
- Chưa từng có backup thành công nào, HOẶC
- Bản gần nhất `FAILED`, HOẶC
- Bản thành công gần nhất đã **quá 24 giờ** (RPO cố định — hằng số
  `RPO_HOURS`, không đọc từ policy).

Cơ chế cảnh báo dùng chung một hàm `send_alert()`: **luôn ghi log** (mức
`CRITICAL`/`WARNING`), rồi gửi qua **từng kênh đã cấu hình, độc lập với
nhau** — một kênh lỗi không được phép chặn kênh còn lại:

- **Webhook JSON chung** — POST tới `settings.backup_alert_webhook_url` nếu
  đã cấu hình (để trống = tắt, không phải lỗi).
- **Telegram** (thêm sau, dùng chung `send_alert()` — xem mục 7.2b) — gửi
  qua Telegram Bot API nếu `settings.telegram_alerts_enabled=true` **và**
  đã có bot token + chat id.

Lỗi gửi ở BẤT KỲ kênh nào (mạng, endpoint sai, token sai) đều bị **nuốt và
chỉ log** — một alert gửi thất bại không được phép làm hỏng luồng backup/
drill vừa kích hoạt nó, và lỗi ở kênh này không được phép làm kênh kia
không chạy.

Webhook JSON là **cơ chế cảnh báo đầu tiên** trong toàn bộ ceph-aiops (không
có Slack/SMTP nào khác) — cố tình đơn giản ở phiên bản này: không chống
trùng lặp (một lỗi kéo dài nhiều tick vẫn gửi lại mỗi lần), không phân cấp
mức độ nghiêm trọng ngoài critical/warning.

### 7.2b. Telegram — kênh cảnh báo thứ hai (đẩy qua điện thoại)

Cùng dữ liệu, cùng điểm gửi (`send_alert()`) như webhook ở trên — **thêm
một kênh, không thay thế** — dùng module dùng chung
`shared/telegram_client.py::send_telegram_message()` (cùng vị trí
`shared/` mà `shared/router_client.py` đã đặt, để cả Worker lẫn Dashboard
đều gọi được mà không phá vỡ ranh giới AD-3), gọi Telegram Bot API
`sendMessage` qua `httpx`, tiền tố mỗi tin nhắn bằng biểu tượng mức độ
nghiêm trọng (🔴 CRITICAL / 🟡 WARNING / ℹ️ INFO) để đọc được ngay trên màn
hình khoá điện thoại.

**Kênh cảnh báo Backup này vẫn MỘT CHIỀU (gửi ra, không nhận vào):** không
có phản hồi nào từ Telegram được đọc lại cho kênh Backup, và không có gì
gõ trên Telegram ảnh hưởng tới `worker/backup/`. (Kể từ 2026-08-05,
ceph-aiops NÓI CHUNG có thêm một tính năng riêng, tuỳ chọn, đọc phản hồi
Telegram — "Yêu cầu phê duyệt qua Telegram" — nhưng đó là một mục hoàn
toàn tách biệt, công tắc bật/tắt riêng, không liên quan tới cảnh báo
Backup mô tả ở đây; xem toàn cảnh cả 4 mục tại
[telegram-alerts.md](./telegram-alerts.md).) Mặc định (mục này tắt), mọi
hành động khắc phục vẫn luôn phải đi qua đúng quy trình đề xuất → duyệt
trên Dashboard (`dashboard/routes/actions.py`) — Telegram chỉ là nơi vận
hành viên biết tin, không phải nơi ra lệnh.

Cấu hình tại Settings → **Cảnh báo Telegram** (chỉ admin thấy/sửa được —
cùng `_require_admin_privilege` mọi form nhạy cảm khác trong trang Settings
đã dùng):

| Trường | Ý nghĩa |
|---|---|
| Bot Token | Token tạo qua [@BotFather](https://t.me/BotFather) trên Telegram — lưu theo quy ước "để trống khi lưu = giữ nguyên giá trị đã lưu" giống `router_api_key`/secret key S3, không bao giờ hiện lại giá trị thật trên form |
| Chat ID | ID cuộc trò chuyện/nhóm/kênh sẽ nhận cảnh báo — DÙNG CHUNG cho cả 3 loại cảnh báo (xem [telegram-alerts.md](./telegram-alerts.md)) |
| Cảnh báo Backup | Công tắc bật/tắt RIÊNG khỏi "đã cấu hình token/chat id chưa" — admin tắt tạm thời được mà không phải xoá rồi gõ lại token. Từ 2026-08-05, đây chỉ là MỘT trong 3 công tắc độc lập trên cùng trang (còn có "Cảnh báo lỗi cụm" và "Cảnh báo phần cứng" — không thuộc phạm vi tài liệu này, xem [telegram-alerts.md](./telegram-alerts.md)) |

Lưu cấu hình này **khởi động lại Worker** ngay (cùng cách "Lưu trữ Backup"
đã làm) — vì `worker/backup/alerting.py` chạy trong tiến trình Worker, và
`settings` singleton của mỗi tiến trình chỉ đọc `.env` một lần lúc khởi
động. Nút **"Gửi thử"** gửi ngay một tin nhắn thật bằng cấu hình ĐÃ LƯU
(không phải giá trị chưa lưu trên form) trực tiếp từ tiến trình Dashboard —
xác nhận token/chat id đúng ngay lập tức thay vì phải chờ tới lần cảnh báo
thật đầu tiên mới biết cấu hình sai.

### 7.3. Phát hiện bất thường (`worker/backup/anomaly.py`) — thống kê, KHÔNG dùng AI

Chạy trên **mọi job backup thành công** (không phải chỉ khi thất bại), so
sánh `duration_seconds`/`size_bytes` của lần chạy này với lịch sử **của
chính ảnh đó** (tối thiểu 30 lần chạy `SUCCESS` gần nhất, cùng
`job_type` — full so với full, incremental so với incremental, không trộn):

```
deviation = |giá_trị_hiện_tại - trung_bình_lịch_sử| / độ_lệch_chuẩn_lịch_sử
```

Nếu `deviation >= anomaly_threshold_stddev` (mặc định **3** độ lệch chuẩn —
cấu hình được, không hard-code) ở **duration hoặc size** (kiểm tra độc lập,
bất thường ở 1 trong 2 đã đủ để gắn cờ) → coi là bất thường. Chưa đủ 30 lần
lịch sử → **không phán đoán** (không phải "coi là bình thường"), tránh báo
sai khi mới bắt đầu theo dõi một ảnh.

Ý nghĩa thực tế: một backup **exit code 0** nhưng đột nhiên nhanh bất
thường + nhỏ bất thường có thể là dấu hiệu dữ liệu nguồn đã mất/hỏng âm
thầm; ngược lại chạy lâu bất thường có thể là dấu hiệu sớm của vấn đề cluster
— cả hai đều **không thể phát hiện chỉ bằng exit code**.

### 7.4. Phân tích nguyên nhân bằng AI (`worker/backup/ai_analysis.py`)

Được gọi cho **mọi job thất bại**, và cho job thành công nhưng bị
`anomaly.py` gắn cờ bất thường (**không gọi AI cho một job thành công bình
thường** — kiểm soát chi phí/nhiễu). Dùng cùng router AI provider-agnostic
mà `worker/llm/router_client.py` (chẩn đoán Incident) và Chat-with-AI đã
dùng — không tạo client LLM riêng.

AI trả về (bắt buộc qua tool-calling, có schema `strict`):

| Trường | Ý nghĩa |
|---|---|
| `root_cause_summary_vi` | Tóm tắt nguyên nhân gốc bằng tiếng Việt |
| `severity` | `critical` / `warning` / `info` |
| `suggested_action_vi` | Đề xuất khắc phục **cụ thể**, tham chiếu đúng số liệu/host/lỗi quan sát được — yêu cầu rõ trong system prompt: không được nói chung chung |

Có **cơ chế fallback**: nếu bản thân lệnh gọi AI thất bại (router lỗi, hết
thời gian chờ), vẫn tạo một kết quả generic (`"Kiểm tra log Worker để biết
chi tiết"`, severity suy ra từ status) — vận hành viên **luôn nhận được một
tín hiệu nào đó**, không bị im lặng hoàn toàn chỉ vì AI tạm thời không gọi
được.

Có một bước **hạ cấp độ nghiêm trọng thông minh**: nếu job thất bại nhưng
sau đó **đã có một lần chạy SUCCESS mới hơn** cho đúng `(pool, image,
job_type)` đó (tự phục hồi rồi), severity `critical` do AI gán tự động hạ
xuống `warning` — tránh báo động giả cho một lỗi đã tự khỏi.

`critical` → cảnh báo **ngay lập tức** qua `alerting.send_alert`.
`warning`/`info` → **không** cảnh báo ngay, chỉ log — tích luỹ để xuất hiện
trong BackupDigest định kỳ (mục 7.5), tránh làm phiền vận hành viên với
từng sự kiện nhỏ lẻ.

### 7.5. BackupDigest — tổng hợp định kỳ bằng AI (`worker/backup/digest.py`)

Chạy hàng ngày (mặc định 07:00), gom số liệu của **24 giờ gần nhất**
(`period_hours` cấu hình được): số job thành công/thất bại, số bất thường,
kết quả RestoreDrill gần nhất — gửi cho AI viết một đoạn tóm tắt **văn xuôi
tự do bằng tiếng Việt** (không có schema cấu trúc như phân tích lỗi từng
job, vì đây chỉ cần một bản tóm tắt dễ đọc, không cần máy đọc lại). Có
fallback tương tự mục 7.4 nếu AI thất bại (in thẳng số liệu thô).

Digest được lưu bền vào `BackupDigestLog` (Dashboard trang **Backups** hiển
thị lại) và cũng đi qua `alerting.send_alert("info", ...)` — dùng lại đúng
cơ chế webhook đã có, không tạo đường gửi thứ hai.

## 8. Khôi phục (Restore) — 2 kịch bản khác nhau

### 8.1. Khôi phục MỘT image đè lên production (`restore_rbd_image_to_production`)

Dùng khi: **một image cụ thể** bị hỏng/mất dữ liệu, nhưng **cụm vẫn đang
chạy bình thường** — không cần dựng lại gì cả. Trang **Backups**
(`dashboard/routes/backups.py`, nút "Khôi phục" cạnh mỗi ảnh trong
`tracked_images`).

- Luôn **RISKY**, luôn cần duyệt thủ công — hành động này **ghi đè trực
  tiếp** lên dữ liệu production đang tồn tại.
- Toàn bộ logic thực sự nằm trong `worker/backup/restore.py::restore_image()`
  — dùng CHUNG với cả bước 12 của khôi phục toàn cụm (mục 8.2): tải bản
  **full** gần nhất + **toàn bộ chuỗi incremental** đã build trên đúng bản
  full đó (theo `base_job_id`), **áp dụng theo đúng thứ tự tạo** —
  `rbd import` cho full, rồi `rbd import-diff` lần lượt cho từng incremental.
- Mỗi file tải về đều được **verify SHA256 + kích thước** với storage
  backend trước khi đưa vào `rbd import`/`import-diff` — một lần tải bị lỗi
  giữa đường (mất mạng, hỏng dữ liệu truyền) bị bắt lỗi Ở ĐÂY, không bao giờ
  đẩy dữ liệu hỏng thẳng vào `rbd import`.
- Không bao giờ raise ra ngoài — trả về `RestoreResult(success=False,
  error_message=...)` để nơi gọi (route Dashboard hoặc phase DR) tự quyết
  định cách ghi nhận/báo cáo theo ngữ cảnh riêng của mình.

### 8.2. Khôi phục TOÀN BỘ cụm sau thảm hoạ (`restore_cluster_from_backup`)

Dùng khi cụm **đã sập hoàn toàn**, cần dựng lại từ đầu trên node mới. Đi qua
`worker/executor/cluster_deploy.py` (cùng orchestrator với dựng/xoá/chuyển
đổi cụm — xem tài liệu
[convert-ceph-deploy-to-cephadm.md](./convert-ceph-deploy-to-cephadm.md)
cho quy trình phase-runner tương tự), **không phải** `worker/backup/
engine.py`.

Xem chi tiết từng bước, điều kiện tiên quyết, và các rủi ro cần biết trước
tại **[runbook-dr.md](./runbook-dr.md)**.

## 9. Chính sách duyệt (SAFE vs RISKY)

| action_id | Phân loại | Vì sao |
|---|---|---|
| `rbd_backup_run` | SAFE — tự động, không qua duyệt | Chỉ đọc dữ liệu cụm (snapshot + export), ghi vào đích backup riêng, không sửa gì trên cụm nguồn |
| `retention_sweep_delete` | SAFE | Chỉ xoá các BẢN BACKUP cũ (ở đích lưu trữ), không đụng cụm |
| `backup_metadata_run` | SAFE | 5 lệnh `ceph ... get*/export/dump`, hoàn toàn read-only với cụm nguồn |
| `restore_drill_execute` | SAFE | Chỉ đụng scratch pool/image riêng, không đụng dữ liệu thật |
| `restore_rbd_image_to_production` | **RISKY**, luôn cần duyệt | Ghi đè dữ liệu THẬT đang tồn tại — không có ngoại lệ |
| `backup_delete_manual` | **RISKY**, luôn cần duyệt | Xoá một bản backup NGOÀI lịch retention tự động — chưa được triển khai thực thi (`engine.run()` trả `False` với log rõ ràng nếu action_id này được duyệt, chờ story sau) |
| `restore_cluster_from_backup` | **RISKY**, luôn cần duyệt | Dựng lại toàn bộ cụm — cùng mức rủi ro với dựng/xoá cụm |

`worker/policy/action_policy.yaml` đăng ký **cả 6 action_id** (kể cả
`backup_delete_manual` chưa có logic thật) ngay từ Story 9.1 — cùng tiền lệ
"đăng ký cả họ, nối logic thực thi dần dần" mà `cluster_deploy_action_ids`
đã thiết lập ở Epic 8, để các Story sau (9.3/9.4/9.7) không cần đổi gì ở
tầng policy khi thêm action_id mới.

## 10. Cấu hình cần thiết trước khi dùng

1. **`worker/policy/backup_policy.yaml`** — `backup_targets:` (transport
   nào cho slot a/b, slot nào immutable), `tracked_images:` (danh sách
   `(pool, image)` cần backup định kỳ — rỗng nghĩa là chưa bật gì),
   `retention:`, `schedule:`, `anomaly_threshold_stddev`, `restore_drill:`.
2. **Biến môi trường / trang Cài đặt** (`config/settings.py`) — với MỖI slot
   `a`/`b` đã chọn transport trong bước 1:
   - SSH: `backup_target_<slot>_ssh_host/user/key_path/landing_dir`
   - S3: `backup_target_<slot>_s3_endpoint/access_key/secret_key/bucket`
     (+ `immutable_lock_days` nếu slot đó `immutable: true`)
   - `backup_alert_webhook_url` (tuỳ chọn — để trống thì chỉ log, không gửi
     webhook; hiện chỉ chỉnh được qua `.env`, chưa có form trên Settings)
   - `telegram_bot_token`/`telegram_chat_id`/`telegram_alerts_enabled` —
     cấu hình qua Settings → **Cảnh báo Telegram** (chỉ admin), xem mục 7.2b

Thiếu cấu hình transport cho một slot khiến `get_backend()` raise
`BackupTargetNotConfiguredError` ngay khi cần dùng slot đó — lỗi rõ ràng,
không âm thầm bỏ qua đích đó.

## 11. Giới hạn / rủi ro cần biết

- Bản thân module gốc `worker/backup/engine.py` không tự gắn cảnh báo "chưa
  kiểm chứng trên cụm thật" như `watcher/volume_monitor.py`/
  `worker/executor/cluster_deploy.py`'s convert-to-cephadm — nhưng phần
  **khôi phục monmap** trong luồng DR toàn cụm (mục 8.2, chi tiết ở
  runbook-dr.md) có ghi rõ là **best-effort, chưa kiểm chứng trên cụm lab
  thật**, vì fsid của cụm mới dựng khác fsid gốc trong monmap backup — có
  thể bị Ceph từ chối `--inject-monmap` tuỳ phiên bản.
- `backup_delete_manual` (xoá thủ công một bản backup ngoài lịch retention)
  **đã đăng ký ở tầng policy nhưng chưa có logic thực thi** — duyệt Action
  này hiện tại sẽ bị `engine.run()` từ chối rõ ràng (log lỗi, trả `False`),
  không phải một no-op âm thầm.
- Cảnh báo alerting hiện **không chống trùng lặp/không throttle** — một lỗi
  kéo dài nhiều chu kỳ 5 phút sẽ gửi lại webhook mỗi lần, cho tới khi Story
  9.5's phân loại độ nghiêm trọng + digest (đã có) hoặc một cơ chế de-dup
  tương lai xử lý.
- RPO cảnh báo quá hạn (24 giờ) là **hằng số cố định trong code**
  (`alerting.py::RPO_HOURS`), không đọc từ `backup_policy.yaml` — muốn đổi
  phải sửa code, không chỉ sửa policy.

## 12. Các file liên quan trong mã nguồn

| File | Vai trò |
|---|---|
| `worker/backup/engine.py` | Orchestrator chính: `rbd_backup_run`, `retention_sweep_delete`, `restore_rbd_image_to_production`, dispatch tới `metadata.py`/`restore_drill.py` |
| `worker/backup/scheduler.py` | APScheduler — đăng ký & kích hoạt mọi job cron của Epic 9 |
| `worker/backup/metadata.py` | Backup monmap/osdmap/crushmap/auth/config |
| `worker/backup/restore.py` | Logic khôi phục full+diff chain dùng chung (Story 9.7) |
| `worker/backup/restore_drill.py` | Diễn tập khôi phục định kỳ, chỉ full, ra scratch, kèm so khớp SHA256 |
| `worker/backup/anomaly.py` | Heuristic thống kê phát hiện bất thường duration/size |
| `worker/backup/ai_analysis.py` | Phân tích nguyên nhân lỗi/bất thường + tóm tắt digest bằng AI |
| `worker/backup/alerting.py` | Gửi cảnh báo qua webhook + Telegram, kiểm tra overdue/failed định kỳ |
| `shared/telegram_client.py` | Client Telegram Bot API dùng chung (Worker + Dashboard), gửi-một-chiều |
| `worker/backup/digest.py` | Tổng hợp định kỳ, lưu `BackupDigestLog` |
| `worker/backup/policy_config.py` | Loader `backup_policy.yaml` dùng chung |
| `worker/backup/storage/{base,ssh_backend,s3_backend,factory}.py` | Abstraction lưu trữ 2 transport, immutability |
| `worker/executor/cluster_deploy.py` (`_phase_restore_*`) | 3 phase DR nối sau `deploy_cluster_ceph_deploy` |
| `dashboard/routes/backups.py` | Trang Backups: hàng đợi, lịch sử, digest, anomaly, đề xuất khôi phục 1 image |
| `dashboard/routes/restore_cluster.py` | Trang Restore Cluster: đề xuất khôi phục toàn cụm |
| `dashboard/routes/settings.py` (`telegram_settings_submit`/`telegram_settings_test`) | Form cấu hình Telegram trên Settings, chỉ admin — lưu + "Gửi thử" |
| `worker/policy/backup_policy.yaml` | Cấu hình đích lưu trữ, ảnh theo dõi, retention, lịch, ngưỡng bất thường |
| `worker/policy/action_policy.yaml` | Phân loại SAFE/RISKY cho từng action_id của họ backup |
| `shared/models.py` | `BackupJob`, `BackupAnomaly`, `BackupDigestLog` |
| `config/settings.py` | Cấu hình 2 slot đích backup (`backup_target_a_*`/`backup_target_b_*`), webhook cảnh báo |
