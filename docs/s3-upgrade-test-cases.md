# QUY TRÌNH TEST S3 (RGW) KHI NÂNG CẤP VERSION CEPH

---

## 1. Mục tiêu & phạm vi

| Mục | Nội dung |
|---|---|
| Mục tiêu | Đảm bảo dịch vụ S3 (RGW) hoạt động ổn định, không mất dữ liệu, không gián đoạn quá ngưỡng cho phép trong và sau khi nâng cấp Ceph |
| Phạm vi | RGW single-site và multisite, các thao tác S3 cơ bản, quyền truy cập, encryption (SSE-S3/SSE-KMS qua Vault), hiệu năng |
| Áp dụng cho | Mọi path nâng cấp Ceph (Nautilus→Octopus, Octopus→Pacific, Nautilus→Pacific) |
| Không thuộc phạm vi | Nâng cấp OS, nâng cấp hạ tầng network/firewall (xem tài liệu riêng) |

**Mức ưu tiên:** P1 = bắt buộc PASS; P2 = quan trọng, cần workaround được duyệt; P3 = bổ sung.

---

## 2. Chuẩn bị trước khi test (Pre-check)

### 2.1 Chuẩn bị môi trường test

| ID | Việc cần làm | Lệnh | P |
|---|---|---|---|
| PREP-01 | Tạo user test riêng, không dùng chung với user production | `radosgw-admin user create --uid=s3-upgrade-test --display-name="S3 Upgrade Test" --access-key=<key> --secret-key=<secret>` | P1 |
| PREP-02 | Tạo bucket test riêng cho từng kịch bản | `aws s3 mb s3://s3-upgrade-test-bucket --endpoint-url <rgw_endpoint>` | P1 |
| PREP-03 | Cấu hình `aws cli`/`s3cmd` trỏ đúng endpoint và key test | `aws configure set aws_access_key_id <key>` / `aws configure set aws_secret_access_key <secret>` | P1 |
| PREP-04 | Xác nhận kết nối tới RGW hoạt động bình thường trước khi bắt đầu | `curl -I <rgw_endpoint>` | P1 |
| PREP-05 | Ghi nhận baseline: user list, bucket list, dung lượng, số object | `radosgw-admin user list`; `radosgw-admin bucket stats --bucket=<bucket>` | P1 |
| PREP-06 | Nếu có multisite: xác nhận sync đang `caught up` trước khi bắt đầu | `radosgw-admin sync status` | P1 |
| PREP-07 | Nếu có dùng SSE-KMS/Vault: xác nhận Vault đang hoạt động, token còn hạn | `vault token lookup $(cat /etc/ceph/vault-root)` | P1 |
| PREP-08 | Ghi lại danh sách RGW instance đang chạy trên từng node (port, tên) | `systemctl list-units 'ceph-radosgw@*' --no-pager` | P2 |

### 2.2 Chuẩn bị dữ liệu baseline

| ID | Việc cần làm | Lệnh | P |
|---|---|---|---|
| DATA-01 | Upload 100-500 object nhỏ (1KB-1MB) vào bucket test | Script loop `aws s3 cp` | P1 |
| DATA-02 | Upload ít nhất 2-3 object lớn (>100MB) để test multipart | `aws s3 cp bigfile.bin s3://s3-upgrade-test-bucket/` | P1 |
| DATA-03 | Bật versioning trên 1 bucket, tạo 2-3 version cho cùng 1 object | `aws s3api put-bucket-versioning --bucket <bucket> --versioning-configuration Status=Enabled` | P2 |
| DATA-04 | Set 1 bucket có default encryption (nếu dùng SSE-S3/KMS) | `aws s3api put-bucket-encryption --bucket <bucket> --server-side-encryption-configuration '{...}'` | P2 |
| DATA-05 | Tạo manifest MD5/ETag của toàn bộ object để đối chiếu sau này | Script duyệt bucket, lưu `key,etag,size` vào file CSV | P1 |
| DATA-06 | Set 1 bucket policy và 1 lifecycle rule mẫu | `aws s3api put-bucket-policy`; `aws s3api put-bucket-lifecycle-configuration` | P2 |

