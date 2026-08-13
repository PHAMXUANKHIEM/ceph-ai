# Lý thuyết đo hiệu năng từ bên trong VM sử dụng Ceph RBD

## 1. Phạm vi

Muốn đo **hiệu năng thực tế mà VM nhận được**, workload benchmark phải được
phát từ bên trong VM và kết quả chính phải được thu tại VM. Đây là phép đo
end-to-end, vì I/O đi qua guest OS, virtual block device, QEMU/KVM, librbd,
network và cuối cùng mới tới Ceph.

Trong ceph-ai, trang **Ceph → Volumes** hiện theo dõi hiệu năng của từng RBD
image. Khi một RBD image được gắn làm disk cho VM, số liệu này chỉ phản ánh
**backend storage nhìn từ phía Ceph**. Đây là số liệu hỗ trợ chẩn đoán, không
phải phép đo hiệu năng VM.

Muốn kết luận VM chậm do đâu nên thu thập ở hai phía, nhưng vai trò khác nhau:

1. **Trong VM (kết quả chính):** chủ động phát workload, đo IOPS, throughput và
   latency mà VM thực nhận.
2. **Trong Ceph (đối chiếu):** quan sát RBD image, pool, PG, OSD và thiết bị vật
   lý để giải thích kết quả hoặc tìm bottleneck.

> **Nguyên tắc:** chạy `fio` trực tiếp qua `ioengine=rbd` trên node Ceph chỉ đo
> pool/RBD. Kết quả đó không được ghi là “hiệu năng VM”.

Tài liệu này trình bày phương pháp đo và cách diễn giải. Hướng dẫn chi tiết về
tính năng Load Sweep hiện có nằm tại
[volume-max-performance.md](volume-max-performance.md).

## 2. Mô hình đường đi của một I/O

```text
Ứng dụng trong VM
  → filesystem / page cache của guest
  → virtual block device (virtio-blk hoặc virtio-scsi)
  → QEMU/KVM + librbd trên compute node
  → mạng client/public của Ceph
  → primary OSD → replica OSD
  → BlueStore → thiết bị lưu trữ
```

Độ trễ ứng dụng quan sát được là tổng thời gian ở các lớp trên. Vì vậy latency
trong VM thường cao hơn latency do Ceph báo. Nếu Ceph nhanh nhưng VM vẫn chậm,
bottleneck có thể nằm ở guest, hypervisor, CPU scheduling, queue của virtual
disk hoặc network. Nếu latency VM và Ceph cùng tăng, cần điều tra pool/OSD và
thiết bị vật lý.

## 3. Các chỉ số nền tảng

### 3.1. IOPS

IOPS là số thao tác đọc/ghi hoàn tất trong một giây:

```text
IOPS tổng = read IOPS + write IOPS
```

IOPS chỉ có ý nghĩa khi công bố cùng block size, tỷ lệ read/write, kiểu truy
cập random/sequential và concurrency. Ví dụ 20.000 IOPS với block 4 KiB không
tương đương 20.000 IOPS với block 64 KiB.

### 3.2. Throughput

Throughput là lượng dữ liệu hoàn tất trong một giây, thường dùng MiB/s:

```text
Throughput xấp xỉ IOPS × block size
```

Workload block nhỏ thường quan tâm IOPS; workload tuần tự block lớn thường
quan tâm throughput.

### 3.3. Latency

Latency là thời gian hoàn tất một I/O. Cần lưu cả:

- trung bình (average/mean) để nhìn xu hướng tổng thể;
- p95, p99 hoặc p99.9 để thấy các I/O chậm ở phần đuôi;
- đọc và ghi riêng, vì replicated write thường có đường đi dài hơn read.

Không nên chỉ báo cáo latency trung bình: một số ít I/O rất chậm có thể làm
ứng dụng bị giật nhưng bị trung bình che khuất.

### 3.4. Queue depth và concurrency

Queue depth là số I/O đang chờ hoặc đang được xử lý đồng thời. Tăng queue depth
thường làm IOPS tăng cho tới khi tài nguyên bão hòa; sau đó IOPS gần như không
tăng nhưng latency tăng nhanh. Vùng chuyển tiếp này gọi là **điểm knee** và là
cách thực tế để xác định trần hiệu năng khả dụng.

Theo Little's Law, ở trạng thái ổn định có thể kiểm tra gần đúng:

```text
I/O đang thực hiện ≈ IOPS × latency (giây)
```

### 3.5. Saturation và utilization

- **Utilization** cho biết tài nguyên bận đến mức nào.
- **Saturation** cho biết công việc đã bắt đầu xếp hàng hay chưa.

