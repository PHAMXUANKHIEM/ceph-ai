# TEST CASE CHI TIẾT: KIỂM THỬ TRONG & SAU KHI NÂNG CẤP CEPH
## 14.2.22 (Nautilus) → 16.2.15 (Pacific) — bỏ qua Octopus

> Nguồn gốc tài liệu này (2026-08-04): đây là tài liệu test-case gốc mà Epic 10
> (Ceph Upgrade Test Runner) phải tự động hoá. Bản đầu tiên bị mất khi prototype
> đứng riêng (`ceph-upgrade-test-runner/`) bị xoá trong lúc gộp vào Epic 10 của
> `ceph-aiops` (xem `_bmad-output/implementation-artifacts/deferred-work.md`,
> mục "Superseded"). Người dùng cung cấp lại toàn văn ngày 2026-08-04 để làm
> Story 10.3 (Nhóm A). **File này là nguồn sự thật duy nhất cho nội dung 63 test
> case — không sửa/diễn giải lại nội dung dưới đây khi implement, chỉ tham chiếu.**
>
> **Gap đã biết**: bảng tổng hợp ở mục 4 ghi Nhóm RUN có 13 test case, nhưng nội
> dung dưới đây chỉ có 11 (TC-RUN-001, TC-RUN-004 đến TC-RUN-013 — thiếu
> TC-RUN-002 và TC-RUN-003). Story 10.3 implement đúng 11 cái đã có nội dung;
> TC-RUN-002/003 để trống, chưa có test case tương ứng trong engine cho tới khi
> user cung cấp bổ sung.

---

## 1. Thông tin tài liệu

| Mục | Nội dung |
|---|---|
| Phạm vi | Test case thực thi **trong lúc** nâng cấp và **sau khi** nâng cấp hoàn tất. Không bao gồm quy trình/lệnh thao tác nâng cấp (backup, chuyển version daemon...). |
| Điều kiện | Quy trình nâng cấp (MON → MGR → OSD → MDS → RGW) đã thực hiện, có backup đầy đủ trước khi bắt đầu, và **toàn bộ node đã đạt yêu cầu OS của Pacific (el8/Ubuntu 20.04, không còn el7)** |
| Version nguồn → đích | 14.2.22 (Nautilus) → 16.2.15 (Pacific) — nhảy 2 major release, bỏ qua Octopus |
| Mức rủi ro | **Cao nhất trong 3 path nâng cấp** — cụm phải xử lý chuyển đổi OMAP hai lớp (per-pool + per-PG) cùng lúc, thay vì tách làm 2 đợt như đi tuần tự |
| Baseline bắt buộc trước khi test | Manifest checksum RBD/CephFS/S3, `ceph config dump`, `ceph osd crush dump`, `ceph auth list`, kết quả `fio`/`rados bench`/`warp`/`mdtest` đo trước nâng cấp |

**Mức ưu tiên:** P1 = bắt buộc PASS (fail → dừng/rollback ngay); P2 = quan trọng (fail → phân tích, cần workaround được duyệt trước khi tiếp tục); P3 = bổ sung (fail → ghi nhận, không chặn tiến độ).

> ⚠️ **Lưu ý xuyên suốt tài liệu:** vì bỏ qua Octopus, mọi test case liên quan đến OMAP phải kiểm tra **cả 2 loại cảnh báo cùng lúc** (`BLUESTORE_NO_PER_POOL_OMAP` và `BLUESTORE_NO_PER_PG_OMAP`), và ngưỡng thời gian downtime OSD được nới rộng hơn 2 path đi tuần tự do khối lượng convert lớn hơn.

---

## 2. NHÓM A — TEST CASE TRONG LÚC NÂNG CẤP

> Toàn bộ test case nhóm này chạy **song song, liên tục** trong suốt thời gian nâng cấp (từ lúc dừng MON đầu tiên đến lúc RGW cuối cùng khởi động lại xong). Cần ít nhất 2 người: 1 người theo dõi nhóm A liên tục, 1 người thực hiện các bước nâng cấp. **Vì đây là path rủi ro cao nhất, khuyến nghị tăng tần suất kiểm tra thủ công (mỗi 15-30 phút) thay vì chỉ dựa vào script tự động.**

---

### TC-RUN-001: I/O RBD liên tục không gián đoạn | P1

**Mục tiêu:** Xác nhận client đang ghi/đọc dữ liệu trên RBD image không gặp bất kỳ lỗi I/O nào trong suốt quá trình nâng cấp, đặc biệt trong giai đoạn OSD phải xử lý bước nhảy định dạng lớn (2 thế hệ cùng lúc).

**Điều kiện tiền đề:**
- Có ít nhất 1 RBD image (≥ 20 GB) đã map vào client qua krbd hoặc rbd-nbd.
- Client cài `fio` với engine hỗ trợ rbd hoặc dùng qua block device đã map + libaio.

**Các bước thực hiện:**
1. Trên client, chạy lệnh sau **trước khi** bắt đầu nâng cấp và để nó chạy xuyên suốt:
   ```bash
   fio --name=upgrade_io_test \
       --filename=/dev/vde \
       --ioengine=libaio --direct=1 \
       --rw=randrw --rwmixread=70 \
       --bs=4k --iodepth=32 --numjobs=4 \
       --time_based --runtime=99999 \
       --verify=crc32c --verify_fatal=1 \
       --output-format=json --output=/var/log/fio_upgrade.json &
   ```
2. Ghi lại PID của tiến trình để dừng đúng lúc sau khi nâng cấp hoàn tất.
3. Mỗi 5 phút kiểm tra log fio:
   ```bash
   tail -n 50 /var/log/fio_upgrade.json | grep -Ei "error|fail"
   ```
4. Ghi lại các mốc thời gian: bắt đầu nâng MON, bắt đầu nâng OSD từng host, **đặc biệt là giai đoạn `ceph-bluestore-tool repair` chạy convert OMAP 2 lớp** — đây là điểm có khả năng gây latency spike lớn nhất của toàn bộ path.
5. Sau khi nâng cấp hoàn tất, dừng fio, tổng hợp báo cáo cuối.

**Kết quả mong đợi:** Toàn bộ I/O hoàn thành không lỗi; verify checksum khớp 100%; độ trễ tăng tạm thời khi OSD restart/convert nhưng không vượt ngưỡng đã nới rộng cho path này.