---

## 3. Test TRONG lúc nâng cấp (chạy song song, liên tục)

> Chạy liên tục từ lúc bắt đầu nâng RGW đầu tiên đến khi RGW cuối cùng khởi động lại xong. Cần theo dõi cả từ phía client (request thật) lẫn phía server (log RGW).

### TC-S3-RUN-001: Tải PUT/GET liên tục qua Load Balancer | P1

**Mục tiêu:** Xác nhận client vẫn nhận response hợp lệ khi từng RGW instance lần lượt được nâng cấp (rolling upgrade qua LB).

**Các bước:**
```bash
# Chạy warp (Minio benchmark) liên tục qua VIP/LB
warp mixed --host=<VIP_RGW> --access-key=<key> --secret-key=<secret> \
  --bucket=s3-upgrade-test-bucket --duration=99999s --concurrent=20 \
  --obj.size=64KiB > /var/log/warp_upgrade.log 2>&1 &

# Song song, probe HTTP mỗi giây để bắt lỗi ngắn hạn
while true; do
  code=$(curl -s -o /dev/null -w "%{http_code}" <rgw_endpoint>/s3-upgrade-test-bucket/)
  [[ "$code" =~ ^(2|3)[0-9]{2}$ ]] || echo "$(date -Is) HTTP_FAIL code=$code" >> /var/log/s3_probe.log
  sleep 1
done &
```

**Tiêu chí Pass:**
- [ ] Tỉ lệ HTTP 5xx ≤ 0,1%.
- [ ] Không có chuỗi lỗi liên tiếp kéo dài > 5 giây.
- [ ] `warp` summary không báo timeout hàng loạt.

---

### TC-S3-RUN-002: Giám sát log RGW real-time trong lúc nâng cấp | P1

**Mục tiêu:** Phát hiện sớm lỗi bất thường trong log ngay khi từng RGW instance restart.

**Các bước:**
```bash
tail -f /var/log/ceph/ceph-client.rgw.*.log | grep -iE "error|denied|fail|crash|vault|kms"
```

**Tiêu chí Pass:**
- [ ] Không có lỗi `crash`/`abort`/`assert`.
- [ ] Mọi dòng `error` xuất hiện đều tương ứng đúng thời điểm restart dự kiến, không xuất hiện đơn lẻ bất thường.

---

### TC-S3-RUN-003: Multisite sync vẫn hoạt động trong lúc nâng cấp (nếu có) | P1

**Mục tiêu:** Xác nhận zone đang nâng cấp không làm gián đoạn đồng bộ với zone khác.

**Các bước:**
```bash
watch -n30 'radosgw-admin sync status'
```

**Tiêu chí Pass:**
- [ ] Không xuất hiện lỗi kiểu `failed to fetch all metadata keys` kéo dài.
- [ ] Nếu có shard "behind" tạm thời, phải tự bắt kịp (`caught up`) trong vòng vài phút sau khi RGW zone đó hoàn tất nâng cấp.

---

### TC-S3-RUN-004: Đo downtime từng RGW instance khi restart | P2

**Các bước:**
```bash
date +%s > /tmp/rgw_stop_ts
# ... restart RGW instance ...
while ! curl -sf -o /dev/null <rgw_instance_endpoint>; do sleep 1; done
date +%s > /tmp/rgw_up_ts
echo "Downtime: $(($(cat /tmp/rgw_up_ts) - $(cat /tmp/rgw_stop_ts))) giây"
```

**Tiêu chí Pass:**
- [ ] Mỗi instance downtime ≤ 30 giây (vì LB đã chuyển tải sang instance khác, downtime riêng từng instance không ảnh hưởng client nếu rolling đúng cách).

---

## 4. Test SAU khi nâng cấp

### 4.1 Trạng thái dịch vụ

