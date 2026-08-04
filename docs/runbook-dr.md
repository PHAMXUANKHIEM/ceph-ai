# Runbook: Khôi phục cụm Ceph sau thảm hoạ (Disaster Recovery)

Runbook thao tác cho tính năng **Restore Cluster** (`restore_cluster_from_backup`,
Story 9.7) — dùng khi cụm Ceph cũ đã **sập hoàn toàn, không còn khả năng cứu
tại chỗ**, cần dựng lại từ đầu trên node mới rồi khôi phục dữ liệu từ các bản
backup đã có. Đây là tài liệu được `dashboard/routes/restore_cluster.py`
tham chiếu trực tiếp trên giao diện Dashboard trước khi vận hành viên bấm
"Duyệt".

Muốn hiểu tổng thể hệ thống backup (không chỉ riêng DR) — kiến trúc lưu
trữ, retention, giám sát AI — xem **[ceph-backup.md](./ceph-backup.md)**
trước.

Nguồn: `dashboard/routes/restore_cluster.py`, `worker/executor/
cluster_deploy.py` (phần "Khôi phục cụm sau thảm họa"), `worker/backup/
restore.py`, `worker/backup/metadata.py`.

## 1. Khi nào dùng runbook này

**DÙNG khi:** toàn bộ cụm cũ (MON/MGR/OSD) không còn khởi động được, hoặc
không còn node vật lý/VM nào giữ được dữ liệu cũ — cần dựng một cụm HOÀN
TOÀN MỚI trên node khác rồi nạp lại dữ liệu.

**KHÔNG dùng khi:** cụm vẫn chạy bình thường, chỉ MỘT volume bị hỏng/mất dữ
liệu — trường hợp đó dùng nút **"Khôi phục"** trên trang **Backups**
(`restore_rbd_image_to_production`) thay vì runbook này — xem mục 8.1 của
[ceph-backup.md](./ceph-backup.md). Restore Cluster **dựng lại toàn bộ hạ
tầng cụm**, không phải cách để sửa một image đơn lẻ.

## 2. Điều kiện tiên quyết — kiểm tra TRƯỚC khi bắt đầu

1. **Có ít nhất một bản backup metadata thành công** (`backup_metadata_run`)
   — nếu chưa từng chạy job này thành công lần nào, bước 11 (khôi phục
   auth/CRUSH map) sẽ thất bại ngay từ đầu, dừng toàn bộ tiến trình.
2. **Có ít nhất một bản backup full thành công cho MỖI ảnh trong
   `tracked_images:`** (`worker/policy/backup_policy.yaml`) — ảnh nào không
   có bản full nào thành công sẽ làm bước 12 thất bại ngay khi tới lượt ảnh
   đó (dừng cả tiến trình, không bỏ qua rồi tiếp tục các ảnh khác).
3. **Node mới đã chuẩn bị sẵn**, SSH được từ Dashboard/Worker, đúng số
   lượng vai trò tối thiểu: **≥1 MON, ≥1 MGR, ≥1 OSD** (MDS tuỳ chọn). Mỗi
   node có role OSD phải khai rõ **đĩa OSD** sẽ dùng.
4. **Cùng số lượng/vai trò MON như cụm gốc là khuyến nghị mạnh** — bước
   khôi phục monmap (mục 5, bước 11) coi member MON của cụm mới **đã khớp**
   với backup theo đúng danh sách vận hành viên nhập vào; runbook không tự
   đối chiếu ngược lại số MON của cụm gốc.
5. Đã đọc kỹ mục 6 (rủi ro) — đặc biệt phần khôi phục monmap có thể **không
   thành công do khác fsid**, và điều đó vẫn được coi là "không chặn toàn bộ
   tiến trình" (best-effort).

## 3. Cách thao tác trên Dashboard

1. Vào trang **Restore Cluster**, điền:
   - Phiên bản Ceph (`x.y.z`, phải khớp phiên bản các bản backup được tạo
     ra để tương thích).
   - Bảng node mới: IP, vai trò (mon/mgr/osd/mds), đĩa OSD cho node có role
     osd.
   - Mạng public/cluster network (để trống cluster_network thì dùng chung
     public_network).