**Tiêu chí Pass:**
- [ ] 0 lỗi `Input/output error`, 0 lỗi verify.
- [ ] p99 latency không vượt quá 3 lần so với baseline.
- [ ] Không có khoảng thời gian nào I/O bị treo hoàn toàn quá 30 giây liên tục.

---


### TC-RUN-004: Giám sát trạng thái PG liên tục | P1

**Mục tiêu:** Đảm bảo không có PG nào rơi vào trạng thái không phục vụ được (`inactive`, `down`, `incomplete`, `stale`).

**Các bước thực hiện:**
```bash
while true; do
  echo "=== $(date -Is) ===" >> /var/log/pg_watch.log
  ceph pg stat >> /var/log/pg_watch.log
  ceph health detail | grep -Ei "inactive|down|incomplete|stale" >> /var/log/pg_watch.log
  sleep 5
done
```
Song song mở `watch -n2 ceph -s` để quan sát trực tiếp. Nếu phát hiện PG bất thường, lưu ngay bằng chứng qua `ceph pg dump` và báo dừng nâng cấp.

**Tiêu chí Pass:**
- [ ] Các từ khóa trên = 0 trong suốt quá trình (ngoại trừ `degraded` tạm thời — xem TC-RUN-011).

---

### TC-RUN-005: Giám sát quorum MON liên tục | P1

**Mục tiêu:** Đảm bảo quorum MON không giảm dưới 2/3.

**Các bước thực hiện:**
```bash
while true; do
  ts=$(date -Is)
  n=$(ceph quorum_status --format json-pretty 2>/dev/null | jq '.quorum | length')
  echo "$ts quorum_size=$n" >> /var/log/quorum_watch.log
  [ "$n" -lt 2 ] && echo "$ts *** QUORUM LOST ***" >> /var/log/quorum_watch.log
  sleep 2
done
```

**Tiêu chí Pass:**
- [ ] 0 dòng `*** QUORUM LOST ***`.
- [ ] Thời gian mỗi MON quay lại quorum ≤ 60 giây.

---

### TC-RUN-006: Giám sát slow ops | P2

**Mục tiêu:** Phát hiện sớm thao tác OSD chậm bất thường — có thể là dấu hiệu OSD đang bị nghẽn bởi quá trình convert OMAP 2 lớp.

**Các bước thực hiện:**
```bash
while true; do
  out=$(ceph health detail | grep -i "slow ops\|SLOW_OPS")
  [ -n "$out" ] && echo "$(date -Is) $out" >> /var/log/slowops_watch.log
  sleep 10
done
```

**Tiêu chí Pass:**
- [ ] Mỗi lần xuất hiện, thời gian tồn tại < 60 giây.
- [ ] Không OSD nào lặp lại slow ops liên tục nhiều lần.

---

### TC-RUN-007: Đo downtime từng daemon khi restart | P2

**Mục tiêu:** Định lượng chính xác thời gian gián đoạn của từng loại daemon, đặc biệt OSD vì phải xử lý khối lượng convert lớn hơn 2 path còn lại.

**Các bước thực hiện:**
1. Trước khi dừng mỗi daemon: `date +%s.%N > /tmp/<daemon>_stop_ts`.
2. Sau khi khởi động lại, poll đến khi thực sự `up`:
   ```bash
   while ! ceph osd stat | grep -q "osd.<id> up"; do sleep 1; done
   date +%s.%N > /tmp/<daemon>_up_ts
   echo "Downtime: $(echo "$(cat /tmp/<daemon>_up_ts) - $(cat /tmp/<daemon>_stop_ts)" | bc) giây" >> /var/log/downtime.log
   ```
3. Lặp lại cho từng MON, MGR, mỗi OSD, MDS, RGW.

**Tiêu chí Pass:**
- [ ] OSD: downtime ≤ **180 giây** mỗi cái (ngưỡng cao hơn 2 path đi tuần tự do phải xử lý convert 2 lớp).
- [ ] MON: join lại quorum ≤ 60 giây.
- [ ] MGR: failover sang standby ≤ 30 giây.
- [ ] MDS: gián đoạn I/O metadata ≤ 30 giây.

---

### TC-RUN-008: Kiểm tra log lỗi hệ thống trên toàn bộ node | P1

**Mục tiêu:** Phát hiện sớm crash, assertion failure, hoặc lỗi nghiêm trọng ẩn trong log — đặc biệt quan trọng ở path này vì thao tác `ceph-bluestore-tool repair` xử lý khối lượng chuyển đổi lớn, rủi ro lỗi cao hơn.

**Các bước thực hiện:**
1. Trên mỗi node:
   ```bash
   journalctl -u 'ceph-*' -f | grep --line-buffered -Ei "error|assert|crash|abort|segfault" >> /var/log/ceph_error_watch.log &
   ```
2. Sau khi hoàn tất, tổng hợp từ toàn bộ node:
   ```bash
   for host in $(cat hosts.txt); do
     echo "=== $host ==="
     ssh "$host" "cat /var/log/ceph_error_watch.log"
   done
   ```

**Tiêu chí Pass:**
- [ ] 0 dòng chứa `FAILED ceph_assert`, `Segmentation fault`, `core dumped`.
- [ ] Mọi dòng `error` khác đều tương ứng thời điểm restart/convert dự kiến.

---

### TC-RUN-009: Kiểm tra crash module | P1

**Mục tiêu:** Xác nhận không có daemon nào crash trong suốt quá trình nâng cấp, đặc biệt trong lúc chạy `ceph-bluestore-tool repair`.

**Các bước thực hiện:**
1. Baseline: `ceph crash ls > /tmp/crash_before.txt`.
2. Sau mỗi giai đoạn lớn: `ceph crash ls-new`.
3. Nếu có crash mới: `ceph crash info <crash-id>` để phân tích trước khi tiếp tục — **không được bỏ qua bước này ở path rủi ro cao này**.

**Tiêu chí Pass:**
- [ ] 0 crash mới từ lúc bắt đầu đến khi hoàn tất.

---

### TC-RUN-010: Client version cũ vẫn hoạt động trong lúc nâng cấp | P1

