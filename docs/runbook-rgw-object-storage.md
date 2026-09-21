# Runbook: RGW Object Storage và Multi-site

Runbook này dành cho operator khi Dashboard báo RGW health, Object Storage
metrics hoặc multisite finding. Các bước thu thập evidence bên dưới là
read-only. Không chạy remediation topology, resync hoặc xóa object chỉ vì một
finding đơn lẻ.

## 1. Nguyên tắc an toàn

- Xác nhận đúng cluster trong Dashboard trước khi chạy lệnh.
- Ghi lại thời điểm, RGW host, MON/MGR host và `period epoch`.
- Không đưa access key, secret key, session token hoặc SSH key vào ticket/log.
- Với object version, luôn dùng preview rồi xác nhận đúng mã target; Object Lock,
  retention và Legal Hold không được bypass.
- Một finding là evidence cần xác minh, không phải lệnh sửa tự động.

## 2. Kiểm tra nhanh

Trong `Object Storage → Buckets`, mở hai panel **Request metrics** và
**Service and topology evidence**:

1. Kiểm tra `Evidence` có phải `ready` hay `partial` không.
2. Kiểm tra daemon count/status và endpoint có khớp node RGW dự kiến không.
3. Đọc `evidence gaps`; nếu endpoint chỉ `inferred`, chưa coi đó là endpoint
   đã được xác nhận bởi MGR.
4. Kiểm tra `period`, `master zone`, `sync` và `capacity dependency` trước khi
   kết luận về multisite.

Nếu dữ liệu stale, tải lại sau khi collector hoàn tất. Không suy luận “healthy”
từ một snapshot cũ.

## 3. Evidence read-only trên cluster

Chỉ chạy các lệnh sau bằng quyền vận hành phù hợp và giới hạn output:

```bash
ceph orch ps --service_type rgw --format json
ceph mgr services --format json
ceph config dump --format json
ceph df --format json
radosgw-admin realm get --format json
radosgw-admin zonegroup get --format json
radosgw-admin zone get --format json
radosgw-admin period get --format json
radosgw-admin sync status --format json
radosgw-admin sync error list --format json
```

Không dùng `realm set`, `zone modify`, `period update`, `sync run` hoặc lệnh
tương đương trong bước chẩn đoán.

## 4. Xử lý finding thường gặp

### RGW daemon unavailable

- Đối chiếu `ceph orch ps --service_type rgw` với host trong cấu hình cluster.
- Kiểm tra endpoint inferred/observed và frontend port.
- Nếu deployment legacy systemd, ghi rõ collector chưa có adapter đầy đủ thay
  vì suy luận daemon đang down.
- Chỉ sau khi xác minh bằng service manager và log mới tạo incident vận hành.

### Endpoint unavailable hoặc inferred

- Kiểm tra DNS/routing từ Dashboard hoặc node kiểm tra đến endpoint.
- Đối chiếu `ceph mgr services`, frontend config và port thực tế.
- Không thay endpoint trong Settings chỉ để làm mất cảnh báo.

### Sync lag / not caught up

- Ghi `lag_seconds`, source/target zone, period epoch và thời điểm snapshot.
- Lấy lại `sync status` hai lần cách nhau một khoảng ngắn để phân biệt lag tăng
  với snapshot stale.
- Kiểm tra network và pool dependency trước khi cân nhắc resync; resync cần
  change/approval riêng.

### Shard error hoặc conflict

- Lưu `sync error list` và period evidence cùng một request ID/incident.
- Không tự xóa error, đổi master zone hoặc cập nhật period.
- Escalate cho người quản trị multisite nếu lỗi lặp lại sau lần refresh kế tiếp.

### Period/master state conflict

- Đối chiếu realm, zonegroup, zone và period cùng epoch.
- Kiểm tra zone nào được xác định là master và liệu các site có đang dùng cùng
  period hay không.
- Không chạy `period update`/`period commit` khi chưa có kế hoạch failover.

### Capacity dependency missing

- Collector chỉ map được placement pool khi `ceph df` và zone placement cùng
  cung cấp tên pool.
- Không coi `not_available` là dung lượng còn trống.
- Kiểm tra pool stats riêng và xác nhận replication/EC trước khi dự báo capacity.

## 5. Object version operations

1. Chọn object version từ Object Browser.
2. Nhập S3 endpoint đúng cluster và chạy **Preview**.
3. Đọc target, risk, size, retention/legal hold và mã xác nhận.
4. Chỉ execute nếu target vẫn đúng; server sẽ re-check version và Object Lock.
5. Kiểm tra Object Storage Audit bằng request ID.

Delete bị từ chối khi Object Lock không xác định, retention còn hiệu lực hoặc
Legal Hold bật. Restore version thường tạo version mới; restore delete marker
chỉ xóa marker đã chọn và không gỡ lock của version khác.

## 6. Release acceptance

Trước khi đóng incident hoặc release:

- Chạy regression Object Storage/RGW trong CI.
- Kiểm tra default cluster và secondary cluster scope.
- Kiểm tra RGW unavailable, stale evidence, missing version, Object Lock và
  confirmation mismatch.
- Thực hiện live read-only acceptance trên môi trường được phê duyệt.
- Không thực hiện delete, purge, resync hoặc topology mutation như một phần của
  smoke test tự động.
