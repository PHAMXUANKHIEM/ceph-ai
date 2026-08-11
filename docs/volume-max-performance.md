# Hiệu năng tối đa của Volume (RBD Image) trong ceph-aiops

Tài liệu này mô tả cách ceph-aiops xác định **hiệu năng tối đa thực sự** của
một Volume (RBD image) / pool, đặc biệt là thuật toán dùng để nhận biết khi
nào một Volume đã **chạm trần hiệu năng**.

Nguồn: `worker/executor/volume_perf.py`, `dashboard/routes/volumes.py`,
`dashboard/volume_perf_analysis.py`, `watcher/volume_monitor.py`,
`shared/models.py` (`VolumeMetric`, `VolumePerfSweep`).

## 1. Vì sao "peak IOPS đã quan sát được" KHÔNG phải là hiệu năng tối đa thật

ceph-aiops lưu mỗi lần Watcher poll một dòng `VolumeMetric` (pool, image,
iops, latency đọc/ghi, ...). Trang Volumes có thể hiển thị **peak** — giá trị
IOPS/latency cao nhất từng ghi nhận được trong toàn bộ lịch sử của volume đó.

Con số "peak" này **chỉ phản ánh workload thực tế đã từng chạy**, không phản
ánh volume có thể chịu tải tới đâu — nếu VM phía trên chưa bao giờ đẩy tải đủ
mạnh, "peak" quan sát được sẽ thấp hơn nhiều so với khả năng thật của
pool/cluster đằng sau. Đây chính là lý do ceph-aiops có riêng một tính năng
**chủ động đo** (Load Sweep) thay vì chỉ dựa vào số liệu quan sát thụ động.

ceph-aiops thực ra có **hai cơ chế bổ sung cho nhau**:

| Cơ chế | Kiểu | Trả lời câu hỏi | Nguồn |
|---|---|---|---|
| Giám sát bão hoà thụ động | Passive, luôn chạy nền | "Volume này **đang** có dấu hiệu chạm trần **ngay bây giờ**, dưới tải thật không?" | `watcher/volume_monitor.py` |
| Đo hiệu năng tối đa (Load Sweep) | Active, chạy khi được yêu cầu | "Pool này **thực sự** chịu được tối đa bao nhiêu IOPS trước khi độ trễ tăng vọt?" | `worker/executor/volume_perf.py` |

Phần trọng tâm trả lời "làm sao biết Volume đạt tới hiệu năng tối đa" là cơ
chế thứ hai (mục 3) — nhưng mục 2 (thụ động) cũng đáng nói vì nó là tín hiệu
sớm, chạy liên tục không cần vận hành viên bấm gì.

## 2. Cơ chế thụ động: phát hiện bão hoà từ traffic thật (`watcher/volume_monitor.py`)

Chạy trong mỗi vòng poll của Watcher, hoàn toàn bằng heuristic, **không gọi
AI**. Giữ một cửa sổ trượt (rolling window) trong bộ nhớ cho từng
`(pool, image)`:

- `ROLLING_WINDOW_SIZE = 12` mẫu gần nhất (khoảng vài phút ở chu kỳ poll 15s
  mặc định).
- Với mỗi mẫu mới, một volume được coi là **"trông giống bão hoà"** ở đúng
  mẫu đó khi thoả **đồng thời cả hai** điều kiện:
  1. **Gần đỉnh:** `iops_hiện_tại >= NEAR_PEAK_RATIO (0.9) × peak_iops_trong_cửa_sổ` — IOPS hiện tại đã đạt ít nhất 90% đỉnh mà chính volume này từng đạt được trong cửa sổ gần đây.
  2. **Latency tăng bất thường:** `latency_hiện_tại >= LATENCY_SPIKE_MULTIPLIER (2.0) × latency_baseline` — trong đó `latency_baseline` là **trung vị (median)** độ trễ trong cùng cửa sổ, tức latency hiện tại cao gấp ít nhất 2 lần mức "bình thường" gần đây của chính volume đó.