**Mục tiêu:** Xác nhận client giữ nguyên 14.2.22 vẫn thao tác bình thường khi cụm đang trộn version (Nautilus ↔ Pacific, chênh lệch 2 major).

**Các bước thực hiện:**
```bash
# RBD
fio --name=old_client_rbd --filename=/dev/rbd1 --ioengine=libaio \
    --rw=randrw --bs=4k --time_based --runtime=99999 --verify=crc32c &
# CephFS
while true; do echo test > /mnt/cephfs_old/f.txt; cat /mnt/cephfs_old/f.txt >/dev/null; sleep 1; done &
# S3
while true; do aws --endpoint-url http://<VIP_RGW>:8080 s3 ls s3://upgrade-test-bucket/ >/dev/null; sleep 2; done &
```

**Tiêu chí Pass:**
- [ ] 0 lỗi trên cả 3 loại tải trong suốt quá trình.

---

### TC-RUN-011: Giám sát PG degraded tạm thời | P1

**Mục tiêu:** Xác nhận PG `degraded` chỉ tạm thời và tự phục hồi nhanh dù cụm đang xử lý khối lượng convert OMAP lớn hơn.

**Các bước thực hiện:**
```bash
while true; do
  d=$(ceph pg dump 2>/dev/null | grep -c degraded)
  echo "$(date -Is) degraded_count=$d" >> /var/log/degraded_watch.log
  sleep 30
done
```
Ghi lại thời điểm mỗi host hoàn tất restart, và thời điểm `degraded_count` về 0.

**Tiêu chí Pass:**
- [ ] Thời gian từ lúc host hoàn tất đến khi về 0 không vượt quá 30 phút.

---

### TC-RUN-012: Không rebalance/backfill ngoài ý muốn | P2

**Mục tiêu:** Xác nhận cờ `noout`/`norebalance` phát huy tác dụng, không có backfill lớn ngoài kế hoạch.

**Các bước thực hiện:**
```bash
ceph -s | grep -Ei "recovery|backfill|misplaced"
ceph osd dump | grep flags
```

**Tiêu chí Pass:**
- [ ] Không phát hiện `backfilling`/`recovering` chiếm tỉ lệ lớn (>5% object) khi cờ đang set.

---

### TC-RUN-013: Giám sát riêng quá trình convert OMAP hai lớp | P1

**Mục tiêu:** Đây là test case **quan trọng nhất và đặc thù riêng của path này** — theo dõi sát quá trình `ceph-bluestore-tool repair` xử lý đồng thời cả 2 tầng chuyển đổi (per-pool + per-PG) trên từng OSD, vì đây là điểm rủi ro và tốn thời gian nhất của toàn bộ quy trình.

**Điều kiện tiền đề:** Đã ghi nhận số liệu OSD có OMAP lớn (đặc biệt `.rgw.buckets.index`) từ bước tiền kiểm để so sánh thời gian thực tế với ước lượng.

**Các bước thực hiện:**
1. Với mỗi OSD chuẩn bị convert, ghi lại thời gian bắt đầu và log chi tiết:
   ```bash
   echo "$(date -Is) START convert osd.<id>" >> /var/log/omap_convert.log
   time ceph-bluestore-tool repair --path /var/lib/ceph/osd/ceph-<id> 2>&1 | tee -a /var/log/omap_convert.log
   echo "$(date -Is) END convert osd.<id>" >> /var/log/omap_convert.log
   ```
2. Đối chiếu thời gian thực đo với ước lượng đã đo trên lab (nếu vượt quá 2 lần so với ước lượng, phải cảnh báo sớm cho team vận hành trước khi tiếp tục các OSD còn lại).
3. Theo dõi riêng OSD chứa pool index RGW lớn — đây thường là OSD tốn thời gian convert lâu nhất trong toàn cụm.
4. Sau mỗi OSD hoàn tất, kiểm tra ngay:
   ```bash
   ceph -s   # chờ active+clean trước khi sang OSD tiếp theo
   ceph health detail | grep -i omap
   ```

**Kết quả mong đợi:** Từng OSD hoàn tất convert với thời gian có thể dự đoán được (không có OSD nào bất ngờ treo quá lâu so với ước lượng); không có OSD nào lỗi convert.

**Tiêu chí Pass:**
- [ ] Không OSD nào có thời gian convert vượt quá 2 lần so với ước lượng đã đo trên lab.
- [ ] Nếu vượt ngưỡng, đã có cảnh báo sớm gửi team vận hành và quyết định tiếp tục/tạm dừng được ghi nhận.
- [ ] 100% OSD convert thành công (`success`), không có OSD nào phải chạy lại lệnh repair do lỗi giữa chừng.

---

## 3. NHÓM B — TEST CASE SAU KHI NÂNG CẤP

### 3.1 Trạng thái cụm

#### TC-POST-001: Xác nhận version đồng nhất | P1

**Mục tiêu:** Đảm bảo toàn bộ daemon (mon, mgr, osd, mds, rgw) đã chuyển sang đúng version đích, không sót lại version cũ.

**Các bước thực hiện:**
```bash
ceph versions
ceph tell mon.* version
ceph tell osd.* version | grep -v "16.2.15"   # phải trả về rỗng
ceph tell mds.* version
radosgw-admin --version
```

**Tiêu chí Pass:**
- [ ] `ceph versions` chỉ liệt kê 1 version = 16.2.15 cho mỗi loại daemon.
- [ ] `ceph tell osd.* version | grep -v "16.2.15"` trả về rỗng.

---

#### TC-POST-002: Sức khoẻ tổng thể cụm | P1

**Mục tiêu:** Xác nhận cụm khoẻ mạnh sau khi hoàn tất bước nhảy 2 major release.

**Các bước thực hiện:**
```bash
ceph -s
ceph health detail
```

**Tiêu chí Pass:**
- [ ] `ceph -s` = `HEALTH_OK`, hoặc mọi dòng `HEALTH_WARN` đều đã map với 1 test case xử lý cụ thể ở mục 3.3.

---

#### TC-POST-003: PG toàn vẹn | P1

**Mục tiêu:** Đảm bảo toàn bộ PG ổn định ở trạng thái phục vụ bình thường.

**Các bước thực hiện:**
```bash
ceph pg stat
ceph pg dump_stuck
```