2. Bấm **"Đề xuất"** → hệ thống hiện toàn bộ kế hoạch 13 bước (mục 5) +
   cảnh báo, kèm lệnh xem trước.
3. Bấm **Duyệt** — như mọi action RISKY khác, Dashboard chỉ đổi trạng thái,
   Worker mới thực sự thực thi qua SSH.
4. Theo dõi tiến độ real-time theo từng bước tại chính trang Restore
   Cluster (`/restore-cluster/progress`).
5. Trong lúc đang có một đề xuất Restore Cluster đang chờ duyệt/đã duyệt,
   **không thể tạo thêm** bất kỳ đề xuất dựng/xoá/chuyển đổi/khôi phục cụm
   nào khác (khoá lẫn nhau giữa toàn bộ họ `cluster_deploy_action_ids`) —
   và ngược lại.

## 4. Cơ chế bên dưới: tái sử dụng gần như nguyên vẹn "Dựng cụm"

`restore_cluster_from_backup` **không phải một orchestrator riêng** — nó
lấy **nguyên vẹn toàn bộ danh sách phase** của `deploy_cluster_ceph_deploy`
(dựng cụm trống bằng phương thức ceph-deploy) rồi **nối thêm 3 phase khôi
phục dữ liệu** vào cuối:

```python
_PHASES_BY_ACTION_ID["restore_cluster_from_backup"] = (
    _PHASES_BY_ACTION_ID["deploy_cluster_ceph_deploy"] + [
        ("restore_metadata", ...),
        ("restore_rbd_images", ...),
        ("verify_integrity", ...),  # tên hiển thị khác, xem mục 5 bước 13
    ]
)
```

Nói cách khác: **10 bước đầu tiên hệt như trang Dựng cụm, phương thức
ceph-deploy** (không có lựa chọn cephadm hay rpm-local nào khác cho DR) —
cùng logic, cùng khả năng thất bại/rủi ro như dựng một cụm mới hoàn toàn.
Chỉ 3 bước cuối là logic mới, viết riêng cho DR.

## 5. Chi tiết 13 bước

| # | Bước | Nội dung |
|---|---|---|
| 1 | `ssh_check` | Kiểm tra SSH + hệ thống từng node |
| 2 | `dependencies` | Cài `chrony`, tắt `firewalld`/SELinux |
| 3 | `repo` | Cấu hình repo gói Ceph (`download.ceph.com`) |
| 4 | `packages` | Cài gói Ceph theo đúng vai trò từng node |
| 5 | `mon_init` | Khởi tạo MON: fsid **MỚI**, monmap, keyring, mkfs |
| 6 | `wait_quorum` | Chờ các MON đạt quorum |
| 7 | `mon_security` | Bật `msgr2`, tắt `insecure global-id-reclaim` |
| 8 | `mgr_create` | Tạo MGR |
| 9 | `osd_create` | Tạo OSD (`ceph-volume lvm create`) trên đĩa đã khai |
| 10 | `verify` | `ceph -s` — dừng nếu `HEALTH_ERR` |
| 11 | `restore_metadata` | Khôi phục auth keys + CRUSH map (chắc chắn); cố khôi phục monmap (best-effort) |
| 12 | `restore_rbd_images` | Khôi phục từng ảnh trong `tracked_images` (full + toàn bộ chain incremental) |
| 13 | `verify_integrity`* | Đối chiếu kích thước từng ảnh sau khôi phục với bản full gốc |

\* Tên hiển thị trong danh sách phase, tên hàm nội bộ là
`_phase_verify_integrity`.

Tới hết bước 10, cụm mới đã là **một cụm Ceph trống, khoẻ mạnh, fsid hoàn
toàn mới** — chưa có auth/CRUSH map/dữ liệu gì từ cụm cũ. Bước 11–13 mới
thực sự "khôi phục sau thảm hoạ".