| ID | Việc cần làm | Lệnh | Tiêu chí Pass | P |
|---|---|---|---|---|
| POST-01 | Version RGW đồng nhất | `radosgw-admin --version`; `ceph versions \| jq .rgw` | Toàn bộ instance cùng 1 version đích | P1 |
| POST-02 | Toàn bộ RGW instance đang `active running` | `systemctl list-units 'ceph-radosgw@*' --no-pager` | 100% instance `active`, không `failed` | P1 |
| POST-03 | Mỗi instance bind đúng port đã cấu hình | `ss -tlnp \| grep -E "808[0-9]"` | Mỗi port tương ứng đúng 1 process, không có port trùng/conflict | P1 |
| POST-04 | Không có crash mới | `ceph crash ls-new` | 0 crash | P1 |

### 4.2 Toàn vẹn dữ liệu

| ID | Việc cần làm | Lệnh | Tiêu chí Pass | P |
|---|---|---|---|---|
| POST-10 | Đối chiếu toàn bộ object với manifest baseline (ETag/MD5) | Script so `manifest.csv` với `aws s3api head-object` từng key | 100% khớp, 0 MISSING/MISMATCH | P1 |
| POST-11 | Xác nhận số lượng object & dung lượng bucket khớp baseline | `radosgw-admin bucket stats --bucket=<bucket>` | Số object/dung lượng = baseline (cộng thêm nếu có ghi trong lúc test) | P1 |
| POST-12 | Object versioning — các version cũ vẫn tải được | `aws s3api list-object-versions --bucket <bucket>`; `aws s3api get-object --version-id <id>` | Toàn bộ version cũ đọc được, nội dung đúng | P2 |
| POST-13 | Multipart object lớn tải về nguyên vẹn | `aws s3 cp s3://.../bigfile.bin ./verify.bin`; `md5sum` so sánh | Checksum khớp | P1 |
| POST-14 | Bucket policy / lifecycle rule vẫn còn nguyên | `aws s3api get-bucket-policy`; `aws s3api get-bucket-lifecycle-configuration` | Nội dung khớp cấu hình đã set trước nâng cấp | P2 |

### 4.3 Chức năng cơ bản (Regression)

| ID | Thao tác | Lệnh | Tiêu chí Pass | P |
|---|---|---|---|---|
| POST-20 | PUT object mới | `aws s3api put-object --bucket <bucket> --key test.txt --body test.txt` | HTTP 200, không lỗi | P1 |
| POST-21 | GET object | `aws s3api get-object --bucket <bucket> --key test.txt out.txt` | Nội dung khớp | P1 |
| POST-22 | LIST bucket | `aws s3 ls s3://<bucket>` | Trả về đúng danh sách | P1 |
| POST-23 | DELETE object | `aws s3api delete-object --bucket <bucket> --key test.txt` | HTTP 204 | P1 |
| POST-24 | Multipart upload | `aws s3api create-multipart-upload` → upload-part → complete | Hoàn tất, object đọc lại đúng | P1 |
| POST-25 | Copy object | `aws s3api copy-object --copy-source ...` | Thành công | P2 |
| POST-26 | Presigned URL | `aws s3 presign s3://<bucket>/test.txt` | URL truy cập được, tải đúng nội dung | P2 |
| POST-27 | Tạo/xoá bucket mới | `aws s3 mb` / `aws s3 rb` | Thành công | P1 |
| POST-28 | Quản trị user (tạo, quota, suspend/enable) | `radosgw-admin user create/quota set/suspend/enable` | Thành công, đúng hành vi | P2 |
| POST-29 | Bucket resharding | `radosgw-admin bucket reshard --bucket=<bucket> --num-shards=N` | Thành công, không mất object | P2 |

### 4.4 Quyền truy cập & bảo mật (dựa trên bài học thực tế đã gặp)

> **Lưu ý quan trọng:** trong quá trình test thực tế trước đây từng gặp lỗi `AccessDenied` không liên quan đến nâng cấp mà do quyền user/bucket — nhóm test case này giúp phân biệt rạch ròi để tránh chẩn đoán sai.