**Tiêu chí Pass:**
- [ ] 100% PG `active+clean`.
- [ ] `pg dump_stuck` trả về rỗng.

---

#### TC-POST-004: OSD map đúng cấu trúc | P1

**Mục tiêu:** Xác nhận không có OSD nào bị mất, sai trạng thái, hoặc CRUSH tree bị thay đổi ngoài dự kiến.

**Các bước thực hiện:**
```bash
ceph osd tree
ceph osd dump | head -50
diff <(ceph osd crush dump) /baseline/cluster/osd_crush_dump_before.json
```

**Tiêu chí Pass:**
- [ ] Tổng số OSD `up`/`in` = baseline.
- [ ] `diff` với CRUSH dump baseline không có sai khác ngoài dự kiến.

---

#### TC-POST-005: Dung lượng & số lượng object | P1

**Mục tiêu:** Đối chiếu dung lượng sử dụng và số object mỗi pool để phát hiện bất thường (mất object sau convert 2 lớp).

**Các bước thực hiện:**
```bash
ceph df detail
rados df
diff <(ceph df detail) /baseline/cluster/df_before.txt
```

**Tiêu chí Pass:**
- [ ] Số object mỗi pool = baseline + số lượng ghi thêm dự kiến từ nhóm A, không có chênh lệch âm/bất thường.

---

#### TC-POST-006: CRUSH map không đổi ngoài dự kiến | P1

**Các bước thực hiện:**
```bash
ceph osd crush dump > /tmp/crush_after.json
diff /baseline/cluster/crush_dump_before.json /tmp/crush_after.json
```

**Tiêu chí Pass:**
- [ ] `diff` không có nội dung khác biệt ngoài các thay đổi đã lên kế hoạch.

---

#### TC-POST-007: Cấu hình được bảo toàn | P2

**Mục tiêu:** Xác nhận config không mất sau nâng cấp; ghi nhận rõ các option đổi tên/deprecated tích luỹ qua **cả 2 thế hệ** (Octopus + Pacific).

**Các bước thực hiện:**
```bash
ceph config dump > /tmp/config_after.txt
diff /baseline/cluster/config_dump_before.txt /tmp/config_after.txt
```

**Tiêu chí Pass:**
- [ ] Không có config tuỳ chỉnh nào biến mất mà không giải thích được.
- [ ] Toàn bộ trường hợp đổi tên/deprecated tích luỹ qua 2 thế hệ được liệt kê đầy đủ kèm giá trị tương đương mới.

---

#### TC-POST-008: Auth/keyring nguyên vẹn | P1

**Các bước thực hiện:**
```bash
ceph auth list > /tmp/auth_after.txt
diff /baseline/cluster/auth_list_before.txt /tmp/auth_after.txt
```

**Tiêu chí Pass:**
- [ ] `diff` không có sai khác.

---

#### TC-POST-009: Không có crash mới | P1

**Các bước thực hiện:**
```bash
ceph crash ls
```

**Tiêu chí Pass:**
- [ ] Không có crash-id mới so với trước khi nâng cấp.

---

### 3.2 Toàn vẹn dữ liệu (quan trọng nhất ở path này)

#### TC-POST-010: Toàn vẹn dữ liệu RBD replicated | P1

**Mục tiêu:** Xác nhận dữ liệu trên RBD image thuộc pool replicated không bị hỏng sau bước nhảy 2 major.

**Các bước thực hiện:**
```bash
rbd map rbd_rep/testimage1
mount /dev/rbd0 /mnt/verify_rbd
sha256sum -c /baseline/rbd_rep.sha256 --directory=/mnt/verify_rbd
```
Lặp lại cho toàn bộ 5 image baseline.

**Tiêu chí Pass:**
- [ ] `sha256sum -c` trả về `OK` cho 100% file, 0 dòng `FAILED`.

---

#### TC-POST-011: Toàn vẹn dữ liệu RBD erasure coded | P1

**Mục tiêu:** Tương tự TC-POST-010 nhưng trên pool EC — rủi ro cao hơn do cơ chế tính chunk phức tạp và ảnh hưởng bởi convert OMAP 2 lớp.

**Các bước thực hiện:** Tương tự TC-POST-010, thay pool bằng `rbd_ec`.

**Tiêu chí Pass:**
- [ ] 100% checksum khớp trên toàn bộ image thuộc pool EC.

---

#### TC-POST-012: Snapshot & clone RBD | P1

**Mục tiêu:** Xác nhận toàn bộ snapshot và clone RBD tạo trước nâng cấp còn nguyên vẹn.

**Các bước thực hiện:**
```bash
rbd snap ls rbd_rep/testimage1
rbd snap rollback rbd_rep/testimage1@snap_baseline
rbd map rbd_rep/testimage1_clone
mount /dev/rbd1 /mnt/verify_clone
diff -r /mnt/verify_clone /baseline/clone_reference/
rbd children rbd_rep/testimage1@snap_baseline
```

**Tiêu chí Pass:**
- [ ] `rbd snap ls` liệt kê đủ snapshot đã tạo ở baseline.
- [ ] `rollback` không lỗi.
- [ ] `diff -r` không có khác biệt.

---

#### TC-POST-013: Toàn vẹn dữ liệu CephFS | P1

**Các bước thực hiện:**
```bash
mount -t ceph <mon_ip>:/ /mnt/verify_cephfs -o name=admin,secretfile=/etc/ceph/admin.secret
find /mnt/verify_cephfs -type f | wc -l
sha256sum -c /baseline/cephfs.sha256 --directory=/mnt/verify_cephfs
```

**Tiêu chí Pass:**
- [ ] Số file = baseline (200.050 file theo kịch bản chuẩn bị).
- [ ] `sha256sum -c` không có dòng `FAILED`.

---

#### TC-POST-014: Snapshot CephFS | P2

**Các bước thực hiện:**
```bash
ls /mnt/verify_cephfs/.snap/base
cat /mnt/verify_cephfs/.snap/base/<file_mẫu> | md5sum
```

**Tiêu chí Pass:**
- [ ] Snapshot truy cập được, nội dung khớp checksum baseline.

---

#### TC-POST-015: Toàn vẹn dữ liệu S3 | P1

