# Runbook: Log Intelligence & AI RCA

Tài liệu vận hành cho tính năng ở `Plan/log-intelligence-rca-plan.md` (Pha 6
của `Plan/ai-missing-features-roadmap.md`). Dành cho người bật/tắt/chỉnh và
người trực nhận cảnh báo.

Cập nhật: 2026-08-21. Trạng thái code: L0–L5 đã hoàn thành; production đã
chạy nguồn Loki. Xem trạng thái bàn giao tổng thể tại `docs/CEPH_AI_HANDOFF.md`.

Quy trình triển khai để biến LogFinding đã xác minh thành dữ liệu học có giám
sát nằm tại [`loki-daemon-log-learning.md`](loki-daemon-log-learning.md).

---

## 1. Nó làm gì

Cứ 15 phút, đọc log mon/mgr/osd/rgw của mọi node đã cấu hình, gom các dòng
log thành **mẫu** (bỏ số/địa chỉ/id), đếm tần suất, chọn ra mẫu bất thường,
rồi (tuỳ chọn) hỏi AI xem nguyên nhân gốc có thể là gì.

Kết quả đi tới 3 nơi: **Telegram** (kênh Cụm Ceph), trang
**`/log-intelligence`**, và **hàng chờ Duyệt** dưới dạng đề xuất.

**Không có gì tự chạy ra cụm.** Đề xuất luôn ở trạng thái chờ Duyệt thủ công.

---

## 2. Bật lần đầu

Hai công tắc **tách riêng**, bật theo đúng thứ tự này:

> **Cấu hình ở đâu:** trang **Cài đặt → Log Intelligence** (`/settings`, mục
> "Log Intelligence"). Lưu xong Watcher tự khởi động lại. Các khoá `.env`
> tương ứng vẫn dùng được nếu bạn thích sửa file, nhưng phải tự restart Watcher.

### Bước 1 — bật thu thập (không tốn tiền AI)

Tick **"Bật thu thập log"** trên form (tương đương `log_intel_enabled=true`).

Chạy **ít nhất 3–7 ngày** trước khi sang bước 2. Lý do:

- Tầng phát hiện đột biến cần lịch sử để có baseline. Ngày đầu baseline rỗng
  nên nó cố ý im lặng (không đoán khi thiếu mẫu).
- Bạn cần biết mỗi lần quét gắn cờ bao nhiêu mẫu **trước khi** trả tiền
  token theo đúng con số đó.

Theo dõi cột **Gắn cờ** trên trang `/log-intelligence`, hoặc:

```sql
SELECT created_at, status, lines_scanned, patterns_seen, patterns_flagged
FROM log_ingest_runs ORDER BY created_at DESC LIMIT 20;
```

**Ngưỡng lành mạnh: 0–5 mẫu gắn cờ mỗi lần quét.** Nếu vài chục thì đừng bật
AI vội — sang mục 4 chỉnh trước.

### Bước 2 — bật phân tích AI

Tick thêm **"Bật phân tích AI"** (tương đương `log_intel_ai_enabled=true`).

Cần `router_api_key` / `router_base_url` / `router_model` đã cấu hình sẵn
(cùng router mà Chat-with-AI và chẩn đoán sự cố đang dùng).

### Yêu cầu trước khi bật

- Đã chạy `alembic upgrade head` (4 bảng `log_*`).
- SSH tới các node đã hoạt động (chính là đường mà panel "Log" trên trang
  Nodes đang dùng — nếu panel đó xem được log thì tính năng này chạy được).

---

## 3. Đọc kết quả

### Trang `/log-intelligence`

Đọc **từ trên xuống**, đúng thứ tự trang bày ra:

1. **Trạng thái thu thập** — nhìn cột Trạng thái trước tiên.
   - `OK`: mọi node đọc được log.
   - `PARTIAL`: có node hụt. Kết luận bên dưới vẫn dùng được nhưng AI đã bị
     hạ độ tin cậy tối đa xuống MEDIUM. Xem cột Lỗi để biết node nào.
   - `FAILED`: không đọc được node nào. Cửa sổ đó không có dữ liệu, và bước
     đóng vòng đời được bỏ qua (nếu không sẽ đóng nhầm hàng loạt).
2. **Phát hiện** — kết luận AI. Luôn có **bằng chứng gốc** (mẫu log thật)
   ngay dưới. **Hãy đọc bằng chứng trước, kết luận sau.**
3. **Mẫu log** — nơi tắt nhiễu (mục 4).

### Ý nghĩa các nhãn

| Nhãn | Nghĩa |
|---|---|
| `FINDING` | AI cho rằng có vấn đề, và neo được vào mẫu log có thật |
| `NO_FINDING` | Không có gì bất thường (không lưu hàng, không báo) |
| `INSUFFICIENT_EVIDENCE` | Không đủ bằng chứng để kết luận — **không phải lỗi**, là câu trả lời hợp lệ và đáng tin hơn một phỏng đoán |
| Độ tin cậy LOW/MEDIUM/HIGH | Tự đánh giá của AI, đã bị hệ thống hạ xuống nếu dữ liệu thu thập không đầy đủ |