Thiết bị `%util` cao, `await` tăng, queue dài, OSD commit/apply latency tăng và
RBD p99 tăng đồng thời là bằng chứng mạnh hơn một metric đơn lẻ.

## 4. Phép đo chính và dữ liệu đối chiếu

### 4.1. Phép đo chính: benchmark từ trong VM

Mục tiêu là trả lời: “Ứng dụng trong VM thực sự nhận được bao nhiêu IOPS,
throughput và latency?”. `fio` phải được cài và chạy bên trong guest, nhắm vào
một disk hoặc file test thuộc VM đó.

Ví dụ một profile đọc ngẫu nhiên, chạy trên file test riêng:

```bash
fio --name=vm-randread \
  --filename=/mnt/perf-test/fio.data \
  --size=10G --rw=randread --bs=4k \
  --ioengine=libaio --direct=1 \
  --iodepth=32 --numjobs=1 \
  --runtime=60 --ramp_time=10 --time_based \
  --group_reporting --output-format=json
```

Đây chỉ là profile minh họa; `size`, `rw`, `bs`, `iodepth` và thời lượng phải
phù hợp workload thực tế. File test phải nằm trên disk cần đo, working set nên
lớn hơn cache nếu mục tiêu là storage, và tuyệt đối không trỏ write benchmark
vào block device/filesystem đang chứa dữ liệu cần bảo toàn.

Kết quả chính lấy từ JSON của `fio` trong VM: read/write IOPS, bandwidth,
completion latency trung bình và p95/p99, số lỗi I/O và độ ổn định giữa các lần
chạy. Trong lúc đo, thu thêm `iostat -x`, CPU `iowait`/`steal`, RAM/swap và
network trong guest.

### 4.2. Dữ liệu đối chiếu: monitor từ Ceph

Mục tiêu là trả lời: “RBD backend của disk VM đang hoạt động thế nào và có dấu
hiệu bão hòa phía Ceph ngay lúc này không?”.

ceph-ai lấy mẫu bằng:

```bash
rbd perf image iostat <pool> --format json
```

Watcher chuẩn hóa và lưu theo `(cluster, pool, image, thời điểm)` các trường:

- tổng IOPS đọc + ghi;
- read latency và write latency, đơn vị ms;
- cờ bão hòa tại thời điểm lấy mẫu.

Trang **Volumes** hiển thị lịch sử và peak từng quan sát được. Peak này chỉ là
mức tải VM đã từng tạo ra, **không phải năng lực tối đa của volume**.

Heuristic bão hòa hiện tại của ceph-ai dùng cửa sổ 12 mẫu gần nhất. Một mẫu
đáng ngờ khi đồng thời:

```text
IOPS hiện tại ≥ 90% peak IOPS trong cửa sổ
latency hiện tại ≥ 2 × median latency trong cửa sổ
```

Phải có 3 poll đáng ngờ liên tiếp mới tạo Incident
`VOLUME_SATURATED:<pool>/<image>`. Đây là phát hiện theo baseline gần đây của
chính image, không phải chứng minh tuyệt đối rằng cluster đã đạt trần vật lý.

Ưu điểm của quan sát thụ động là không tạo thêm tải và phản ánh workload thật.
Nhược điểm là không xác định được trần nếu VM chưa từng phát đủ tải; image không
có I/O gần đây cũng có thể không xuất hiện trong kết quả live.

### 4.3. Load Sweep phía Ceph không phải benchmark VM

Mục tiêu là trả lời: “Với một workload đã định nghĩa, hệ thống chịu được bao
nhiêu tải trước khi latency tăng bất cân xứng?”.

Có hai vị trí chạy benchmark, phục vụ hai câu hỏi khác nhau và không thể dùng
thay cho nhau:

| Vị trí chạy | Kết quả phản ánh | Khi nên dùng |
|---|---|---|
| `fio` trong VM, trên disk/filesystem cần kiểm tra | End-to-end: guest, hypervisor, librbd, network và Ceph | Xác nhận trải nghiệm thực tế của VM |
| `fio` dùng `ioengine=rbd` trên scratch image | Năng lực RBD/pool, bỏ qua phần lớn guest filesystem và virtual device | So sánh pool/cluster, tìm knee phía storage |

Tính năng **Đo hiệu năng tối đa (Load Sweep)** hiện có của ceph-ai thuộc loại
thứ hai.
Nó tạo scratch image riêng, chạy 4 KiB random write với `iodepth` tăng từ 1
đến 256, đo 3 lần mỗi mức và lấy median. Nó không benchmark trực tiếp disk
production của VM. Kết quả Load Sweep chỉ nên dùng làm baseline của pool hoặc
cluster và làm bằng chứng đối chiếu với kết quả `fio` chạy trong VM.