| ID | Thao tác | Lệnh | Mục đích | P |
|---|---|---|---|---|
| POST-30 | Test LIST (đọc) trước, PUT (ghi) sau — để tách biệt lỗi | `aws s3 ls` rồi mới `aws s3api put-object` | Nếu LIST OK nhưng PUT lỗi → vấn đề quyền ghi/quota, không phải do nâng cấp | P1 |
| POST-31 | Tra user sở hữu access key đang test | `radosgw-admin user info --access-key=<key>` | Xác nhận key hợp lệ, chưa bị suspend | P1 |
| POST-32 | Xác nhận owner thật của bucket | `radosgw-admin bucket stats --bucket=<bucket> \| grep owner` | Khớp với uid đang test | P1 |
| POST-33 | Kiểm tra quota user chưa đầy | `radosgw-admin user info --uid=<uid> \| grep -A5 user_quota` | Chưa vượt `max_size`/`max_objects` | P2 |
| POST-34 | PUT object không kèm SSE trước, có SSE sau — tách biệt lỗi do encryption | So sánh 2 lần PUT | Nếu không SSE OK, có SSE lỗi → vấn đề Vault/KMS, không phải quyền cơ bản | P1 |

### 4.5 Encryption / SSE-S3 qua Vault (nếu có dùng)

| ID | Việc cần làm | Lệnh | Tiêu chí Pass | P |
|---|---|---|---|---|
| POST-40 | Vault vẫn kết nối được từ RGW | `curl -H "X-Vault-Token: $(cat /etc/ceph/vault-root)" <vault_addr>/v1/sys/health` | Trả về JSON hợp lệ | P1 |
| POST-41 | Transit engine + key vẫn tồn tại | `curl ... <vault_addr>/v1/transit/keys?list=true` | Danh sách key không đổi so với trước nâng cấp | P1 |
| POST-42 | PUT object với SSE-S3/SSE-KMS thành công | `aws s3api put-object ... --server-side-encryption aws:kms --ssekms-key-id <key>` | HTTP 200 | P1 |
| POST-43 | GET lại object đã mã hoá, giải mã đúng nội dung | `aws s3api get-object ...` rồi so checksum | Nội dung khớp bản gốc | P1 |
| POST-44 | Đối chiếu Vault audit log để xác nhận RGW thực sự gọi transit encrypt/decrypt | `tail -f /var/log/vault/audit.log \| grep transit` trong lúc test POST-42/43 | Thấy request transit từ user-agent RGW (không phải curl thủ công) | P2 |

### 4.6 Multisite (nếu có)

| ID | Việc cần làm | Lệnh | Tiêu chí Pass | P |
|---|---|---|---|---|
| POST-50 | Ghi object ở zone A, kiểm tra xuất hiện ở zone B | PUT ở zone A → `sleep 60` → GET ở zone B | Xuất hiện ≤ 5 phút, nội dung khớp | P1 |
| POST-51 | Sync status healthy ở cả 2 chiều | `radosgw-admin sync status` (chạy ở cả 2 zone) | `caught up`, không lỗi `failed to fetch metadata keys` | P1 |
| POST-52 | Metadata (user, bucket config) đồng bộ đúng | Tạo user mới ở zone A, kiểm tra xuất hiện ở zone B | Đồng bộ thành công | P2 |

### 4.7 Hiệu năng

| ID | Việc cần làm | Lệnh | Tiêu chí Pass | P |
|---|---|---|---|---|
| POST-60 | Throughput PUT/GET hỗn hợp, so với baseline | `warp mixed --duration=4h ...` | Suy giảm ≤ 15% so với baseline trước nâng cấp | P2 |
| POST-61 | Latency trung bình request | Trích từ `warp` summary hoặc log RGW field `latency=` | Không tăng quá 15% so với baseline | P2 |
| POST-62 | Không có `Broken pipe` bất thường tăng đột biến | `grep -c "Broken pipe" /var/log/ceph/ceph-client.rgw.*.log` | Tần suất tương đương mức nền trước nâng cấp (lỗi client-side tự nhiên), không tăng đột biến | P3 |

---

## 5. Bảng theo dõi kết quả