### Dòng "Hệ thống đã chỉnh câu trả lời của AI"

Xuất hiện khi server phải sửa/loại bỏ thứ gì đó model trả về — ví dụ model
trích dẫn mẫu log không tồn tại, hoặc đề xuất một hành động ngoài danh sách
cho phép.

**Thấy dòng này nhiều lần liên tiếp = tín hiệu xấu về chất lượng model.** Cân
nhắc đổi `router_model`.

### Cảnh báo Telegram

Chỉ gửi khi mức **WARNING/CRITICAL**. Mỗi vấn đề báo **đúng một lần**, kể cả
khi nó kéo dài nhiều ngày. Khi hết sẽ có tin nhắn "Đã hết".

Mức INFO và INSUFFICIENT_EVIDENCE **không** gửi Telegram — vẫn xem được trên
trang.

---

## 4. Chỉnh khi bị nhiễu

Theo thứ tự nên thử:

### 4.1 Gắn nhãn BENIGN (ưu tiên — chính xác nhất)

Trang `/log-intelligence` → khối **Mẫu log** → chọn `BENIGN` → Lưu.

Mẫu đó bị loại khỏi mọi kiểm tra, vĩnh viễn, **không cần sửa code hay khởi
động lại**. Đây là cách đúng để tắt một loại log ồn cụ thể.

Ngược lại, `NOTABLE` khiến một mẫu luôn nổi lên kể cả khi tần suất bình
thường.

> Cần quyền admin. Gắn `BENIGN` sai sẽ làm hệ thống im lặng với đúng thứ lẽ
> ra phải báo — hãy chắc chắn trước khi gắn.

### 4.2 Nới ngưỡng (khi nhiễu trải rộng nhiều mẫu)

| Tham số | Mặc định | Tăng lên khi |
|---|---|---|
| `log_intel_burst_ratio` | 5.0 | Bị báo đột biến quá nhiều |
| `log_intel_novelty_min_count` | 3 | Mẫu mới lác đác cũng bị báo |
| `log_intel_burst_min_baseline_samples` | 3 | Baseline mỏng vẫn kết luận |

### 4.3 Giảm tải thu thập

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `log_intel_scan_interval_seconds` | 900 | Tăng nếu SSH tới node quá nặng |
| `log_intel_max_lines_per_daemon` | 5000 | Giảm nếu mỗi lần quét quá lâu |
| `log_intel_window_minutes` | 60 | **Luôn để lớn hơn chu kỳ quét** (chống lỗ hổng dữ liệu) |

### 4.4 Giảm chi phí AI

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `log_intel_max_evidence_chars` | 20000 | Trần kích thước phần evidence trong prompt |
| `log_intel_ai_enabled` | false | Tắt hẳn AI, vẫn giữ thu thập + phân loại |

---

## 5. Tắt / rollback

### Tắt mềm (khuyến nghị)

Bỏ tick trên form **Cài đặt → Log Intelligence**:
- Bỏ **"Bật phân tích AI"** → chỉ tắt AI, vẫn thu thập.
- Bỏ cả **"Bật thu thập log"** → tắt hẳn.

Watcher tự khởi động lại khi Lưu. **Dữ liệu đã có được giữ nguyên**, trang
`/log-intelligence` vẫn xem được.

Tắt `log_intel_enabled` sẽ dừng luôn cả bước đóng vòng đời — các phát hiện
đang mở sẽ **kẹt ở trạng thái OPEN**. Đóng thủ công nếu cần.

### Rollback schema

```bash
alembic downgrade 5bdecca5014e   # gỡ cả 4 bảng log_*
```

Chỉ làm khi thật sự cần gỡ tính năng: mọi mẫu log, số đếm và phát hiện đã
tích luỹ sẽ mất và phải xây lại baseline từ đầu.

Không cần gỡ code — hai công tắc mặc định `false` nên code nằm im.

---

## 6. Dung lượng và retention

Bảng lớn nhất là `log_pattern_observations` (phình theo **khối lượng** log).
Các bảng còn lại phình theo **số loại** log nên nhỏ hơn nhiều bậc.

| Bảng | Retention | Tham số |
|---|---|---|
| `log_pattern_observations` | 30 ngày | `log_intel_observation_retention_days` |
| `log_findings` (chỉ RESOLVED) | 90 ngày | `log_intel_finding_retention_days` |
| `log_ingest_runs` | 180 ngày | `log_intel_pattern_retention_days` |
| `log_patterns` | **không xoá** | — (danh mục; là nơi phát hiện neo bằng chứng vào) |

Dọn dẹp chạy tự động mỗi lần quét. Phát hiện còn **OPEN/ACKNOWLEDGED không
bao giờ bị xoá vì già** — nó vẫn là việc chưa xong.