### Bước 11 — Khôi phục metadata (auth, CRUSH map, monmap)

Tải bản backup metadata **thành công gần nhất** (`backup_metadata_run`),
thực hiện theo thứ tự:

1. `ceph auth import -i auth_export.txt` trên MON đầu tiên — khôi phục
   toàn bộ auth key của cụm cũ.
2. `ceph osd setcrushmap -i crushmap.bin` trên MON đầu tiên — khôi phục
   CRUSH map (topology/rule đặt dữ liệu) của cụm cũ.
3. Với **từng MON**, lần lượt (không song song — để cụm không bao giờ mất
   quorum hoàn toàn giữa chừng): dừng `ceph-mon`, tiêm (`--inject-monmap`)
   monmap của cụm cũ, khởi động lại.

**Bước tiêm monmap là best-effort, CHƯA được kiểm chứng trên cụm lab thật.**
File `monmap.bin` tải về mang **fsid của cụm CŨ**, trong khi mon vừa được
`mon_init` (bước 5) khởi tạo với **fsid MỚI hoàn toàn** — Ceph được tài liệu
hoá là từ chối `--inject-monmap` nếu fsid không khớp fsid cục bộ, và hành vi
này có thể khác nhau giữa các phiên bản. Nếu bước này thất bại trên một MON,
lỗi chỉ được **log cảnh báo**, không dừng toàn bộ tiến trình — vì thành
viên MON của cụm mới về danh sách IP vốn đã khớp với backup (vận hành viên
được yêu cầu dựng lại đúng node list cũ), nên coi việc tiêm monmap thất bại
là chấp nhận được **miễn là vận hành viên biết để kiểm tra lại thủ công**,
không phải một lỗi âm thầm bị bỏ qua.

### Bước 12 — Khôi phục dữ liệu RBD

Với **mỗi entry** trong `tracked_images:`:

1. Nếu pool đích chưa tồn tại trên cụm mới → tự tạo (`ceph osd pool create`
   + `rbd pool init`).
2. Xác định slot lưu trữ (`a`/`b`) đang giữ bản full gần nhất thành công
   của ảnh đó.
3. Gọi `worker/backup/restore.py::restore_image()` — **dùng chung logic**
   với nút "Khôi phục" một image trên trang Backups (mục 8.1 của
   ceph-backup.md): tải bản full + toàn bộ chuỗi incremental theo đúng thứ
   tự tạo, mỗi file tải về đều verify SHA256/kích thước trước khi
   `rbd import`/`import-diff`.
4. **Bất kỳ ảnh nào thất bại đều dừng toàn bộ tiến trình ngay** (không âm
   thầm bỏ qua rồi tiếp tục ảnh sau) — một DR mà chỉ khôi phục được một
   phần dữ liệu, báo "xong" là nguy hiểm hơn nhiều so với dừng lại rõ ràng
   để vận hành viên biết chính xác ảnh nào có vấn đề.

### Bước 13 — Đối chiếu tính toàn vẹn

Với mỗi ảnh vừa khôi phục: so sánh **kích thước logic** (`rbd info`) của
ảnh trên cụm mới với `size_bytes` đã ghi nhận khi bản full gốc được tạo.
Vì một `rbd export` đầy đủ của một ảnh ghi ra đúng bằng kích thước logic
của ảnh đó, đây là một **phép kiểm tra toàn vẹn thật**, không phải ước
lượng gần đúng — khớp kích thước tuyệt đối là điều kiện để coi bước này
thành công (thông báo lỗi nội bộ dùng chữ "checksum KHÔNG khớp" nhưng thực
chất đang so kích thước, không phải băm lại toàn bộ nội dung — băm SHA256
đã được xác nhận riêng ở TỪNG file tải về trong bước 12, đây là lớp kiểm
tra cuối cùng ở mức toàn ảnh sau khi ráp xong).

## 6. Rủi ro & giới hạn cần biết trước khi chạy thật