- Một mẫu bão hoà đơn lẻ **không đủ** để báo cáo — phải bão hoà
  `CONSECUTIVE_POLLS_REQUIRED = 3` lần poll **liên tiếp** mới được coi là
  bão hoà thật (`is_saturated_now`), tránh báo động giả vì một mẫu nhiễu
  ngẫu nhiên.
- Cửa sổ phải **đầy** (đủ 12 mẫu lịch sử) trước khi heuristic được phép trả
  `True` lần đầu — một volume Watcher mới thấy lần đầu có "thời gian khởi
  động" thay vì bị phán đoán ngay khi chưa có baseline.

Khi một volume đạt `is_saturated_now=True`, hệ thống tự tạo một `Incident`
với `ceph_code = "VOLUME_SATURATED:<pool>/<image>"` (không qua chẩn đoán AI —
lý do đã hoàn toàn xác định từ con số, gọi AI chỉ tốn thêm round-trip và có
rủi ro AI chọn nhầm action_id) + một `Action` (`investigate_manually`, RISKY,
chờ duyệt thủ công). Khi volume không còn nằm trong tập bão hoà ở lần poll
sau, Incident tương ứng tự chuyển `RESOLVED`.

**Giới hạn đã biết** (ghi trong chính module):
- Đây là bản triển khai **chưa được kiểm chứng trên cluster thật** (`2026-07-28,
  NOT verified against a real cluster yet` — chưa có pool RBD nào được tạo
  trong lab cluster của dự án tại thời điểm viết).
- Trạng thái cửa sổ trượt **chỉ nằm trong bộ nhớ**, mất khi Watcher restart
  (không phải bug, chỉ cần vài poll để "làm nóng" lại).
- Chỉ có heuristic latency-knee; so sánh với QoS cap đã cấu hình
  (`rbd_qos_iops_limit`/`rbd_qos_bps_limit`) — vốn đơn giản và chính xác hơn
  khi vận hành viên có đặt QoS — đã được thiết kế nhưng **chưa triển khai**
  trong bản này.

## 3. Cơ chế chủ động: "Đo hiệu năng tối đa" (Load Sweep) — trọng tâm

Đây là câu trả lời trực tiếp cho câu hỏi **"làm sao biết một Volume/pool đã
đạt tới hiệu năng tối đa"**: chủ động tạo tải tăng dần bằng `fio`, đo IOPS và
latency ở từng mức tải, rồi tìm điểm **"đầu gối" (knee)** — nơi đẩy tải thêm
chỉ đổi lấy một chút IOPS nhưng khiến latency tăng vọt bất cân xứng.

### 3.1. Nguyên tắc an toàn: không bao giờ đụng volume thật

Toàn bộ phép đo chạy trên **một scratch image riêng**
(`_ceph_aiops_perf_probe`, 50GB, thin-provisioned) được tạo mới ngay trong
pool đang đo. Nếu còn image probe từ một lần chạy bị gián đoạn, hệ thống xóa
nó trước rồi mới tạo lại; sau khi đo xong hoặc fio gặp lỗi, image cũng được
xóa hẳn để không giữ các block random-write đã cấp phát. Cleanup thất bại làm
cả lượt đo chuyển `FAILED`, thay vì âm thầm để lại image rác. Route đề xuất
(`propose_volume_perf_sweep`) **không** nhận tham số `image` từ request —
nên không có cách nào (kể cả bằng request thủ công) trỏ phép đo vào một
volume thật. Đây là quyết định phạm vi tường minh của vận hành viên khi yêu
cầu tính năng này: một lần đo tuyệt đối không được cạnh tranh I/O với traffic
thật trên dữ liệu thật.

Hệ quả cần hiểu rõ: kết quả đo phản ánh **năng lực khả dụng của cả pool/
cluster tại thời điểm đo**, không phải "trần riêng của một volume cụ thể" —
hai con số này chỉ trùng nhau khi không có traffic nào khác đang cạnh tranh
I/O, là trường hợp phổ biến khi cần trả lời "cluster còn dư sức hay đã kịch
trần" nhưng không phải một cam kết per-volume tuyệt đối.