**Mục tiêu:** Xác nhận toàn bộ object trên bucket S3 (bao gồm bucket 500.000 object) không bị hỏng hay mất — đây là pool có OMAP index lớn nhất, rủi ro cao nhất sau convert 2 lớp.

**Các bước thực hiện:**
```bash
python3 verify_s3_manifest.py \
  --endpoint http://<VIP_RGW>:8080 \
  --manifest /baseline/s3_manifest.csv \
  --report /tmp/s3_verify_report.csv
radosgw-admin bucket stats --bucket=<tên_bucket>
```

**Tiêu chí Pass:**
- [ ] Không có dòng `MISMATCH`/`MISSING` trong report.
- [ ] Số object trong `bucket stats` = baseline.

---

#### TC-POST-016: Object versioning S3 | P2

**Các bước thực hiện:**
```bash
s3cmd ls --list-versions s3://versioned-bucket/
s3cmd get s3://versioned-bucket/testfile.txt --version-id=<version_id_cũ> /tmp/verify_version.txt
md5sum /tmp/verify_version.txt
```

**Tiêu chí Pass:**
- [ ] Toàn bộ version đầy đủ như baseline, nội dung khớp checksum.

---

#### TC-POST-017: Deep-scrub toàn cụm | P1

**Mục tiêu:** Đây là bước kiểm tra quan trọng nhất để xác nhận quá trình convert OMAP 2 lớp không làm hỏng dữ liệu ở tầng object/PG — bằng chứng cuối cùng và đáng tin cậy nhất cho toàn bộ path rủi ro cao này.

**Các bước thực hiện:**
```bash
ceph osd deep-scrub all
watch -n30 'ceph pg dump | grep -c scrubbing'
ceph health detail | grep -i inconsistent
```
Nếu có PG `inconsistent`, chạy `ceph pg repair <pgid>` và **bắt buộc điều tra kỹ nguyên nhân** trước khi đóng test case — vì đây là dấu hiệu tiềm ẩn liên quan đến quá trình convert 2 lớp, không được xem nhẹ.

**Tiêu chí Pass:**
- [ ] `ceph health detail` không báo `inconsistent`.
- [ ] 100% PG hoàn tất deep-scrub ít nhất 1 lần kể từ sau nâng cấp.

---

### 3.3 Cảnh báo & cấu hình mới sau nâng cấp

#### TC-POST-018: Siết bảo mật `global_id` | P1

**Các bước thực hiện:**
```bash
ceph health detail | grep -i global_id
ceph features
ceph config set mon auth_allow_insecure_global_id_reclaim false
ceph health detail | grep -i global_id
```

**Tiêu chí Pass:**
- [ ] Cảnh báo biến mất.
- [ ] Không client nào bị từ chối kết nối sau khi siết.

---

#### TC-POST-019: Cảnh báo OMAP đã hết (cả 2 loại) | P1

**Mục tiêu:** Đây là điểm khác biệt cốt lõi so với 2 path đi tuần tự — phải xác nhận **cả 2 cảnh báo cùng biến mất**, vì cụm đã convert đồng thời cả per-pool và per-PG trong 1 đợt.

**Các bước thực hiện:**
```bash
ceph health detail | grep -i omap
ceph df detail | grep -i omap
```

**Tiêu chí Pass:**
- [ ] Không còn **cả** `BLUESTORE_NO_PER_POOL_OMAP` **và** `BLUESTORE_NO_PER_PG_OMAP` trong output.

---

#### TC-POST-020: pg_autoscaler đúng trạng thái | P2

**Các bước thực hiện:**
```bash
ceph osd pool autoscale-status
```

**Tiêu chí Pass:**
- [ ] Trạng thái autoscale mỗi pool khớp baseline.
- [ ] Không gợi ý thay đổi PG_NUM đột biến chưa review.

---

#### TC-POST-021: Balancer hoạt động | P2

**Các bước thực hiện:**
```bash
ceph balancer status
ceph balancer eval
```

**Tiêu chí Pass:**
- [ ] Mode giữ nguyên (`upmap`); score không xấu đi.

---

#### TC-POST-022: Module MGR | P2

**Mục tiêu:** Phát hiện module nào bị đổi tên/loại bỏ tích luỹ qua cả 2 thế hệ Octopus + Pacific (do bỏ qua Octopus, các thay đổi giữa chừng có thể không được nhận biết ngay lập tức).

**Các bước thực hiện:**
```bash
ceph mgr module ls
```

**Tiêu chí Pass:**
- [ ] Mọi module trước nâng cấp vẫn bật hoặc có ghi chú rõ lý do đổi tên/thay thế qua cả 2 thế hệ.

---

#### TC-POST-023: Dashboard hoạt động | P2

**Các bước thực hiện:**
1. Truy cập `https://<mgr_active>:8443`.
2. Đăng nhập admin.
3. Kiểm tra các trang Hosts, OSDs, Pools, RGW, Cluster status.

**Lưu ý:** Cú pháp tạo user Dashboard đổi ở Pacific: `ceph dashboard ac-user-create <user> -i <file_mật_khẩu> <role>` (không truyền mật khẩu trực tiếp trên CLI như ở Nautilus).

**Tiêu chí Pass:**
- [ ] Đăng nhập thành công; mỗi trang hiển thị đúng số liệu, không lỗi 500.

---

#### TC-POST-024: Prometheus/Grafana | P2

**Các bước thực hiện:**
```bash
curl http://<mgr_active>:9283/metrics | head -50
```

**Tiêu chí Pass:**
- [ ] Trả về metric hợp lệ.
- [ ] Grafana không có panel trống do đổi tên metric tích luỹ qua 2 thế hệ.

---

#### TC-POST-025: Telemetry | P3

**Các bước thực hiện:**
```bash
ceph telemetry status
ceph telemetry on   # nếu chấp nhận opt-in kênh mới
```

**Tiêu chí Pass:**
- [ ] Trạng thái telemetry rõ ràng, không treo lơ lửng.

---

### 3.4 Kiểm thử chức năng (Regression)

#### TC-POST-030: Tạo/xoá pool | P1

```bash
ceph osd pool create regression_test_pool 32 32
ceph osd pool application enable regression_test_pool rbd
ceph osd pool ls detail | grep regression_test_pool
ceph osd pool delete regression_test_pool regression_test_pool --yes-i-really-really-mean-it
```