- **Chưa từng chạy trên một cụm lab thật trong phiên phát triển tính năng
  này** (ghi rõ trong chính mã nguồn) — nên diễn tập trên môi trường không
  quan trọng ít nhất một lần trước khi cần dùng thật trong một sự cố.
- **Chỉ khôi phục MON/MGR/OSD** — giống mọi phase-runner khác trong
  `cluster_deploy.py`, không có RGW/MDS. Nếu cụm cũ có RGW/MDS, dữ liệu cấu
  hình các daemon đó **không nằm trong phạm vi khôi phục tự động** này.
- **Chỉ dựng lại bằng phương thức ceph-deploy** (không phải cephadm) — cụm
  được khôi phục sẽ ở `CEPH_EXEC_MODE=none`, dù cụm gốc trước khi sập có thể
  đã ở `cephadm`. Muốn chuyển sang cephadm sau khi DR xong, chạy tiếp tính
  năng **Convert to Cephadm** riêng (xem
  [convert-ceph-deploy-to-cephadm.md](./convert-ceph-deploy-to-cephadm.md)).
- **fsid của cụm mới luôn khác fsid cụm cũ** — bất kỳ hệ thống/script bên
  ngoài nào tham chiếu fsid cũ (client config, monitoring khác) đều cần cập
  nhật lại thủ công sau DR; đây không phải điều runbook/tính năng này tự xử
  lý.
- **Khôi phục monmap là best-effort** (mục 5, bước 11) — luôn kiểm tra lại
  `ceph mon stat`/`ceph -s` thủ công sau khi DR xong, đừng chỉ tin vào
  "bước 11 done" trên thanh tiến độ.
- Bước 12 chỉ khôi phục **những ảnh đã khai trong `tracked_images:`** tại
  THỜI ĐIỂM chạy DR — một ảnh vận hành viên quên thêm vào danh sách theo
  dõi trước khi cụm sập sẽ **không có bản backup nào để khôi phục**, bất kể
  runbook này chạy đúng thế nào.

## 7. Việc cần làm sau khi DR hoàn tất (không tự động)

1. Kiểm tra `ceph -s`/`ceph mon stat` thủ công — xác nhận monmap thực sự
   khớp mong đợi (mục 6).
2. Rà soát lại quyền truy cập ứng dụng/VM đang dùng các RBD image vừa khôi
   phục — auth key đã khôi phục đúng theo bản backup, nhưng client bên
   ngoài có thể cần cấu hình lại fsid/mon endpoint mới.
3. Nếu cụm gốc có RGW/MDS hoặc pool/ảnh KHÔNG nằm trong `tracked_images:`,
   xử lý khôi phục các phần đó theo quy trình thủ công riêng — nằm ngoài
   phạm vi tự động của tính năng này (mục 6).
4. Xác nhận lịch backup mới (`scheduler.py`) đã bắt đầu chạy lại bình
   thường trên cụm mới — dùng chung `tracked_images`/`backup_policy.yaml`,
   không cần cấu hình lại, nhưng nên xác nhận lần chạy kế tiếp diễn ra đúng
   giờ.
5. Nếu cụm gốc trước khi sập ở `cephadm`, cân nhắc chạy Convert to Cephadm
   (mục 6) để khôi phục đúng kiểu quản lý ban đầu.

## 8. Tham chiếu mã nguồn

| File | Vai trò |
|---|---|
| `dashboard/routes/restore_cluster.py` | Trang Dashboard, route đề xuất/theo dõi tiến độ |
| `worker/executor/cluster_deploy.py` | 3 phase DR (`_phase_restore_metadata`, `_phase_restore_rbd_images`, `_phase_verify_integrity`) + ghép nối phase list |
| `worker/backup/restore.py` | `restore_image()` — logic khôi phục full+diff chain dùng chung |
| `worker/backup/metadata.py` | `latest_successful_metadata_job()`/`download_artifact()` — nguồn dữ liệu bước 11 |
| `worker/policy/backup_policy.yaml` | `tracked_images:` — danh sách ảnh bước 12 sẽ khôi phục |