### 3.2. Cách quét tải

Chạy `fio` trên node MON đầu tiên, nhắm vào scratch image qua `ioengine=rbd`,
với thang `iodepth` tăng dần:

```
IODEPTH_STEPS = 1, 2, 4, 8, 16, 32, 64, 128, 256
```

Mỗi mức iodepth chạy **3 mẫu độc lập**, sau đó dùng median của IOPS, latency
avg và latency p99 làm điểm đại diện. Hệ số biến thiên IOPS (CV%) cũng được
lưu để người vận hành nhận ra một lượt đo thiếu ổn định. Mỗi mẫu chạy:

```bash
fio --name=sweep --ioengine=rbd --pool=<pool> --rbdname=_ceph_aiops_perf_probe \
    --rw=randwrite --bs=4k --iodepth=<depth> --numjobs=1 \
    --runtime=20 --ramp_time=5 --time_based --direct=1 \
    --invalidate=1 --randrepeat=0 --norandommap \
    --group_reporting --output-format=json
```

- **4k random write** — cùng dạng tải benchmark chuẩn ("textbook IOPS vs.
  latency knee") mà vận hành viên đã dùng trong script riêng trước khi tính
  năng này ra đời.
- `ramp_time=5s` để hệ thống ổn định trước khi đo, `runtime=20s` mỗi mẫu —
  ngắn hơn kịch bản gốc (60s+10s) có chủ đích: đây là tải thật lên cluster
  production, mỗi giây đều có giá; 20s/bước (kết hợp early-stop, xem 3.4)
  ba mẫu giúp giảm ảnh hưởng của một khoảng nhiễu đơn lẻ; lượt chạy đầy đủ
  có thể mất khoảng 11–12 phút, còn early-stop thường kết thúc sớm hơn.
- Từ JSON `fio` trả về, hệ thống lấy 3 số cho mỗi bước: `iops`,
  `latency_avg_ms` (từ `clat_ns.mean`), `latency_p99_ms` (từ
  `clat_ns.percentile["99.000000"]`) — dùng **p99**, không dùng latency
  trung bình, để phát hiện knee (trung bình dễ bị làm mượt bởi phần lớn I/O
  vẫn nhanh, che mất đúng phần đuôi bắt đầu xấu đi).

### 3.3. Thuật toán phát hiện "đầu gối" (knee) — cách hệ thống *biết* đã chạm trần

Đây là phần lõi trả lời trực tiếp câu hỏi. Hàm `_detect_knee(steps)` duyệt
qua từng cặp bước liên tiếp `(prev, cur)` theo `iodepth` tăng dần, tính:

```
iops_growth      = (cur.iops - prev.iops) / prev.iops
latency_growth   = (cur.latency_p99_ms - prev.latency_p99_ms) / prev.latency_p99_ms
```

Một chuyển tiếp được coi là xấu khi IOPS đã plateau **và** latency xấu:

| Ký hiệu | Điều kiện | Ngưỡng | Ý nghĩa |
|---|---|---|---|
| A. `plateaued` | `iops_growth < 0.10` | IOPS tăng thêm dưới 10% | Đẩy thêm tải gần như không mang lại IOPS |
| B. tăng latency tương đối | `latency_growth >= 50%` và tăng tuyệt đối ít nhất `1ms` | Loại các dao động rất nhỏ | Tail latency xấu đi rõ rệt |
| C. latency tuyệt đối | `cur.latency_p99_ms >= 20ms` | Chỉ có hiệu lực cùng A | Không gọi là bão hòa nếu IOPS vẫn còn scale mạnh |

Chỉ khi có **hai chuyển tiếp xấu liên tiếp**, hệ thống mới kết luận: bước
ngay trước chuyển tiếp xấu đầu tiên là "trần hiệu năng khả dụng" — không
phải bước có IOPS cao nhất trong toàn bộ sweep, mà là **điểm cuối cùng còn
"đáng dùng"** trước khi rơi xuống vực latency. Đây đúng là cách diễn giải
"hiệu năng tối đa" mà vận hành viên mô tả khi yêu cầu tính năng này: ví dụ
gốc là "~3% thêm IOPS đổi lấy ~14x latency" — rõ ràng không đáng, nên điểm
trước đó mới là trần thật sự, không phải điểm có số IOPS lớn nhất.

Vì sao cần **cả hai** tín hiệu A và B cùng lúc (không chỉ một)? Ví dụ gốc
(~3% IOPS / ~14x latency) là một trường hợp cực đoan — nếu chỉ đòi "IOPS
plateau" không thôi, một cặp điểm còn đang tăng bình thường nhưng có nhiễu
ngẫu nhiên nhỏ cũng có thể bị nhận nhầm là knee. Đòi cả hai cùng đúng loại
bỏ khả năng báo nhầm trên nhiễu thông thường, còn điều kiện C là lưới an
toàn riêng, không phụ thuộc hình dạng đường cong tăng/giảm.

**Nếu sweep chạy hết toàn bộ thang `iodepth` (tới 256) mà không bao giờ thoả
điều kiện knee** — `_detect_knee` trả về `None`. Điều này có nghĩa: **cluster
chưa bao giờ bị đẩy tới bão hoà trong phạm vi đã thử** — bước cuối cùng
(`iodepth=256`) chỉ là một **cận dưới (floor)**, KHÔNG phải trần thật. Kết
quả sweep vẫn được lưu đầy đủ đường cong, nhưng `knee_iodepth`/`knee_iops`
trong DB đều `NULL` để phân biệt rõ hai trường hợp này — trang Volumes và cả
phần phân tích AI (mục 3.6) đều phải diễn giải đúng: "chưa saturate" khác
hẳn "đã tìm ra trần".

### 3.4. Dừng sớm (early stop) — không sweep hết 256 nếu đã thấy knee

Vòng lặp không chạy trọn cả 9 bước một cách mù quáng. Sau mỗi bước, hệ thống
gọi lại `_detect_knee` trên toàn bộ các bước đã có:

- Thuật toán không dừng vì một điểm xấu đơn lẻ. Nó chỉ dừng khi hai cặp mức
  iodepth liên tiếp cùng cho thấy IOPS plateau và latency xấu đi.

Nhờ vậy một sweep bão hoà sớm (vd. ở iodepth=16) kết thúc nhanh hơn nhiều so
với việc luôn chạy đủ cả 9 mức.

### 3.5. Diagnostics bổ sung — tránh hiểu nhầm nguyên nhân "trần"

Ngay sau khi sweep xong, hệ thống thu thập thêm hai loại bằng chứng
**best-effort** (không bao giờ làm hỏng một kết quả sweep tốt nếu chính
chúng thất bại):

- **QoS notes** (`rbd config image list <pool>/_ceph_aiops_perf_probe | grep -i qos`)
  — loại trừ khả năng "trần" đo được thực ra là một giới hạn QoS nhân tạo đã
  cấu hình sẵn trên image, chứ không phải giới hạn vật lý thật của cluster.
  Đây là mục "kiểm tra trước khi kết luận" mà chính vận hành viên đã yêu cầu.
- **Bottleneck notes** — `ceph osd perf` (commit/apply latency mỗi OSD) trên
  MON, cộng `iostat -x 1 2` trên tối đa 5 OSD host đầu tiên (giới hạn số
  lượng vì đây chỉ là best-effort, không đáng để fan-out không giới hạn).
  Lấy ngay sau bước tải nặng nhất của sweep — là một **xấp xỉ**, không phải
  phép đo đồng thời chính xác tại đúng khoảnh khắc nghẽn (SSH lấy iostat
  đồng thời với fio đang chạy sẽ cần một kết nối thứ hai chạy đua với chính
  benchmark). Dữ liệu thô này được hiển thị nguyên văn cho vận hành viên tự
  đọc, hệ thống không tự diễn giải/kết luận thay — cùng triết lý "không lạm
  xưng chẩn đoán" với các công cụ chẩn đoán read-only khác trong ceph-aiops.

### 3.6. Lớp đọc thứ hai: "Phân tích bằng AI" (tuỳ chọn, không thay thế heuristic)

Sau khi có kết quả sweep, vận hành viên có thể bấm **"Phân tích bằng AI"**
(`dashboard/volume_perf_analysis.py`) để gửi toàn bộ đường cong + kết quả
knee thuật toán + QoS/bottleneck notes cho LLM (router đã cấu hình), yêu cầu
kết luận bằng tiếng Việt. Đây là **một lượt đọc thứ hai độc lập** trên cùng
bằng chứng, không phải cách tính lại cùng công thức — vì heuristic ở mục 3.3
là một bộ ngưỡng cố định, được tinh chỉnh theo đúng một hình dạng ví dụ, có
thể bỏ sót một đường cong bão hoà theo kiểu khác. Cả hai kết quả (số của
heuristic VÀ kết luận của AI) đều được hiển thị song song, không cái nào
thay thế cái nào.

System prompt yêu cầu rõ AI phải:
- Tìm **"maximum USABLE performance ceiling"** — điểm mà đẩy thêm tải chỉ
  đổi một chút IOPS lấy latency tăng bất cân xứng, **không đơn thuần là số
  IOPS cao nhất trong dữ liệu**.
- **Thành thật nói "chưa saturate"** nếu sweep chưa rõ ràng chạm trần, thay
  vì bịa ra một con số trần không có thật.
- Giải thích bằng tiếng Việt, ngôn ngữ dễ hiểu cho người không rành sâu về
  benchmark storage.

Kết quả AI trả về (bắt buộc đủ các trường qua tool-calling, không chấp nhận
free-text) gồm:

| Trường | Ý nghĩa |
|---|---|
| `max_iops` | IOPS trần kết luận |
| `max_iops_basis` | `saturation_knee` (đường cong có knee rõ ràng) hoặc `highest_tested_not_saturated` (sweep chưa bao giờ saturate trong phạm vi đã thử — đây chỉ là cận dưới, không phải trần thật) |
| `confidence` | `high` / `medium` / `low` |
| `conclusion_vi` | 1–3 câu kết luận |
| `caveats_vi` | Lưu ý/giới hạn — vd nghi ngờ nghẽn ở đâu, có bị QoS giới hạn không, khi nào nên đo lại |

Việc gọi AI **chỉ đọc dữ liệu đã thu thập sẵn** (không SSH, không tạo tải
mới lên cluster) nên chạy ngay từ tiến trình Dashboard, không cần qua cơ chế
đề xuất/duyệt của Worker — giống `dashboard/chat_client.py`.

## 4. Luồng thao tác trên Dashboard

1. Vận hành viên vào trang **Volumes**, chọn một pool, bấm **"Đo hiệu năng
   tối đa"**. **Chỉ tài khoản admin** mới bấm được nút này (ngưỡng cao hơn
   mức "bất kỳ ai đã đăng nhập" mặc định của các action propose-rồi-duyệt
   khác — vì hành động này tạo tải I/O thật lên cluster trong vài phút một
   khi được duyệt, khác với nút "Xoá" trash chỉ ảnh hưởng dữ liệu đã bị bỏ).
