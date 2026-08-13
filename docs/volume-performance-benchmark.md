# Xác định hiệu năng tối đa (IOPS ceiling) của Ceph RBD volume/pool

## 1. Câu hỏi cần trả lời

Làm sao biết một RBD volume hoặc pool Ceph đã đạt hiệu năng tối đa?

Câu trả lời ngắn gọn: không thể biết chỉ bằng cách quan sát traffic đang chạy. Phải chủ động tăng tải có kiểm soát, đo IOPS cùng tail latency và tìm **điểm gối** (*knee*) của đường cong IOPS–latency.

Knee là điểm cuối cùng còn đáng sử dụng trước khi:

- tăng thêm tải không tạo ra nhiều IOPS hơn;
- latency, đặc biệt p99, tăng nhanh bất cân xứng.

Đây là **trần hiệu năng khả dụng**, không nhất thiết là IOPS lớn nhất xuất hiện trong toàn bộ phép đo.

## 2. Vì sao peak IOPS quan sát được không phải ceiling

Theo dõi thụ động chỉ trả lời: “workload đã từng sử dụng tối đa bao nhiêu IOPS?”. Nó không trả lời: “hạ tầng có thể chịu tối đa bao nhiêu IOPS?”.

Nếu ứng dụng chưa từng phát đủ tải, peak lịch sử sẽ thấp hơn khả năng thật. Nếu peak xuất hiện lúc recovery, scrub hoặc có workload khác cạnh tranh, kết quả lại phản ánh điều kiện bất thường.

Muốn tìm ceiling cần phép đo chủ động:

1. tạo tải nhẹ;
2. tăng tải từng bước;
3. đo phản ứng IOPS và latency;
4. tìm nơi IOPS bắt đầu plateau nhưng latency tăng mạnh;
5. xác nhận bằng một bước tải bổ sung.

## 3. Hiểu đúng đối tượng được đo

Một scratch image trong pool không tạo ra “ceiling riêng vĩnh viễn” cho image đó. Kết quả phản ánh năng lực khả dụng của toàn bộ đường I/O tại thời điểm đo:

```text
fio → librbd → network → primary OSD → replica OSD → storage media
```

Kết quả chịu ảnh hưởng bởi:

- số lượng và loại OSD;
- replication/erasure coding;
- CRUSH placement và số PG;
- network client/cluster;
- recovery, backfill, scrub;
- QoS của image/pool;
- traffic đồng thời;
- block size, read/write mix, numjobs và iodepth.

Vì vậy ceiling luôn phải đi kèm workload profile và điều kiện cluster lúc đo.

## 4. Nguyên tắc an toàn

Không benchmark random-write trên volume chứa dữ liệu thật. Dùng một scratch image riêng trong cùng pool:

```bash
rbd create <pool>/perf_probe --size 50G
```

RBD image mặc định là thin-provisioned; không cần tùy chọn `--thin-provision`.

Scratch image nên được xoá sau mỗi sweep để:

- không giữ dữ liệu random-write;
- tránh lần đo sau dùng lại vùng đã cấp phát;
- bảo đảm mỗi lần đo bắt đầu từ trạng thái xác định.

Không chạy sweep khi:

- PG không `active+clean`;
- OSD down/out hoặc đang recovery/backfill;
- cluster gần đầy;
- đang scrub/deep-scrub diện rộng;
- production đang ở giờ cao tải;
- cluster mất quorum hoặc có lỗi health ảnh hưởng đường I/O.

## 5. Phương pháp load sweep

### 5.1 Tăng tải bằng iodepth

Giữ nguyên workload và tăng queue depth theo cấp số nhân:

```text
1, 2, 4, 8, 16, 32, 64, 128, 256
```

Thang nhân đôi bao phủ dải tải rộng với ít bước. Khi đã tìm thấy vùng knee, có thể chạy sweep tinh hơn quanh vùng đó nếu cần.

### 5.2 Workload chuẩn

```bash
fio --name=sweep \
  --ioengine=rbd --pool="$POOL" --rbdname=perf_probe \
  --rw=randwrite --bs=4k \
  --iodepth="$DEPTH" --numjobs=1 \
  --ramp_time=10 --runtime=30 --time_based \
  --direct=1 --group_reporting \
  --lat_percentiles=1 --percentile_list=99 \
  --output-format=json
```

Ý nghĩa các lựa chọn:

