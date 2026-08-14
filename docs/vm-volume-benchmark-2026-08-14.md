# Kết quả đo hiệu năng Volume từ trong VM — 14/08/2026

Tài liệu này ghi lại phép đo hiệu năng end-to-end thực tế của một volume Ceph
RBD được gắn vào VM OpenStack. Phép đo được thực hiện từ bên trong guest để kết
quả bao gồm toàn bộ đường đi I/O:

```text
ceph-ai (10.3.55.213)
  → OpenStack Controller (10.3.54.79)
  → VM (tuong123@10.1.0.56)
  → /dev/vdb
  → virtio/QEMU → network → Ceph RBD
```

Đây là benchmark **4 KiB random read, direct I/O, read-only**. Phép đo tạo tải
đọc thật nhưng không ghi hoặc thay đổi dữ liệu trên volume.

## 1. Môi trường đo

| Thành phần | Giá trị |
|---|---|
| Server chạy ceph-ai | `10.3.55.213` |
| OpenStack Controller | `10.3.54.79` |
| VM | `10.1.0.56` |
| SSH user trong VM | `tuong123` |
| SSH key của VM trên Controller | `/root/.ssh/id_rsa` |
| Block device | `/dev/vdb` |
| Dung lượng thiết bị | 30 GiB |
| Công cụ | `/usr/bin/fio` |
| Thời gian hoàn tất | 17:22:06, 14/08/2026 (Asia/Ho_Chi_Minh) |
| Trạng thái action | `EXECUTED` |

Do user `tuong123` không được phép mở trực tiếp raw block device, ceph-ai chạy
`fio` bằng `sudo -n`. Tài khoản đã được xác nhận có passwordless sudo và có
quyền đọc `/dev/vdb`.

## 2. Phương pháp đo

Benchmark quét lần lượt các mức `iodepth`: `1`, `4`, `16`, `32`, `64`. Ở mỗi
mức, `fio` chạy ba mẫu độc lập; hệ thống lấy median để giảm ảnh hưởng của nhiễu.

Lệnh tương đương cho mỗi mẫu:

```bash
sudo -n fio \
  --name=ceph-ai-vm-read \
  --readonly \
  --rw=randread \
  --bs=4k \
  --filename=/dev/vdb \
  --ioengine=libaio \
  --direct=1 \
  --iodepth=<IODEPTH> \
  --numjobs=1 \
  --runtime=20 \
  --ramp_time=5 \
  --time_based \
  --group_reporting \
  --lat_percentiles=1 \
  --percentile_list=99 \
  --output-format=json
```

Mỗi mức tải cần khoảng 75 giây: ba lần chạy, mỗi lần gồm 5 giây ramp và 20 giây
đo. Toàn bộ sweep thực tế kéo dài khoảng 6 phút 40 giây.

## 3. Số liệu thực tế

| iodepth | Mẫu | IOPS median | Băng thông | CV IOPS | Latency trung bình | Latency p99 |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 3 | 446.5 | 1.74 MiB/s | 27.49% | 2.167 ms | 33.423 ms |
| 4 | 3 | 2,817.2 | 11.01 MiB/s | 33.83% | 1.361 ms | 25.559 ms |
| 16 | 3 | 11,116.8 | 43.43 MiB/s | 19.68% | 1.393 ms | 31.850 ms |
| 32 | 3 | **12,361.5** | **48.29 MiB/s** | 7.57% | 2.546 ms | 67.633 ms |
| 64 | 3 | 12,304.3 | 48.08 MiB/s | 2.14% | 5.180 ms | 145.752 ms |

### Kết quả chính

- IOPS cao nhất quan sát được: **12,361.5 IOPS** tại `iodepth=32`.
- Băng thông cao nhất: **48.29 MiB/s** tại `iodepth=32`.
- Điểm knee do hệ thống chọn: **`iodepth=16`**, đạt **11,116.8 IOPS** và
  **43.43 MiB/s**, latency p99 **31.85 ms**.
- Trần quan sát được của cấu hình này vào thời điểm đo là khoảng **12.3K IOPS / 48 MiB/s**.
- Mức hiệu năng sử dụng cân bằng hơn là khoảng **11.1K IOPS / 43.4 MiB/s** tại
  `iodepth=16`.

## 4. Diễn giải đường cong

Từ `iodepth=16` lên `iodepth=32`, IOPS chỉ tăng khoảng 11.2%, trong khi p99 tăng
từ 31.85 ms lên 67.63 ms, tức tăng hơn 2.1 lần. Từ `iodepth=32` lên
`iodepth=64`, IOPS giảm nhẹ 0.46%, nhưng:

- latency trung bình tăng từ 2.546 ms lên 5.180 ms, hơn 2 lần;
- latency p99 tăng từ 67.633 ms lên 145.752 ms, hơn 2.1 lần.

Như vậy, tăng queue depth sau mức 16–32 không tạo thêm throughput đáng kể mà
chủ yếu làm I/O chờ lâu hơn. `iodepth=64` đã nằm rõ trong vùng bão hòa.