**Tiêu chí Pass:**
- [ ] Tạo/xoá thành công, PG mới chuyển `active+clean`.

---

#### TC-POST-031: Tạo/xoá RBD image | P1

```bash
rbd create rbd_rep/regression_image --size 5G
rbd resize rbd_rep/regression_image --size 10G
rbd map rbd_rep/regression_image
mkfs.xfs /dev/rbd2
mount /dev/rbd2 /mnt/regression
umount /mnt/regression
rbd unmap /dev/rbd2
rbd rm rbd_rep/regression_image
```

**Tiêu chí Pass:**
- [ ] Toàn bộ lệnh chạy thành công.

---

#### TC-POST-032: RBD snapshot/clone mới | P1

```bash
rbd create rbd_rep/snap_test --size 5G
rbd snap create rbd_rep/snap_test@s1
rbd snap protect rbd_rep/snap_test@s1
rbd clone rbd_rep/snap_test@s1 rbd_rep/snap_test_clone
rbd flatten rbd_rep/snap_test_clone
rbd snap unprotect rbd_rep/snap_test@s1
rbd snap rm rbd_rep/snap_test@s1
rbd rm rbd_rep/snap_test_clone
rbd rm rbd_rep/snap_test
```

**Tiêu chí Pass:**
- [ ] Toàn bộ chuỗi lệnh thành công.

---

#### TC-POST-033: RBD mirroring (nếu dùng) | P2

```bash
rbd mirror pool status --verbose
```

**Tiêu chí Pass:**
- [ ] `up+replaying` cho toàn bộ image, không image `error`.

---

#### TC-POST-034: Ghi/đọc CephFS mới | P1

```bash
for i in $(seq 1 10000); do echo "data-$i" > /mnt/verify_cephfs/regression_$i.txt; done
for i in $(seq 1 10000); do cat /mnt/verify_cephfs/regression_$i.txt > /dev/null; done
rm -f /mnt/verify_cephfs/regression_*.txt
ceph health detail | grep -i "slow metadata"
```

**Tiêu chí Pass:**
- [ ] Toàn bộ thao tác hoàn tất không lỗi, không cảnh báo `slow metadata IO`.

---

#### TC-POST-035: CephFS multi-MDS | P2

```bash
ceph fs set <fs_name> max_mds 2
ceph fs status
setfattr -n ceph.dir.pin -v 0 /mnt/verify_cephfs/dir_a
setfattr -n ceph.dir.pin -v 1 /mnt/verify_cephfs/dir_b
```

**Tiêu chí Pass:**
- [ ] 2 rank đều `up:active`, metadata phân bổ cân bằng.

---

#### TC-POST-036: Failover MDS | P1

```bash
ceph fs status
systemctl stop ceph-mds@<mds_active_hostname>
watch -n1 'ceph fs status'
```
Tiếp tục vòng lặp I/O CephFS (giống TC-RUN-002) để đo gián đoạn thực tế.

**Tiêu chí Pass:**
- [ ] Standby tiếp quản ≤ 30 giây; client không báo lỗi.

---

#### TC-POST-037: Thao tác S3 đầy đủ | P1

```bash
aws --endpoint-url http://<VIP_RGW>:8080 s3api put-object --bucket regression-bucket --key test.txt --body test.txt
aws --endpoint-url http://<VIP_RGW>:8080 s3api get-object --bucket regression-bucket --key test.txt /tmp/downloaded.txt
aws --endpoint-url http://<VIP_RGW>:8080 s3api list-objects --bucket regression-bucket
aws --endpoint-url http://<VIP_RGW>:8080 s3api delete-object --bucket regression-bucket --key test.txt
# Multipart upload, copy, presigned URL, versioning, lifecycle tương tự
```

**Tiêu chí Pass:**
- [ ] Toàn bộ thao tác trả về mã HTTP đúng chuẩn S3; nội dung tải xuống khớp upload.

---

#### TC-POST-038: Quản trị RGW | P2

```bash
radosgw-admin user create --uid=regression-user --display-name="Regression Test"
radosgw-admin user info --uid=regression-user
radosgw-admin quota set --uid=regression-user --max-size=10G --quota-scope=user
radosgw-admin bucket link --bucket=regression-bucket --uid=regression-user
radosgw-admin bucket reshard --bucket=regression-bucket --num-shards=8
```

**Tiêu chí Pass:**
- [ ] Toàn bộ lệnh chạy thành công.

---

#### TC-POST-039: RGW multisite sync | P1

**Mục tiêu:** Đặc biệt quan trọng ở path này vì định dạng data log RGW thay đổi tích luỹ qua cả 2 thế hệ — cần xác nhận sync giữa các zone vẫn hoạt động đúng.

```bash
aws --endpoint-url http://<zoneA_endpoint> s3api put-object --bucket sync-test --key sync-check.txt --body sync-check.txt
sleep 60
aws --endpoint-url http://<zoneB_endpoint> s3api get-object --bucket sync-test --key sync-check.txt /tmp/synced.txt
md5sum /tmp/synced.txt sync-check.txt
radosgw-admin sync status
```

**Tiêu chí Pass:**
- [ ] Object xuất hiện ở zone B ≤ 5 phút, nội dung khớp; `sync status` = caught up.

---

#### TC-POST-040: Thêm OSD mới | P1

```bash
ceph-volume lvm create --data /dev/sdX
ceph osd tree
watch -n5 ceph -s
```

**Tiêu chí Pass:**
- [ ] OSD mới `up/in`; backfill hoàn tất, `HEALTH_OK`.

---

#### TC-POST-041: Xoá OSD | P1

```bash
ceph osd out osd.<id>
watch -n5 ceph -s
ceph osd purge osd.<id> --yes-i-really-mean-it
ceph osd tree
```

**Tiêu chí Pass:**
- [ ] Rebalance hoàn tất không mất PG; purge thành công.

---

#### TC-POST-042: Thay MON | P2

```bash
ceph mon remove <mon_id>
ceph -s
ceph mon add <mon_id> <ip>:<port>
ceph quorum_status
```

**Tiêu chí Pass:**
- [ ] Quorum không mất khi xoá; đủ 3/3 sau khi thêm lại.