2. Hệ thống tạo `Incident` (`VOLUME_PERF_SWEEP`) + `Action`
   (`volume_perf_sweep`, luôn **RISKY**, luôn cần duyệt — không có ngoại lệ
   tự động), kèm lệnh xem trước.
3. Vận hành viên bấm **Duyệt** → Dashboard chỉ đổi `Action.status`, Worker
   (`router_client.py::poll_approved_actions`) phát hiện và gọi
   `worker/executor/volume_perf.py::run()`.
4. 4 bước thực thi, theo dõi real-time qua `/api/volumes/{pool}/perf-sweep/progress`:

   | Bước | Nội dung | % |
   |---|---|---|
   | `prepare` | Kiểm tra `fio`, xóa probe cũ nếu có và tạo image mới | 10 |
   | `sweep` | Quét `iodepth` 1→256, 3 mẫu/mức, lấy median | 85 |
   | `diagnostics` | Thu thập QoS notes + bottleneck notes (best-effort) | 95 |
   | `cleanup` | Xóa hẳn scratch image 50 GiB | 100 |

5. Kết quả cuối được lưu bền vào bảng `VolumePerfSweep` (không chỉ nằm
   trong `Action.execution_progress`, vốn chỉ là view tạm thời của một lần
   chạy) — trang Volumes đọc trực tiếp bảng này để vẽ biểu đồ/hiển thị
   knee, qua `/api/volumes/{pool}/perf-sweep/latest`.
