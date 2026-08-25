# Lab drill: LARGE_OMAP_OBJECTS tự tạo, tự sửa, tự học

Chạy trên AI server:

```bash
bash scripts/lab/large_omap_training.sh 10.3.53.1 test-large-omap us-east-1.rgw.buckets.index
```

Harness chỉ chấp nhận bucket `test-*` và Ceph admin host trong mạng `10.3.x.x`.
Nó lưu cấu hình ban đầu, tắt dynamic resharding, hạ ngưỡng OMAP, đưa bucket về
1 shard, tìm object và PG thật rồi deep-scrub để tạo cảnh báo. Sau thời điểm phát
hiện, harness không chạy lệnh sửa: watcher/worker phải tự tính shard, reshard và
deep-scrub. Cuối cùng harness đối chiếu `existing_header`, `calculated_header`, số
object ban đầu, số shard và `ceph health detail`, rồi luôn khôi phục cấu hình bằng
`trap` kể cả khi bài test thất bại.

Biến tùy chọn:

- `TEST_THRESHOLD` mặc định `5000`.
- `TIMEOUT_SECONDS` mặc định `600`.

Không chạy với bucket chứa dữ liệu thật.