---

#### TC-POST-043: Restart toàn cụm | P1

```bash
ceph osd set noout
for host in $(cat hosts.txt); do
  ssh "$host" "reboot"
  sleep 120
  ssh "$host" "systemctl is-enabled ceph.target"
  ceph -s
done
ceph osd unset noout
```

**Tiêu chí Pass:**
- [ ] Mỗi node tự khởi động daemon sau reboot; cụm về `HEALTH_OK` sau toàn bộ.

---

#### TC-POST-044: Erasure code hoạt động | P1

```bash
rbd map rbd_ec/ec_test_image
mount /dev/rbd3 /mnt/ec_verify
echo "before failure" > /mnt/ec_verify/test.txt
systemctl stop ceph-osd@<id1>
systemctl stop ceph-osd@<id2>
cat /mnt/ec_verify/test.txt
systemctl start ceph-osd@<id1>
systemctl start ceph-osd@<id2>
watch -n5 ceph -s
```

**Tiêu chí Pass:**
- [ ] Đọc được khi mất 2 OSD (EC 4+2); recovery hoàn tất sau khi bật lại.

---

#### TC-POST-045: Script vận hành nội bộ | P2

**Mục tiêu:** Kiểm tra script monitoring/tự động hoá nội bộ tương thích với format output thay đổi tích luỹ qua **cả 2 thế hệ** (Octopus + Pacific) — rủi ro không tương thích cao hơn vì bỏ qua bước kiểm chứng trung gian ở Octopus.

**Các bước thực hiện:** Chạy toàn bộ script monitoring/backup/report nội bộ hiện có, đối chiếu kết quả; đặc biệt kiểm tra script parse `ceph df`, `ceph osd df`, `ceph -s --format json`.

**Tiêu chí Pass:**
- [ ] Script chạy đúng, hoặc điểm không tương thích đã ghi nhận kèm kế hoạch cập nhật.

---

### 3.5 Tương thích client

#### TC-COMPAT-001: Client 14.2.22 → cụm 16.2.15 | P1

**Mục tiêu:** Xác nhận client chênh lệch 2 major release vẫn hoạt động (Ceph hỗ trợ tương thích ngược nhưng cần kiểm chứng thực tế do đây là khoảng cách version lớn hơn thông thường).

**Các bước thực hiện:** Tiếp tục tải ở TC-RUN-010 thêm ≥1 giờ sau khi cụm `HEALTH_OK`.

**Tiêu chí Pass:**
- [ ] 0 lỗi trong suốt 1 giờ bổ sung.
- [ ] Ghi nhận rõ các lệnh CLI mới trên client không có sẵn (chấp nhận được, không phải lỗi).

---

#### TC-COMPAT-002: Kernel RBD client | P1

```bash
uname -r
rbd map rbd_rep/testimage1
dd if=/dev/zero of=/dev/rbd0 bs=1M count=100 oflag=direct
rbd unmap /dev/rbd0
```

**Tiêu chí Pass:**
- [ ] Map/unmap, ghi dữ liệu thành công trên các phiên bản kernel test (4.18/5.4).

---

#### TC-COMPAT-003: Kernel CephFS client | P1

```bash
mount -t ceph <mon_ip>:/ /mnt/kernel_cephfs -o name=admin,secretfile=/etc/ceph/admin.secret
dd if=/dev/zero of=/mnt/kernel_cephfs/test.bin bs=1M count=100
umount /mnt/kernel_cephfs
```

**Tiêu chí Pass:**
- [ ] Mount/umount thành công, không có client bị blocklist.

---

#### TC-COMPAT-004: `ceph-fuse` phiên bản cũ | P2

```bash
ceph-fuse -m <mon_ip> /mnt/fuse_old
echo test > /mnt/fuse_old/test.txt
cat /mnt/fuse_old/test.txt
fusermount -u /mnt/fuse_old
```

**Tiêu chí Pass:**
- [ ] Mount/umount/I-O thành công.

---

#### TC-COMPAT-005: Tích hợp OpenStack | P1

```bash
openstack volume create --size 10 test-volume
openstack server create --image <image_id> --flavor <flavor_id> --boot-from-volume test-volume --nic net-id=<net_id> test-instance
openstack volume snapshot create --volume test-volume test-snapshot
```

**Tiêu chí Pass:**
- [ ] Volume/instance ở trạng thái `available`/`ACTIVE`.

---

#### TC-COMPAT-006: Tích hợp Kubernetes (Ceph-CSI) | P1

```bash
kubectl apply -f pvc-rbd-test.yaml
kubectl apply -f pvc-cephfs-test.yaml
kubectl get pvc
kubectl apply -f pod-using-pvc.yaml
kubectl exec test-pod -- sh -c "echo test > /data/test.txt"
kubectl delete -f pvc-rbd-test.yaml -f pvc-cephfs-test.yaml
```

**Tiêu chí Pass:**
- [ ] PVC `Bound`, pod đọc/ghi thành công, xoá không lỗi.

---

#### TC-COMPAT-007: S3 SDK | P1

```python
import boto3
s3 = boto3.client('s3', endpoint_url='http://<VIP_RGW>:8080',
                   aws_access_key_id='<key>', aws_secret_access_key='<secret>')
s3.put_object(Bucket='sdk-test', Key='test.txt', Body=b'hello')
obj = s3.get_object(Bucket='sdk-test', Key='test.txt')
print(obj['Body'].read())
```

**Tiêu chí Pass:**
- [ ] Toàn bộ thao tác cơ bản (put/get/list/delete) thành công.

---

#### TC-COMPAT-008: `require-min-compat-client` | P2

```bash
ceph osd dump | grep min_compat_client
ceph features
```

**Tiêu chí Pass:**
- [ ] Giá trị hợp lệ, không từ chối client hợp pháp đang hoạt động.

---

### 3.6 Hiệu năng sau nâng cấp

#### TC-PERF-001–003: IOPS, băng thông, latency RBD | P1

