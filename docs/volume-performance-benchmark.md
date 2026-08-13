# Xác định hiệu năng tối đa (IOPS ceiling) của Ceph RBD volume/pool

## 1. Câu hỏi cần trả lời

Không thể suy ra trần hiệu năng chỉ từ peak IOPS của traffic thật. Muốn tìm trần khả dụng phải chủ động tăng tải có kiểm soát và tìm **knee**: điểm cuối còn đáng dùng trước khi IOPS gần như phẳng nhưng tail latency tăng bất cân xứng.

Knee không nhất thiết là điểm có IOPS lớn nhất. Nếu sweep chưa tạo được hình dạng knee đủ rõ, kết quả phải là **chưa tìm thấy ceiling**, không được lấy điểm cuối hoặc peak IOPS làm trần.

## 2. Peak quan sát thụ động không phải ceiling

Peak lịch sử chỉ cho biết workload đã từng yêu cầu bao nhiêu IOPS:

- workload nhẹ làm peak thấp hơn khả năng thật;
- recovery, scrub hoặc cạnh tranh tài nguyên làm peak/latency bị nhiễu;
- thay đổi số OSD, CRUSH, PG, QoS hoặc traffic làm ceiling thay đổi.

Biểu đồ lịch sử hữu ích để quan sát vận hành. Load sweep mới là phép đo chủ động dùng để tìm ceiling.

## 3. Phạm vi và an toàn dữ liệu

Ứng dụng không benchmark volume sản xuất. Worker:

1. xoá probe cũ nếu lần chạy trước bị gián đoạn;
2. tạo RBD image `_ceph_aiops_perf_probe` dung lượng logic 50 GiB;
3. chạy fio trực tiếp qua librbd;
4. thu thập chẩn đoán;
5. xoá probe kể cả khi sweep lỗi.

RBD image mặc định là thin-provisioned. Với Ceph Pacific, không dùng `--thin-provision`; tùy chọn tồn tại là `--thick-provision` để làm điều ngược lại.

Scratch image trong một pool phản ánh năng lực khả dụng của pool/cluster tại thời điểm đo. Nó không phải ceiling riêng cố định của từng volume, trừ khi không có tải cạnh tranh và mọi volume dùng cùng đường dữ liệu/chính sách.

Không chạy khi PG không `active+clean`, OSD recovery/backfill, cluster gần đầy hoặc production đang cao tải. Nếu cluster HEALTH_WARN, phải lưu trạng thái đó cùng kết quả.

## 4. Workload chuẩn trong ứng dụng

```bash
fio --name=sweep \
  --ioengine=rbd --pool="$POOL" --rbdname=_ceph_aiops_perf_probe \
  --rw=randwrite --bs=4k --iodepth="$DEPTH" --numjobs=1 \
  --runtime=30 --ramp_time=10 --time_based --direct=1 \
  --invalidate=1 --randrepeat=0 --norandommap --thread=1 \
  --group_reporting --lat_percentiles=1 --percentile_list=99 \
  --output-format=json
```

Dải depth: `1, 2, 4, 8, 16, 32, 64, 128, 256`.

Mỗi depth lấy median ba mẫu. Nếu hệ số biến thiên IOPS lớn hơn 7,5%, hệ thống lấy thêm hai mẫu và dùng median năm mẫu. Ramp 10 giây không tính vào cửa sổ đo 30 giây.

Các chỉ số lưu lại:

| Chỉ số | Nguồn fio | Cách dùng |
|---|---|---|
| IOPS | `jobs[0].write.iops` | Khả năng phục vụ tải |
| Average latency | `clat_ns.mean / 1e6` | Quan sát tổng quát |
| p99 latency | `clat_ns.percentile["99.000000"] / 1e6` | Tín hiệu chính để tìm knee |
| Bandwidth | `bw_bytes / 1024 / 1024` | MiB/s của workload 4K |
| IOPS CV | độ lệch chuẩn / median | Phát hiện mẫu nhiễu |