| Tham số | Mục đích |
|---|---|
| `--rw=randwrite --bs=4k` | Workload nhạy với giới hạn IOPS và tail latency |
| `--ioengine=rbd` | Đi trực tiếp qua librbd, không qua filesystem |
| `--direct=1` | Không để page cache che hiệu năng storage |
| `--ramp_time=10` | Bỏ giai đoạn warm-up khỏi cửa sổ đo |
| `--runtime=30 --time_based` | Tạo đủ mẫu để percentile ổn định hơn |
| `--numjobs=1` | Chỉ thay đổi iodepth, tránh đổi nhiều biến cùng lúc |
| `--percentile_list=99` | Thu p99 completion latency |

Workload 4K random-write không đại diện cho mọi ứng dụng. Muốn đánh giá workload đọc, tuần tự hoặc block lớn phải chạy profile riêng; không dùng ceiling của profile này thay cho profile khác.

## 6. Vì sao dùng p99 thay vì chỉ dùng average latency

Average latency dễ che tail latency. Ví dụ, phần lớn I/O vẫn nhanh nhưng một nhóm nhỏ bắt đầu chờ rất lâu thì average chỉ tăng nhẹ, trong khi người dùng thực tế đã gặp request chậm.

p99 trả lời: 99% I/O hoàn tất nhanh hơn giá trị này và 1% còn lại chậm hơn. Khi queue bắt đầu bão hoà, p99 thường xấu đi trước và rõ hơn average.

Nên lưu cả hai:

- average để quan sát xu hướng tổng thể;
- p99 làm tín hiệu chính để phát hiện knee.

## 7. Giảm nhiễu trước khi tìm knee

Một mẫu ở mỗi depth không đủ tin cậy trên cluster đang hoạt động. Nên:

1. đo ít nhất ba lần/depth;
2. dùng median IOPS, median average latency và median p99;
3. tính hệ số biến thiên IOPS:

```text
CV (%) = standard_deviation(IOPS samples) / median(IOPS samples) × 100
```

Nếu CV vượt ngưỡng cho phép, lấy thêm mẫu. Ví dụ ứng dụng hiện tại lấy thêm hai mẫu khi CV lớn hơn 7,5%, rồi dùng median của năm mẫu.

Median giảm ảnh hưởng của một spike recovery hoặc network ngắn hạn tốt hơn arithmetic mean.

## 8. Thuật toán tìm knee

### 8.1 Tính mức tăng giữa hai bước

Với hai điểm liên tiếp `prev` và `cur`:

```text
iops_growth = (cur.iops - prev.iops) / prev.iops

latency_growth =
    (cur.latency_p99_ms - prev.latency_p99_ms)
    / prev.latency_p99_ms

latency_delta_ms =
    cur.latency_p99_ms - prev.latency_p99_ms
```

### 8.2 Xác định transition xấu

Một transition là ứng viên saturation khi đồng thời:

```text
A. iops_growth < 0.15

B. latency_delta_ms >= 2

C. latency_growth > 3 × max(iops_growth, 0)
```

Ý nghĩa:

- A: IOPS tăng dưới 15%, bắt đầu plateau;
- B: latency phải tăng đủ lớn để không bắt nhiễu vài phần nhỏ millisecond;
- C: latency tăng nhanh hơn ít nhất ba lần lợi ích IOPS nhận được.

Nếu IOPS giảm, `max(iops_growth, 0)` bằng 0. Khi đó transition vẫn cần latency tăng thực sự ít nhất 2 ms mới được xem là xấu.

### 8.3 Knee nằm ở đâu

Khi transition `prev → cur` là xấu, `prev` là **ứng viên knee** vì đó là điểm cuối trước cliff latency.

Không trả kết quả ngay. Chạy thêm một depth và so điểm xác nhận với cùng `prev`:

- nếu IOPS vẫn plateau và p99 vẫn xấu bất cân xứng so với `prev`, xác nhận `prev` là knee;
- nếu IOPS phục hồi mạnh hoặc latency trở lại bình thường, coi tín hiệu đầu là nhiễu và tiếp tục sweep.

So với cùng điểm tốt cuối rất quan trọng. Latency có thể nhảy lên một plateau cao rồi giữ nguyên ở bước xác nhận; không cần bắt nó tăng mạnh lần thứ hai mới công nhận cliff.

### 8.4 Pseudocode

```python
def bad_transition(prev, cur):
    iops_growth = (cur.iops - prev.iops) / prev.iops
    latency_growth = (cur.p99 - prev.p99) / prev.p99
    latency_delta = cur.p99 - prev.p99

    return (
        iops_growth < 0.15
        and latency_delta >= 2.0
        and latency_growth > 3.0 * max(iops_growth, 0.0)
    )


def detect_knee(steps):
    for i in range(1, len(steps)):
        candidate = steps[i - 1]
        first_bad = steps[i]

        if not bad_transition(candidate, first_bad):
            continue

        if i + 1 >= len(steps):
            return None  # cần thêm một depth xác nhận

        confirmation = steps[i + 1]
        if bad_transition(candidate, confirmation):
            return candidate

    return None
```

