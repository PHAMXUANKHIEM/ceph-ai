# Kế hoạch: tính năng còn thiếu cho cụm ceph-deploy

**Ngày lập:** 09/10/2026
**Trạng thái:** `IN-PROGRESS`. Mới rà soát; chưa giai đoạn nào được triển khai.
**Phạm vi:** cụm cài bằng ceph-deploy hoặc gói, tức `exec_mode = none`: lệnh `ceph` chạy thẳng trên MON, không có
`cephadm shell` và không có orchestrator `ceph orch`.

## 1. Hiện trạng (rà ở `origin/main`, 09/10/2026)

Mọi lệnh Ceph đi qua `build_exec_command` (`watcher/ceph_client.py`). Hàm này bọc lệnh theo `exec_mode`:
`cephadm` → `cephadm shell -- …`, `none` → chạy thẳng, `docker`/`podman` → `exec` vào container có sẵn.

**Đã chạy với ceph-deploy, không cần làm thêm:**

- giám sát health, inventory, incident, AI chẩn đoán, log intelligence, forecast, trang Object/Block;
- Deploy Cluster, Upgrade bằng gói (download.ceph.com hoặc gói cục bộ), Delete Cluster, Convert Cluster
  (ceph-deploy → cephadm);
- restart OSD (có nhánh systemd), thẻ Servers (dự phòng `ceph node ls`);
- phần chạy qua SSH tới host: node postmortem, node config audit, giãn nhịp SSH theo host.

**Còn thiếu hoặc kém:**

| # | Chỗ thiếu | Bằng chứng trong code |
|---|---|---|
| G1 | Remove Nodes | `worker/executor/node_removal.py`: "Gỡ node chỉ hỗ trợ cụm cephadm"; route `remove_nodes.py` cũng chặn |
| G2 | Danh sách và bằng chứng RGW daemon | `watcher/rgw_evidence.py`, `watcher/ceph_finding_verifier.py` chỉ dùng `ceph orch ps`; code tự ghi "legacy systemd có thể cần adapter riêng" |
| G3 | Restart RGW daemon (remediation) | `worker/executor/commands.py`: chỉ có `ceph orch daemon restart` |
| G4 | Tiến độ nâng cấp | Theo dõi dựa trên `ceph orch upgrade status` (`watcher/ceph_client.py`), chỉ có ở cephadm |
| G5 | Failure Lab dừng/chạy OSD | `shared/failure_lab_fault.py` dùng `ceph orch daemon stop/start` |
| G6 | Đọc số liệu qua mgr prometheus | ceph-deploy không tự bật module `prometheus` |

**Giới hạn hiện tại:** cụm thật đang giám sát (CS-LAB) là cephadm. Nhánh ceph-deploy mới được test bằng giả lập,
chưa chạy trên cụm ceph-deploy thật.

## 2. Nguyên tắc

- Không làm hỏng hành vi cephadm: mỗi thay đổi rẽ nhánh theo `exec_mode`, và có test cho cả hai.
- Thao tác thay đổi cụm vẫn đi qua phân loại hành động, approval và guardrail hiện có. Không có đường tắt cho ceph-deploy.
- Đọc trạng thái daemon bằng systemd unit là bằng chứng phụ. Trạng thái do Ceph báo (`osd tree`, `ceph -s`) vẫn là nguồn chính.
- Mỗi giai đoạn là một nhánh `cand/*`: test tự động, kiểm tra trên cụm lab, rồi duyệt qua Telegram.

## 3. Các giai đoạn

### Giai đoạn 0: cụm lab ceph-deploy (điều kiện tiên quyết)

- [ ] Operator thêm SSH key của máy Ceph AI vào VM lab 10.3.54.145. Nếu cần cụm nhiều node thì cấp thêm 1–2 VM.
- [ ] Dựng cụm bằng chính Deploy Cluster (nhánh ceph-deploy). Ghi lại phiên bản Ceph và hệ điều hành mà ceph-deploy còn hỗ trợ.
- [ ] Đăng ký cụm lab là cụm quan sát thứ hai, không phải cụm mặc định (xem sự cố 06/10 "Deploy Cluster cướp giám sát").
- **Nghiệm thu:** health, inventory, Object/Block và incident chạy được trên cụm lab, có ảnh chụp và log làm bằng chứng.