## 5. Thuật toán knee thực tế

Với mỗi cặp `(prev, cur)`:

```text
iops_growth    = (cur.iops - prev.iops) / prev.iops
latency_growth = (cur.p99  - prev.p99)  / prev.p99
latency_delta  = cur.p99 - prev.p99
```

Transition được xem là dấu hiệu saturation khi đồng thời:

```text
iops_growth < 0.15
AND latency_delta >= 2 ms
AND latency_growth > 3 × max(iops_growth, 0)
```

Khi thấy transition xấu đầu tiên, hệ thống chạy thêm một depth. Điểm xác nhận được so lại với cùng bước tốt cuối cùng; nếu IOPS vẫn plateau và p99 vẫn tăng bất cân xứng so với bước tốt đó, knee được xác nhận. Cách này không bắt latency phải tiếp tục tăng lần thứ hai: latency duy trì trên một plateau cao vẫn xác nhận cliff. Knee là bước tốt cuối cùng trước transition xấu đầu tiên.

### Vì sao p99 ≥ 20 ms không tự tạo knee

`p99 >= 20 ms` là cảnh báo vận hành/SLA. Nó không phải bằng chứng độc lập của ceiling vật lý. Nếu p99 đã 25 ms ở depth 1 nhưng IOPS vẫn tăng gần tuyến tính tới depth 16, kết luận depth 1 là ceiling sẽ sai.

Vì vậy code tách hai khái niệm:

- p99 cao: baseline/SLA đang xấu, cần cảnh báo;
- knee: IOPS plateau **và** p99 tăng bất cân xứng, được xác nhận qua bước tiếp theo.

## 6. Early stop

Sau mỗi depth, Worker chạy lại `_detect_knee()` trên toàn bộ điểm đã có. Khi transition xấu và một depth bổ sung xác nhận vẫn ở phía bên kia cùng điểm tốt cuối, Worker dừng; không tiếp tục tới 256.

Nếu chạy hết depth 256 mà không xác nhận được knee:

- `knee_iodepth` và `knee_iops` là `NULL`;
- điểm cuối chỉ là tải cao nhất đã thử;
- không được gọi điểm cuối là ceiling;
- có thể cần `numjobs > 1` hoặc dải tải cao hơn, nhưng chỉ sau đánh giá an toàn.

## 7. Chẩn đoán bổ sung

Sau sweep, Worker lấy best-effort:

```bash
rbd config image list "$POOL/_ceph_aiops_perf_probe" | grep -i qos
ceph osd perf
iostat -x 1 2
```

QoS có thể tạo ceiling nhân tạo. `ceph osd perf` và `iostat` giúp khoanh vùng OSD/host nghẽn, nhưng được lấy ngay sau tải nặng nhất nên chỉ là bằng chứng gần thời điểm đo, không phải đo đồng thời tuyệt đối.

## 8. Benchmark thực tế ngày 13/08/2026

### 8.1 Môi trường

| Thành phần | Giá trị |
|---|---|
| Server | `10.3.53.136` (`rnd-khiempx-ceph1.novalocal`) |
| Ceph | 16.2.15 Pacific |
| Pool | `volumes`, replicated `size=3`, `min_size=2`, 16 PG |
| OSD | 3 up / 3 in, HDD, tổng raw 60 GiB |
| fio | 3.19, `ioengine=rbd` |
| Probe | `volumes/_ceph_aiops_docs_probe`, 4 GiB logic |
| Thời gian | 14:08:15–14:11:12, Asia/Ho_Chi_Minh |
| Phương pháp xác minh | ramp 5 giây, runtime 15 giây, một mẫu/depth |

Hai volume production có watcher từ `10.3.52.250` nên không được dùng làm đích benchmark. Probe đã được xác nhận xoá sau sweep.