## 9. Không dùng ngưỡng latency tuyệt đối để suy ra ceiling

Một ngưỡng như `p99 >= 20 ms` hữu ích để cảnh báo SLA hoặc dừng phép đo vì an toàn. Tuy nhiên nó không đủ để chứng minh ceiling vật lý.

Nếu p99 đã cao ngay tại depth 1 nhưng IOPS vẫn tăng gần tuyến tính qua các depth tiếp theo, kết luận depth 1 là ceiling là sai. Tình huống đó cho biết baseline của cluster đã xấu, không cho biết đường cong đã plateau.

Phải tách rõ:

- **latency/SLA violation:** dịch vụ đang chậm hơn mục tiêu;
- **IOPS knee:** tăng tải không còn tạo thêm IOPS tương xứng và làm p99 xấu đi.

Một hệ thống có thể vi phạm SLA trước khi đạt ceiling vật lý. Khi đó “ceiling theo SLA” và “ceiling vật lý” là hai kết quả khác nhau.

## 10. Early stop

Sau mỗi depth:

1. chạy lại thuật toán trên tất cả điểm đã đo;
2. nếu mới có một transition xấu, chạy thêm đúng một depth;
3. nếu bước bổ sung xác nhận cùng knee, dừng;
4. nếu không xác nhận, tiếp tục thang tải.

Early-stop giảm thời gian gây tải cho cluster nhưng vẫn tránh kết luận từ một điểm nhiễu.

## 11. Trường hợp không tìm thấy knee

Nếu chạy hết dải thử nghiệm mà không xác nhận được knee:

```text
knee_iodepth = NULL
knee_iops = NULL
```

Điều này không có nghĩa hệ thống không có trần. Nó chỉ có nghĩa cluster chưa bão hoà rõ ràng trong phạm vi đã thử hoặc dữ liệu quá nhiễu để xác nhận.

Điểm cuối chỉ là:

> Tải cao nhất đã được thử thành công trong phép đo này.

Nó không phải ceiling. Có thể cần đo lại trong điều kiện ổn định hơn, tăng `numjobs`, mở rộng dải tải hoặc chạy sweep tinh quanh vùng nghi ngờ.

## 12. Kiểm tra nguyên nhân trước khi kết luận

### 12.1 QoS

```bash
rbd config image list "$POOL/perf_probe" | grep -i qos
```

Nếu image/pool có `rbd_qos_iops_limit` hoặc `rbd_qos_bps_limit`, knee có thể là giới hạn nhân tạo thay vì giới hạn vật lý.

### 12.2 OSD và thiết bị

```bash
ceph osd perf
iostat -x 1 2
```

Các chỉ số cần chú ý:

- commit/apply latency bất thường trên một OSD;
- `%util` gần 100%;
- `await` tăng mạnh;
- queue sâu trên một thiết bị;
- một OSD chậm kéo cả replicated write xuống.

Các lệnh lấy sau sweep chỉ là bằng chứng gần thời điểm tải. Muốn phân tích nguyên nhân chính xác hơn cần thu thập đồng thời trong lúc fio chạy.

## 13. Cách diễn giải kết quả

| Kết quả | Cách hiểu |
|---|---|
| Có knee | Điểm cuối còn hiệu quả trước cliff latency trong workload và điều kiện đã đo |
| Không có knee | Chưa xác nhận được ceiling trong dải thử nghiệm |
| p99 cao nhưng không có knee | Baseline/SLA xấu, chưa đủ bằng chứng về ceiling vật lý |
| Knee trùng QoS cap | Có thể là ceiling nhân tạo |
| Kết quả dao động mạnh | Điều kiện đo không ổn định; cần đo lại hoặc tăng số mẫu |

Luôn công bố cùng kết quả:

- Ceph health;
- profile fio;
- số mẫu và cách tổng hợp;
- trạng thái recovery/scrub;
- tải cạnh tranh;
- QoS;
- cấu hình pool và OSD.

## 14. Kết luận

Trần hiệu năng khả dụng của Ceph RBD không phải peak IOPS. Nó là điểm knee được xác định từ hai tín hiệu đi cùng nhau:

1. IOPS bắt đầu plateau;
2. p99 latency tăng bất cân xứng.

Knee phải được xác nhận bằng một bước tải bổ sung, trên scratch image, với nhiều mẫu và trong điều kiện cluster ổn định. Nếu không có đủ bằng chứng, kết luận đúng là “chưa tìm thấy ceiling”, không phải lấy điểm IOPS cao nhất làm trần.