**Không bảng nào chứa log thô.** Chỉ lưu mẫu, số đếm, và một dòng mẫu đã
được che bí mật. Đây là ràng buộc thiết kế cố định, vì DB của app đã có cảnh
báo dung lượng riêng (`watcher/database_capacity_monitor.py`).

Ước lượng nhanh: `số mẫu × số giờ × số host` dòng trong bảng observations.
Ví dụ 300 mẫu × 24h × 30 ngày × 3 host ≈ 650k dòng.

---

## 7. Xử lý sự cố

| Triệu chứng | Nguyên nhân thường gặp | Xử lý |
|---|---|---|
| Không có lần quét nào | `log_intel_enabled=false`, hoặc Watcher chưa khởi động lại | Bật và restart Watcher |
| Mọi lần quét `FAILED` | SSH hỏng, hoặc chưa cấu hình node | Thử panel "Log" trên trang Nodes trước |
| `PARTIAL` liên tục ở một node | Node đó không đọc được log | Xem cột Lỗi; kiểm tra daemon/quyền trên node |
| Có mẫu, không có phát hiện | `log_intel_ai_enabled=false`, hoặc triage không gắn cờ gì (bình thường!) | Kiểm cột Gắn cờ; 0 nghĩa là cụm đang yên |
| Gắn cờ hàng chục mẫu mỗi lần | Ngưỡng quá nhạy hoặc chưa gắn BENIGN | Mục 4 |
| Có phát hiện nhưng không có tin nhắn | Mức INFO, hoặc kênh Telegram chưa bật | Kiểm `telegram_incident_enabled` |
| Log Watcher báo "gọi router thất bại" | Router AI chết/quá tải | Thu thập vẫn chạy bình thường; sửa router rồi thôi |
| Nhiều dòng "Hệ thống đã chỉnh câu trả lời của AI" | Model chất lượng kém | Đổi `router_model` |

Từ khoá tìm trong log Watcher: `log_intel`, `log_analysis`.

---

## 8. Ranh giới an toàn (đừng phá)

Bốn tính chất này là lý do tính năng được phép đọc log do người ngoài tác
động rồi đưa vào model. Đừng nới lỏng nếu chưa hiểu hết hệ quả:

1. **Không có gì tự chạy ra cụm.** Đề xuất luôn ở PENDING_APPROVAL.
2. **AI không được sinh câu lệnh** — chỉ chọn `action_id` từ danh sách rất
   hẹp (6 hành động chẩn đoán), đã loại mọi hành động huỷ dữ liệu.
3. **Log là dữ liệu không tin cậy.** Tên bucket/client trong log RGW do
   người ngoài đặt và có thể chứa mệnh lệnh nhắm vào model. Log được bọc
   trong hàng rào đánh dấu, và **server luôn kiểm tra lại** mọi thứ model
   trả về.
4. **Bí mật không đi vào prompt.** cephx key, chữ ký S3, token đều bị che
   trước khi lưu hoặc gửi.

Nếu cần cho phép thêm hành động, sửa allowlist trong
`watcher/log_analysis.py::_allowed_action_ids` — **và chạy lại nhóm test bảo
mật** (`tests/test_log_analysis.py`, `tests/test_log_intelligence_e2e.py`).

---

## 9. Khi Loki lên (L5, chưa làm)

Trên form **Cài đặt → Log Intelligence**: đổi *Nguồn log* sang `loki`, điền
*Loki URL* (và *tenant* nếu Loki chạy multi-tenant), bấm **Kiểm tra kết nối
Loki** để xác nhận trước, rồi Lưu.

Form chặn sẵn hai lỗi hay gặp: chọn `loki` mà bỏ trống URL, và đặt *cửa sổ
thời gian* nhỏ hơn *chu kỳ quét* (sẽ để lại lỗ hổng dữ liệu vĩnh viễn).

Bên ship log phải gắn nhãn khớp:
`{cluster="<tên cụm>", host="<ip>", daemon_type="mon|mgr|osd|rgw"}`.
Nhãn khác thì sửa `watcher/log_source/loki.py::_selector` — không nơi nào
khác trong codebase biết về nhãn.

> ⚠️ **Đừng lưu chunk Loki lên chính cụm Ceph đang giám sát.** Loki hỗ trợ
> backend S3 và hệ thống có sẵn RGW — nhìn thì gọn, nhưng đó là phụ thuộc
> vòng: Ceph sập thì mất luôn log cần để chẩn đoán tại sao Ceph sập.

Tầng phân tích không đổi gì khi chuyển nguồn.

---

## 10. File liên quan

| Vai trò | File |
|---|---|
| Kế hoạch & lý do thiết kế | `Plan/log-intelligence-rca-plan.md` |
| Adapter nguồn log | `watcher/log_source/` |
| Thu thập + fingerprint | `watcher/log_intel.py` |
| Phân loại (không AI) | `watcher/log_triage.py` |
| Phân tích AI + cảnh báo + đề xuất | `watcher/log_analysis.py` |
| Trang Dashboard | `dashboard/routes/log_intelligence.py` |
| Cảnh báo Telegram | `shared/telegram_alerts.py` |