Cụm ở `HEALTH_WARN`: 1/3 MON down, BlueStore legacy OMAP trên `osd.0`, `require_osd_release < pacific`, và hai pool bị cảnh báo thiếu PG. Toàn bộ 49 PG vẫn `active+clean`.

### 8.2 Số liệu thật

| iodepth | IOPS | MiB/s | Avg ms | p99 ms |
|---:|---:|---:|---:|---:|
| 1 | 32,6 | 0,13 | 30,627 | 254,804 |
| 2 | 75,7 | 0,30 | 26,592 | 212,861 |
| 4 | 90,4 | 0,35 | 44,524 | 467,665 |
| 8 | 158,9 | 0,62 | 50,817 | 346,030 |
| 16 | 136,5 | 0,54 | 116,415 | 1.044,382 |
| 32 | 215,4 | 0,85 | 150,099 | 467,665 |
| 64 | 261,4 | 1,04 | 245,367 | 859,832 |
| 128 | 246,2 | 0,99 | 511,046 | 1.166,017 |

### 8.3 Kết luận đúng từ bộ dữ liệu này

Không xác nhận được knee:

- transition 8→16 xấu nhưng depth 32 tăng IOPS mạnh so với điểm tốt depth 8, nên không xác nhận cliff;
- transition 64→128 xấu nhưng không có depth 256 để xác nhận tiếp;
- phép xác minh chỉ có một mẫu/depth, trong khi ứng dụng dùng median ba hoặc năm mẫu.

Do đó kết quả chính xác là:

> **Chưa tìm thấy IOPS ceiling đáng tin cậy. Baseline latency của cụm đang rất xấu và dữ liệu có nhiễu lớn.**

Không được công bố `iodepth=8`, `158,9 IOPS` hay peak `261,4 IOPS` là ceiling. Chúng chỉ là các điểm đã quan sát trong một sweep ngắn trên cluster HEALTH_WARN.

## 9. Hậu kiểm

- Probe `_ceph_aiops_docs_probe` đã được xoá.
- 3/3 OSD vẫn up/in.
- 49 PG vẫn `active+clean`.
- Dung lượng trở về khoảng 15 GiB đã dùng / 45 GiB còn trống.
- Cảnh báo health không thay đổi so với trước benchmark.

## 10. Khuyến nghị đo lại

1. Khôi phục MON `rnd-khiempx-ceph1.novalocal` vào quorum.
2. Xử lý BlueStore legacy OMAP trong maintenance window.
3. Hoàn tất `require_osd_release pacific` sau khi xác minh version.
4. Đánh giá tăng PG `images` và `volumes` từ 16 lên mức Ceph đề xuất.
5. Chạy sweep chuẩn từ giao diện: 30 giây × median 3/5 mẫu.
6. Chỉ công bố ceiling khi tín hiệu đầu tiên được một depth bổ sung xác nhận so với cùng điểm tốt cuối.

## 11. Runbook thủ công an toàn

```bash
POOL=volumes
IMAGE=_ceph_aiops_perf_probe

cleanup() { rbd rm "$POOL/$IMAGE" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

rbd rm "$POOL/$IMAGE" 2>/dev/null || true
rbd create --size 50G "$POOL/$IMAGE"

for DEPTH in 1 2 4 8 16 32 64 128 256; do
  fio --name=sweep \
    --ioengine=rbd --pool="$POOL" --rbdname="$IMAGE" \
    --rw=randwrite --bs=4k --iodepth="$DEPTH" --numjobs=1 \
    --runtime=30 --ramp_time=10 --time_based --direct=1 \
    --invalidate=1 --randrepeat=0 --norandommap --thread=1 \
    --group_reporting --lat_percentiles=1 --percentile_list=99 \
    --output-format=json --output="sweep_depth${DEPTH}.json"
done
```

Runbook minh họa chỉ chạy một mẫu. Muốn kết quả tương đương ứng dụng phải lặp ba lần/depth, tính median và lấy thêm hai mẫu khi IOPS CV vượt 7,5%.