## 5. Quy trình bắt buộc để đo end-to-end cho một VM

### Bước 1 — Xác định đúng đối tượng

Lập ánh xạ rõ ràng:

```text
VM → virtual disk → pool/RBD image → pool policy → acting OSD
```

Không suy luận chỉ từ tên nếu nền tảng cloud có lớp ánh xạ riêng. Với VM nhiều
disk, đo từng disk vì boot disk và data disk có thể nằm ở pool khác nhau.

### Bước 2 — Ghi lại điều kiện trước khi đo

Ít nhất phải lưu:

- cấu hình vCPU/RAM, loại virtual disk/controller và cache mode;
- phiên bản kernel, filesystem, mount options và I/O scheduler trong guest;
- pool, replication/EC profile, số PG, loại và số lượng OSD;
- Ceph health, recovery/backfill, scrub/deep-scrub;
- QoS ở VM, image, pool hoặc hạ tầng cloud;
- tải nền từ các VM khác và trạng thái network.

Không so sánh hai kết quả nếu các điều kiện trên khác nhau mà không ghi chú.

### Bước 3 — Chọn workload đại diện

Một bộ đo tối thiểu thường gồm:

| Profile | Ý nghĩa tham khảo |
|---|---|
| 4 KiB random read | Cache/database read, IOPS đọc |
| 4 KiB random write | Ghi nhỏ, nhạy với replication và journal |
| 4 KiB randrw theo tỷ lệ thực tế | Workload hỗn hợp của ứng dụng |
| 1 MiB sequential read/write | Throughput cho backup, scan hoặc file lớn |

Chọn working set lớn hơn cache nếu mục tiêu là storage vật lý. Dùng direct I/O
khi cần tránh page cache; nếu ứng dụng thực tế phụ thuộc cache thì thực hiện
thêm một bài đo có cache và ghi rõ đó là kết quả khác.

### Bước 4 — Warm-up, đo lặp và tăng tải

1. Warm-up để cấp phát block, làm nóng đường dữ liệu và loại giai đoạn khởi động.
2. Giữ cố định block size, read/write mix và số job.
3. Tăng concurrency theo thang, ví dụ `iodepth = 1, 2, 4, 8, ...`.
4. Chạy mỗi mức đủ lâu, lặp ít nhất 3 lần và dùng median.
5. Ghi IOPS, bandwidth, average latency, p95/p99 và CPU của tiến trình tạo tải.
6. Dừng nếu latency vượt ngưỡng an toàn hoặc ảnh hưởng workload production.

Không chạy destructive write test lên filesystem/disk có dữ liệu. Dùng disk
test riêng hoặc file test đã giới hạn phạm vi; vẫn phải có phê duyệt vận hành.

### Bước 5 — Thu thập đồng thời ở các lớp

Trong VM theo dõi `iostat -x`, CPU steal/iowait, memory/swap và network. Trên
compute node theo dõi CPU QEMU, virtual-disk queue và network. Trên Ceph theo
dõi RBD image, pool/PG, `ceph osd perf`, slow ops và `iostat -x` của thiết bị
OSD. Đồng bộ timestamp giữa các node để các spike có thể đối chiếu chính xác.

### Bước 6 — Tìm điểm knee

Vẽ IOPS và p99 latency theo concurrency. Trần khả dụng là điểm tốt cuối cùng
trước khi:

1. IOPS bắt đầu plateau; và
2. p99 latency tăng mạnh, không tương xứng với phần IOPS tăng thêm.

Nên có thêm một mức tải xác nhận để tránh kết luận từ nhiễu. Nếu hết dải thử mà
không thấy knee, kết luận đúng là “chưa tìm thấy trần trong phạm vi đã thử”; điểm
cao nhất chỉ là cận dưới, không phải ceiling.

### Bước 7 — Đo lại để xác nhận

Đo baseline khi cluster yên, đo trong khung giờ tải thật và lặp lại sau thay đổi
cấu hình. Một kết quả đơn lẻ không đại diện vĩnh viễn cho VM hoặc cluster.

## 6. Cách khoanh vùng bottleneck

| Quan sát | Khả năng cần kiểm tra |
|---|---|
| Guest latency tăng, Ceph latency ổn định | CPU steal, guest queue, filesystem, cache mode, hypervisor hoặc network client |
| Guest và RBD latency cùng tăng | Pool/PG/OSD, network Ceph hoặc thiết bị lưu trữ |
| Một VM chậm, VM khác cùng pool bình thường | QoS, compute node, đường mạng, cấu hình hoặc workload riêng của VM |
| Nhiều VM cùng pool chậm đồng thời | Shared pool/OSD/network, recovery, scrub hoặc noisy neighbor |
| IOPS đứng đúng một mức, latency không tạo knee tự nhiên | QoS/throttle có thể đang giới hạn |
| p99 xấu ngay từ tải thấp nhưng IOPS vẫn tăng tuyến tính | SLA/baseline đã xấu; chưa đủ bằng chứng cluster bão hòa |
| `%util`/`await` cao trên một OSD, các OSD khác bình thường | OSD hoặc thiết bị lệch/chậm, phân bố dữ liệu không đều |

