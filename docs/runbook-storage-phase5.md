# Storage / Backup Phase 5 Runbook

Phạm vi của runbook này là Block Storage, Backup/Restore và Object Storage trên
đúng cluster đang được chọn. Mọi kết quả phải ghi cluster ID, thời điểm thu thập
và trạng thái stale/partial nếu có.

## Quy tắc release

1. Chạy test matrix với default, secondary active, secondary inactive và
   mixed-version trước khi mở feature flag.
2. Không dùng endpoint global purge bucket. Route legacy
   `/api/object-storage/buckets/delete-all` đã fail-closed (`409`). Xóa bucket
   phải đi qua `delete/preview` rồi `delete/execute` cho từng bucket, có
   re-check object count, Object Lock, capability và audit.
3. Restore mặc định phải tạo volume/image mới trong scratch hoặc destination
   cô lập. Không promote, failover hay ghi đè production từ Chat hoặc AI.
4. RBD replication hiện chỉ là read-only posture qua
   `/api/volumes/{pool}/replication`; không được coi `enabled` là bằng chứng
   failover/failback đã được diễn tập.
5. RestoreDrill multi-cluster chỉ được mở sau khi có policy scratch riêng của
   từng cluster và isolated target acceptance. Cấu hình global hiện tại vẫn
   chỉ đủ cho default cluster.

## Audit Viewer

- Trang: `/audit`
- API: `GET /api/audit/storage?page=1&page_size=25&kind=incident|backup|object_storage`
- Chỉ trả metadata bounded: actor, event, target, result, timestamp và request
  ID rút gọn. Preview, secret và raw backend error không được trả qua feed này.
- Feed luôn scoped theo cluster switcher; không dùng audit của cluster khác làm
  bằng chứng cho cluster hiện tại.

## Backup / DR drill

1. Kiểm tra `/api/backups/multi-cluster-audit` để xác nhận target, tracked image,
   successful backup và restore-drill gap theo cluster.
2. Dùng `/api/backups/multi-cluster-digests` để kiểm tra digest metadata bounded
   của cả active và inactive cluster; đọc nội dung digest tại trang Backup của
   cluster cụ thể.
3. Chỉ chạy RestoreDrill khi scratch pool/image khác source và target đã được
   kiểm tra không tồn tại. Giữ lại `action_id`, recovery point, target slot,
   checksum, duration và cleanup result trong audit.
4. Nếu checksum, chain hoặc target evidence thiếu: dừng và trả
   `INSUFFICIENT_EVIDENCE`; không tự chọn recovery point mới hơn.

## Block Storage acceptance

- Inventory, capacity, dependency health, durability policy, protection gap và
  snapshot/clone insight đều là read-only evidence.
- `GET /api/performance-rca/diagnosis?pool=<pool>&image=<image>` đọc sample đã
  lưu của đúng cluster đang chọn; không truy vấn Ceph live. `candidate` là tương
  quan để điều tra, không phải root cause đã xác minh. `stale=true` hoặc
  `insufficient_evidence` thì không đề xuất mutation. `ai_generated=false` vì
  giai đoạn này chỉ dùng rule-based evidence; không quảng bá là AI verdict.
- Không đánh dấu volume stale/unattached nếu thiếu I/O history, attachment hoặc
  backup evidence.
- Không chạy `rbd lock rm`, `pg repair`, flatten, resize, QoS hoặc pool policy
  change từ insight endpoint. Các mutation phải có action ID, preview, approval,
  timeout, post-check và audit.

## Incident response

- `502/504` từ Ceph/RGW: giữ snapshot cũ có nhãn stale, không retry vô hạn.
- `UNSUPPORTED_VERSION` hoặc mixed-version: ẩn/khóa mutation và yêu cầu cập
  nhật capability matrix.
- Audit thiếu hoặc không ghi được: từ chối mutation; không tiếp tục chỉ vì
  backend đã trả thành công.
- Khi release gate fail, tắt feature flag, giữ read-only evidence và rollback
  theo release manifest. Không xóa database audit để che dấu lỗi.