```bash
fio --name=perf_iops --filename=/dev/rbd0 --ioengine=libaio --direct=1 \
    --rw=randrw --rwmixread=70 --bs=4k --iodepth=32 --numjobs=4 \
    --time_based --runtime=300 --output-format=json --output=/tmp/perf_iops_after.json

fio --name=perf_bw --filename=/dev/rbd0 --ioengine=libaio --direct=1 \
    --rw=write --bs=4M --iodepth=16 --numjobs=1 \
    --time_based --runtime=300 --output-format=json --output=/tmp/perf_bw_after.json
```

**Tiêu chí Pass:**
- [ ] IOPS/băng thông suy giảm ≤ 10% so với baseline.
- [ ] Latency p99 tăng ≤ 15%.

---

#### TC-PERF-004: Throughput object layer | P2

```bash
rados bench -p rbd_rep 300 write --no-cleanup
rados bench -p rbd_rep 300 seq
rados bench -p rbd_rep 300 rand
rados -p rbd_rep cleanup
```

**Tiêu chí Pass:**
- [ ] Suy giảm ≤ 10% so với baseline.

---

#### TC-PERF-005: Hiệu năng RGW | P2

```bash
warp mixed --host=<VIP_RGW>:8080 --access-key=<key> --secret-key=<secret> \
  --bucket=perf-test --duration=4h --concurrent=50 --obj.size=1MiB \
  > /tmp/warp_perf_after.log
```

**Tiêu chí Pass:**
- [ ] Suy giảm ≤ 15% so với baseline.

---

#### TC-PERF-006: Metadata CephFS | P2

```bash
mdtest -d /mnt/verify_cephfs/mdtest_dir -n 10000 -i 3
```

**Tiêu chí Pass:**
- [ ] Suy giảm ≤ 15% so với baseline.

---

#### TC-PERF-007: Thời gian recovery | P2

```bash
date +%s > /tmp/recovery_start
systemctl stop ceph-osd.target
while ! ceph -s | grep -q "HEALTH_OK"; do sleep 10; done
date +%s > /tmp/recovery_end
echo "Recovery time: $(($(cat /tmp/recovery_end) - $(cat /tmp/recovery_start))) giây"
```

**Tiêu chí Pass:**
- [ ] Không chậm hơn baseline quá 20%.

---

#### TC-PERF-008: Mức tiêu thụ RAM OSD | P2

```bash
ceph daemon osd.0 dump_mempools | jq '.mempool.total_bytes'
ceph config get osd.0 osd_memory_target
```

**Tiêu chí Pass:**
- [ ] Không vượt `osd_memory_target` quá 20%.

---

#### TC-PERF-009: Ổn định dài hạn (soak test) | P1

**Mục tiêu:** Xác nhận cụm ổn định lâu dài sau khi trải qua bước nhảy 2 major, không có suy giảm/rò rỉ tiềm ẩn xuất hiện muộn.

**Các bước thực hiện:**
1. Chạy đồng thời `fio`, script I/O CephFS, `warp` liên tục 72 giờ.
2. Theo dõi mỗi 4 giờ: `ceph -s`, `ceph crash ls-new`, RAM/CPU từng OSD.
3. Tổng hợp báo cáo cuối.

**Tiêu chí Pass:**
- [ ] Không crash trong 72 giờ.
- [ ] Không PG lỗi phát sinh.
- [ ] RAM dao động ổn định, không tăng đơn điệu (dấu hiệu rò rỉ).

---

## 4. Biểu mẫu ghi kết quả

| Test Case ID | Ngày thực hiện | Người thực hiện | Kết quả thực tế | Pass/Fail | Defect ID | Ghi chú |
|---|---|---|---|---|---|---|
| TC-RUN-001 | | | | | | |
| TC-RUN-013 | | | | | | |
| TC-POST-017 | | | | | | |
| … | | | | | | |

**Bảng tổng hợp**

| Nhóm | Tổng số TC | Pass | Fail | Blocked | N/A | Tỉ lệ Pass |
|---|---|---|---|---|---|---|
| RUN (trong lúc nâng cấp) | 13 | | | | | |
| POST (sau nâng cấp) | 33 | | | | | |
| COMPAT | 8 | | | | | |
| PERF | 9 | | | | | |
| **Tổng** | **63** | | | | | |

---

## 5. Tiêu chí kết thúc (Exit Criteria)

- [ ] 100% test P1 PASS, ≥ 95% test P2 PASS.
- [ ] 0 defect Critical/Blocker còn mở.
- [ ] 100% checksum khớp baseline trên RBD/CephFS/RGW.
- [ ] `ceph versions` cho thấy toàn bộ daemon ở 16.2.15.
- [ ] `ceph -s` = `HEALTH_OK` (hoặc chỉ còn cảnh báo đã giải thích & chấp nhận).
- [ ] Suy giảm hiệu năng sau nâng cấp ≤ 10% so với baseline.
- [ ] Soak test 72 giờ không phát sinh crash/PG lỗi/rò rỉ bộ nhớ.
- [ ] **Deep-scrub xác nhận 0 PG inconsistent** — bằng chứng cuối cùng rằng convert OMAP 2 lớp không làm hỏng dữ liệu.
- [ ] TC-RUN-013 (convert OMAP 2 lớp) đã hoàn tất trên 100% OSD, không có OSD nào phải chạy lại do lỗi.

---

## 6. Ghi chú tổng kết rủi ro riêng của path này

So với 2 path đi tuần tự (14.2.22→15.2.17 và 15.2.17→16.2.15), path nhảy thẳng này có 3 điểm khác biệt cần đặc biệt lưu ý khi đọc kết quả test:

1. **TC-RUN-013 và TC-POST-019** là 2 test case sống còn — nếu 1 trong 2 fail, khả năng cao dữ liệu OMAP đã bị ảnh hưởng và cần điều tra sâu trước khi công bố nâng cấp thành công.
2. **Ngưỡng downtime OSD được nới rộng lên 180 giây** (so với 120 giây ở path 1-major) — đây là điều chỉnh có chủ đích, không phải sai sót, nhưng nếu thời gian thực tế vượt xa ngưỡng này nhiều lần, cần xem xét lại phương án đi tuần tự qua Octopus cho lần nâng cấp production thật.
3. **TC-POST-017 (deep-scrub)** nên được chạy sớm nhất có thể sau khi hoàn tất, không nên trì hoãn — vì đây là bằng chứng kỹ thuật duy nhất xác nhận không có silent data corruption từ quá trình convert 2 lớp.