6. Vận hành viên có thể bấm thêm **"Phân tích bằng AI"** bất cứ lúc nào sau
   khi sweep `DONE` (không cần quyền admin — chỉ đọc dữ liệu đã có).

## 5. Cách đọc kết quả — tóm tắt thực dụng

- **Có `knee_iodepth`/`knee_iops`:** đây LÀ trần hiệu năng khả dụng đã tìm
  được — mức tải cao nhất còn "đáng đẩy" trước khi latency tăng bất cân
  xứng. Vẫn nên đọc kèm `qos_notes` (loại trừ giới hạn nhân tạo) và
  `bottleneck_notes` (nghi ngờ nghẽn nằm ở OSD nào) trước khi kết luận đây
  là giới hạn của toàn bộ pool/cluster.
- **`knee_iodepth`/`knee_iops` đều `NULL`:** sweep KHÔNG saturate trong toàn
  bộ thang đã thử (tới iodepth=256) — bước cuối cùng trong `steps_json` chỉ
  là một cận dưới đã xác nhận được ("ít nhất chịu được ngần này"), không
  phải trần thật. Cần một phép đo khác (thang `iodepth` cao hơn, hoặc
  `numjobs` lớn hơn) nếu cần biết trần thật.
- Kết quả phản ánh **thời điểm đo** và **trạng thái pool/cluster tại thời
  điểm đó** (bao gồm cả traffic thật khác đang chạy song song, nếu có) —
  không phải một con số cố định vĩnh viễn; nên đo lại sau khi thêm OSD, đổi
  cấu hình, hoặc định kỳ để theo dõi trần có thay đổi theo thời gian không.