| Test Case ID | Thời điểm chạy | Kết quả | Pass/Fail | Ghi chú |
|---|---|---|---|---|
| TC-S3-RUN-001 | | | | |
| TC-S3-RUN-002 | | | | |
| TC-S3-RUN-003 | | | | |
| POST-01 → POST-62 | | | | |

---

## 6. Các lỗi thực tế đã gặp — checklist tra cứu nhanh khi debug

| Triệu chứng | Nguyên nhân thường gặp | Cách kiểm tra nhanh |
|---|---|---|
| RGW service `failed`, log `failed to bind address ... Address already in use` | Thiếu/sai `rgw_frontends`, RGW rơi về port mặc định 7480 gây trùng port | `ceph config get client.rgw.<instance> rgw_frontends` |
| `AccessDenied` khi PUT/GET | Sai access key, sai owner bucket, quota đầy, **không liên quan Vault/nâng cấp** | Test LIST trước, PUT không SSE trước để tách biệt nguyên nhân |
| `ERROR: failed to fetch all metadata keys` (multisite) | Lệch version RGW giữa các zone trong lúc nâng cấp, hoặc network/period lỗi | `radosgw-admin --version` so giữa các zone, đợi hoàn tất rolling upgrade |
| `client_io->complete_request() returned Broken pipe` | Client tự ngắt kết nối (timeout, đóng tab, mạng yếu) — thường **không phải lỗi RGW** | Đếm tần suất, so với mức nền bình thường |
| `n OSD(s) reporting legacy (not per-pool) BlueStore omap usage stats` | Chưa convert OMAP sau nâng cấp — **không ảnh hưởng IOPS/S3**, chỉ là cảnh báo accounting | `ceph-bluestore-tool repair` từng OSD offline |
| RGW không gọi được Vault dù config đúng | Sai `rgw_crypt_sse_s3_vault_prefix`, token file sai quyền/nội dung, hoặc chưa tạo key trong transit | Test bằng `curl` thủ công tới đúng path Vault trước, đối chiếu audit log |

---

## 7. Tiêu chí kết thúc (Exit Criteria)

- [ ] 100% test case P1 PASS.
- [ ] ≥ 95% test case P2 PASS.
- [ ] 0 object bị mất/sai checksum so với baseline.
- [ ] Multisite sync (nếu có) ở trạng thái `caught up`.
- [ ] Không còn RGW instance nào ở trạng thái `failed`.
- [ ] Vault/SSE-KMS (nếu dùng) hoạt động bình thường, xác nhận qua audit log.
- [ ] Hiệu năng S3 sau nâng cấp không suy giảm quá 15% so với baseline.

---

## 8. Quyết định phạm vi cho lần triển khai automation đầu tiên (2026-08-06)

Cụm thật hiện tại (`.env`) có `CEPH_RGW_NODES=` rỗng (chưa cấu hình RGW node nào), và toàn bộ
codebase không có bất kỳ tích hợp Vault/KMS nào (`config/settings.py` không có field nào tên
`vault`). Theo xác nhận của người dùng (tương tự tiền lệ Epic 9 — RBD-only vì cụm không có
RGW/CephFS), lần triển khai automation đầu tiên trong `worker/executor/test_runner/group_e.py`
**declined** (không tự động hoá, đánh dấu SKIP) 2 nhóm sau — không phải vì chúng sai, mà vì hạ
tầng để verify chúng chưa tồn tại trong dự án này:

- Mọi test case liên quan Vault/SSE-KMS: PREP-07, DATA-04, POST-34 (nửa so sánh SSE), POST-40..44.
- Mọi test case liên quan multisite: TC-S3-RUN-003, POST-50..52.

Nếu cụm sau này có RGW + multisite + Vault thật, các lớp `TestCaseDeclined` tương ứng có thể thay
bằng automation thật mà không cần đổi kiến trúc — cùng mẫu hình `TestCaseDeclined` mà
`TcCompat005OpenstackIntegration`/`TcCompat006KubernetesCephCsi` (Story 10.5) đã dùng.