Không nên dùng duy nhất con số IOPS lớn nhất làm cấu hình vận hành. Với workload
nhạy latency, `iodepth=16` là điểm hợp lý hơn; `iodepth=32` chỉ phù hợp khi cần
ưu tiên throughput và chấp nhận p99 khoảng 68 ms.

### Dashboard xác định hiệu năng tối đa như thế nào?

Dashboard không coi dòng có IOPS lớn nhất là câu trả lời mặc định. Hệ thống tìm
**điểm knee**, tức bước cuối cùng trước khi IOPS bắt đầu plateau nhưng latency
tăng bất cân xứng. Một điểm chỉ được chấp nhận khi mức tải tiếp theo cũng xác
nhận hệ thống vẫn nằm bên kia ngưỡng bão hòa; nhờ vậy một mẫu nhiễu đơn lẻ không
đủ để tạo kết luận.

Trong lần đo này:

1. `iodepth=16` đạt 11,116.8 IOPS với p99 31.85 ms.
2. Tăng lên `iodepth=32` chỉ thêm 11.2% IOPS nhưng p99 tăng 112.3%.
3. Tăng tiếp lên `iodepth=64` không thêm IOPS và p99 lên 145.75 ms.

Vì vậy Dashboard phải trả lời trực tiếp: **hiệu năng tối đa sử dụng được khoảng
11.1K IOPS / 43.43 MiB/s tại iodepth 16**. Con số 12.36K IOPS là đỉnh quan sát,
nhưng không phải mức vận hành cân bằng vì tail latency đã tăng mạnh.

CV IOPS ở các mức thấp khá cao (`19.68%–33.83%`), cho thấy môi trường có nhiễu
hoặc cache/scheduling thay đổi giữa các mẫu. Các mức 32 và 64 ổn định hơn. Nếu
dùng kết quả làm baseline chính thức, nên chạy lại vào khung giờ tải thấp và so
sánh ít nhất ba lượt sweep hoàn chỉnh.

## 5. Các lỗi đã gặp và cách khắc phục

### 5.1. Worker SSH thẳng vào VM và bị timeout

Triệu chứng:

```text
10.1.0.56: failed to execute command: timed out
```

Nguyên nhân là Worker cũ chưa nạp code SSH hai hop. Luồng đúng phải đi qua
Controller `10.3.54.79`. Sau khi cập nhật code và restart Worker, SSH hai hop
hoạt động bình thường.

### 5.2. `fio` không có quyền mở `/dev/vdb`

Triệu chứng:

```text
fio: failed opening blockdev /dev/vdb for size check
error=Permission denied
```

Raw block device chỉ cho phép root hoặc user có quyền tương ứng truy cập. Executor
đã được sửa để chạy kiểm tra thiết bị và `fio` bằng `sudo -n`. Dùng `-n` để lệnh
không treo chờ nhập mật khẩu. Nếu passwordless sudo chưa được cấu hình, action sẽ
dừng sớm và báo rõ:

```text
SSH user cần quyền sudo không mật khẩu để đọc block device
```

## 6. Kiểm tra thủ công trước khi chạy lại

Từ server ceph-ai, kiểm tra đúng hai hop:

```bash
ssh -i /root/.ssh/ceph_aiops_watcher root@10.3.54.79
ssh -i /root/.ssh/id_rsa tuong123@10.1.0.56
```

Trong VM:

```bash
command -v fio
sudo -n true
sudo -n test -b /dev/vdb
sudo -n lsblk -dn -o NAME,SIZE,TYPE,RO /dev/vdb
```

Kết quả mong đợi:

```text
/usr/bin/fio
vdb   30G disk 0
```

## 7. Phạm vi và giới hạn của kết quả

- Kết quả phản ánh hiệu năng **end-to-end tại thời điểm đo**, không phải cam kết
  cố định của Ceph cluster hoặc volume.
- Kết quả chịu ảnh hưởng của tải đồng thời trên VM, compute node, network, Ceph
  client, OSD và pool.
- Đây là phép đo random read 4 KiB với một job; không đại diện cho sequential
  throughput, write performance hoặc workload nhiều job.
- Benchmark là read-only nhưng vẫn tạo tải thật. Không nên chạy đồng thời với
  tác vụ production nhạy latency.
- Muốn phân biệt bottleneck trong guest/compute/network với giới hạn Ceph thuần,
  cần đối chiếu thêm benchmark `ioengine=rbd` trực tiếp phía Ceph và số liệu
  `ceph osd perf`, `iostat`, network telemetry trong cùng khoảng thời gian.

## 8. Kết luận

Trong lần đo ngày 14/08/2026, volume `/dev/vdb` trên VM `10.1.0.56` đạt đỉnh
quan sát khoảng **12.3K IOPS và 48.3 MiB/s**. Tuy nhiên, latency tăng mạnh khi
đẩy queue depth từ 16 lên 32 và 64. Vì vậy, khoảng **11.1K IOPS tại
`iodepth=16`** là mức hiệu năng sử dụng hợp lý hơn cho workload cần cân bằng
giữa throughput và tail latency.