### Giai đoạn 1: adapter daemon kiểu systemd (nền móng)

- [ ] Module dùng chung liệt kê daemon trên host qua unit `ceph-mon@`, `ceph-mgr@`, `ceph-osd@`, `ceph-radosgw@` và `ceph-mds@`.
  Tái dùng `_discover_ceph_units` trong `worker/executor/commands.py`.
- [ ] Hành động `start`/`stop`/`restart` qua `systemctl`, tên unit được kiểm tra chặt (không đưa chuỗi tự do vào shell).
- [ ] Hàm chọn theo `exec_mode`: cephadm thì dùng `ceph orch`; `none` thì dùng adapter systemd; docker/podman giữ hành vi hiện tại.
- **Nghiệm thu:** unit test với output `systemctl` thật lấy từ cụm lab; test không dính cephadm. Kiểm tra trên lab:
  liệt kê đủ daemon của từng node.

### Giai đoạn 2: RGW trên ceph-deploy (G2, G3)

- [ ] `rgw_evidence` và `ceph_finding_verifier` lấy danh sách RGW qua adapter khi không có orchestrator.
- [ ] Hành động restart RGW dùng `systemctl restart ceph-radosgw@…` cho `exec_mode = none`, vẫn qua approval.
- **Nghiệm thu:** chẩn đoán một sự cố RGW giả lập trên lab có đủ bằng chứng daemon. Restart có duyệt chạy được và được xác minh lại.

### Giai đoạn 3: tiến độ nâng cấp cho đường gói (G4)

- [ ] Theo dõi từng host qua `ceph versions` và trạng thái daemon sau restart, thay cho `orch upgrade status`.
- [ ] Kiểm tra trước mỗi host: `ok-to-stop`, đủ quorum, cờ `noout` theo quy trình hiện có.
- **Nghiệm thu:** nâng cấp bản nhỏ trên cụm lab, trang Upgrade hiện đúng tiến độ từng host; rollback note.

### Giai đoạn 4: Remove Nodes cho ceph-deploy (G1, có thao tác phá hủy)

- [ ] Quy trình kiểu cũ, từng bước có duyệt như bản cephadm:
  `osd out` → chờ PG `active+clean` → `osd purge` → `ceph mon remove` → dừng và tắt unit →
  tùy chọn xóa đĩa/LUKS (tái dùng logic Delete Cluster).
- [ ] Kiểm tra an toàn trước mỗi bước: `ok-to-stop`, đủ quorum sau khi gỡ MON, đủ dung lượng nhận dữ liệu.
- [ ] Bỏ chặn ở `node_removal.py` và `remove_nodes.py` chỉ cho `exec_mode = none`. docker/podman vẫn chặn.
- **Nghiệm thu:** gỡ một node OSD và một node MON trên cụm lab không mất dữ liệu; test cho từng bước bị từ chối.

### Giai đoạn 5: Failure Lab và mgr prometheus (G5, G6)

- [ ] Failure Lab dừng/chạy OSD qua adapter khi không có orchestrator.
- [ ] Node config audit báo khi module `prometheus` của mgr chưa bật, kèm lệnh bật. Việc bật cần operator duyệt
  (thay đổi cụm).
- **Nghiệm thu:** một fault OSD có undo chạy trọn trên lab; audit báo đúng trước và sau khi bật module.

## 4. Thứ tự và phụ thuộc

0 → 1 → 2 → 3 → 4 → 5. Giai đoạn 1 test được bằng giả lập nên có thể làm song song với giai đoạn 0. Remove Nodes để sau
RGW và nâng cấp vì là thao tác phá hủy, cần adapter đã chạy ổn trên cụm lab trước.

## 5. Theo dõi

| Giai đoạn | Nhánh | Trạng thái |
|---|---|---|
| 0 | — | Chờ SSH key VM lab |
| 1 | — | Chưa bắt đầu |
| 2 | — | Chưa bắt đầu |
| 3 | — | Chưa bắt đầu |
| 4 | — | Chưa bắt đầu |
| 5 | — | Chưa bắt đầu |