## 6. Các file liên quan trong mã nguồn

| File | Vai trò |
|---|---|
| `worker/executor/volume_perf.py` | Toàn bộ logic sweep + thuật toán `_detect_knee` + 3 bước thực thi |
| `dashboard/routes/volumes.py` | Route Dashboard: trang Volumes, đề xuất (`perf-sweep/propose`), theo dõi (`progress`), đọc kết quả (`latest`), gọi phân tích AI (`analyze`) |
| `dashboard/volume_perf_analysis.py` | Gửi kết quả sweep cho LLM, yêu cầu kết luận trần hiệu năng khả dụng bằng tiếng Việt |
| `watcher/volume_monitor.py` | Cơ chế thụ động: rolling window + heuristic latency-knee, tự tạo Incident `VOLUME_SATURATED:*` từ traffic thật |
| `shared/models.py` | `VolumeMetric` (lịch sử mỗi mẫu poll), `VolumePerfSweep` (kết quả sweep bền vững, gồm cả `ai_conclusion`) |
| `worker/executor/commands.py` | `_volume_perf_sweep_preview_command` — lệnh xem trước hiển thị trước khi duyệt |
| `worker/policy/action_policy.yaml` | Xếp `volume_perf_sweep` vào nhóm `volume_perf_action_ids`, luôn RISKY, luôn cần duyệt |
| `tests/test_volume_perf.py`, `tests/test_volume_perf_analysis.py`, `tests/test_dashboard_volumes.py` | Test cho thuật toán knee, phân tích AI, và route Dashboard |