Correlation không tự chứng minh nguyên nhân. Kết luận cần nhiều tín hiệu cùng
timestamp và, nếu có thể, một phép thử lặp sau khi cô lập yếu tố nghi ngờ.

## 7. Cách báo cáo kết quả

Một báo cáo hợp lệ phải trả lời được:

- đo VM/disk/image nào, trên cluster và pool nào;
- đo từ trong guest hay trực tiếp trên RBD;
- profile: block size, random/sequential, read/write mix, jobs, iodepth;
- direct/buffered I/O, working-set size, warm-up và thời lượng;
- IOPS, MiB/s, average và p99 latency tại từng mức tải;
- knee hoặc câu “chưa tìm thấy knee trong dải thử”;
- Ceph health, recovery/scrub, QoS và tải cạnh tranh lúc đo;
- bottleneck nghi ngờ, bằng chứng và mức độ tin cậy;
- sai số giữa các lần lặp và thời điểm cần đo lại.

Không viết “VM đạt tối đa X IOPS” nếu phép đo chỉ chạy trên scratch image phía
Ceph. Cách diễn đạt đúng là: “Pool đạt khoảng X IOPS với profile Y tại thời
điểm Z” hoặc “disk VM đạt X IOPS end-to-end với profile Y”.

## 8. Thao tác đo VM trên ceph-ai

Tại **Ceph → Volumes → Đo hiệu năng từ trong VM**, tài khoản admin nhập:

- IP của VM;
- SSH user;
- đường dẫn tuyệt đối tới SSH private key trên máy chạy Worker;
- block device trong VM, với gợi ý `/dev/vdb`, `/dev/vdc`, `/dev/vdd`,
  `/dev/sdb` hoặc `/dev/nvme1n1`.

Sau khi được duyệt, Worker SSH vào VM, xác minh `fio` và block device rồi chạy
4 KiB random-read, direct I/O tại nhiều mức iodepth. Mỗi mức chạy 3 mẫu và lấy
median. Giao diện hiển thị IOPS, MiB/s, average latency, p99 và độ lệch mẫu.

Phép đo hiện tại là **read-only**. Nó không ghi lên raw device nhưng vẫn tạo tải
đọc thật và có thể ảnh hưởng workload. SSH user phải có quyền đọc block device,
thường là `root`. ceph-ai không nhận hoặc lưu nội dung private key qua form; form
chỉ nhận đường dẫn tới key đã được quản trị viên đặt sẵn trên Worker.

## 9. Giới hạn hiện tại của ceph-ai

- Monitor hiện có nhận diện RBD image, không tự bảo đảm ánh xạ image đó về tên
  VM/instance ở OpenStack, Proxmox hay libvirt.
- `rbd perf image iostat` đo phía Ceph và không cung cấp CPU/RAM/application
  latency trong guest.
- Tổng IOPS được lưu nhưng read IOPS và write IOPS chưa được lưu tách riêng.
- Heuristic thụ động dùng trạng thái rolling window trong bộ nhớ; Watcher
  restart thì cần đủ mẫu để warm-up lại.
- Load Sweep đo scratch image của pool, không phải volume production cụ thể và
  kết quả chịu ảnh hưởng bởi tải dùng chung tại thời điểm chạy.
- Benchmark VM hiện chỉ đo random read 4 KiB. Muốn đo write an toàn cần gắn một
  scratch disk riêng, xác nhận disk không chứa dữ liệu rồi mới bổ sung profile
  ghi; không được chạy raw-write lên disk production.
- Cơ chế monitor RBD được ghi chú trong mã nguồn là chưa xác minh đầy đủ với
  output cluster thật; cần chạy pilot và đối chiếu đơn vị/schema trước khi dùng
  ngưỡng để cam kết SLA.

## 10. Kết luận

ceph-ai có thể monitor **hiệu năng storage của VM trên Ceph RBD**, nhưng không
thể chỉ từ số liệu đó kết luận hiệu năng tổng thể của VM. Phương pháp đáng tin
cậy là đo end-to-end trong VM, thu thập đồng thời phía Ceph, dùng cùng một
workload được mô tả rõ và xác định trần bằng quan hệ IOPS–p99 latency thay vì
lấy peak IOPS đơn lẻ.
