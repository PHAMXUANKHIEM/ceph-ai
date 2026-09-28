# Incomplete Plan — Dựng nhiều cụm Ceph trên cùng host MON

**Trạng thái:** `[ ] Chưa triển khai`  
**Ưu tiên:** P0 — safety và data isolation  
**Phạm vi:** Ceph deployment/executor/monitoring, không áp dụng cho Vitastor  
**Ngày lập kế hoạch:** 2026-09-28

## 1. Mục tiêu

Cho phép tool dựng và vận hành độc lập nhiều cụm Ceph trên cùng một tập host
MON, theo mô hình lab sau:

| Cụm | Phương thức | Ceph | Config | Data/state | MON ports |
|---|---|---|---|---|---|
| A | native package/systemd | 15.2.17 | `/etc/ceph/ceph.conf` | `/var/lib/ceph` | v1 `6789`, v2 `3300` |
| B | Docker thủ công | 15.2.13 / image `ceph/ceph:v15` | `/etc/cephB/ceph.conf` | `/var/lib/cephB` | v1 `6790`, v2 `3301` |

Ba node MON có thể dùng chung cho A và B. Các data node, OSD disk, keyring,
FSID, process/container, service name và port của hai cụm phải được phân tách
tuyệt đối.

Mục tiêu không chỉ là chạy được lệnh deploy lần đầu. Tool phải bảo đảm:

- không ghi đè hoặc xoá state của cụm khác;
- phát hiện conflict trước khi có mutation;
- resume được sau failure theo từng phase;
- xoá hoặc sửa cụm B không ảnh hưởng cụm A;
- Dashboard, Watcher, Worker, backup và audit đều biết đang thao tác cụm nào;
- mọi hành động destructive vẫn qua preview, approval, recheck và post-check.

## 2. Kết luận baseline hiện tại

Baseline hiện tại **không đạt** yêu cầu này và không được dùng để triển khai
hai cụm trên cùng MON host.

Các blocker đã xác định:

- `worker/executor/cluster_deploy.py` dùng cố định `/etc/ceph/ceph.conf`,
  `/var/lib/ceph` và `/var/lib/ceph/mon/ceph-<hostname>`.
- `_mkfs_and_start_mon_command()` có `rm -rf` trên MON data directory; chạy
  lại trên host đang có cụm khác có thể phá MON state của cụm đó.
- `cephadm bootstrap` hiện dọn toàn bộ FSID mà `cephadm ls` phát hiện, không
  giới hạn vào deployment đang được yêu cầu.
- MON port, cluster name, config root, data root, container name và image
  runtime chưa phải là tham số theo từng deployment.
- Phương thức có tên `ceph-deploy` hiện là orchestration native tự viết, không
  phải Docker thủ công và không tạo được `/etc/cephB`/`/var/lib/cephB`.
- Cấu hình vận hành được ghi vào một bộ `CEPH_MON_NODES`, `CEPH_MGR_NODES`,
  `CEPH_OSD_NODES`, `CEPH_EXEC_MODE`, `CEPH_KEYRING_PATH`; deploy cụm sau có
  thể ghi đè cụm trước.
- `run()` khởi tạo lại toàn bộ phase list từ đầu; progress hiện chỉ là log,
  chưa phải checkpoint resume có state identity của deployment.
- Multi-cluster hiện chỉ đủ cho observability giới hạn; deploy/remediation/
  backup/restore vẫn chưa có scope đầy đủ theo cluster.

**Release gate:** cho tới khi hoàn thành P0-01 đến P0-06 và bài nghiệm thu
lab ở P0-12, UI phải từ chối profile multi-cluster shared-MON thay vì âm
thầm dùng luồng single-cluster hiện tại.

## 3. Nguyên tắc không được vi phạm

1. **Isolation trước convenience:** khác FSID nhưng trùng config path, data
   path, service name hoặc port vẫn là conflict.
2. **Không cleanup mù:** không được chạy `rm -rf`, `cephadm rm-cluster`,
   `wipefs`, `ceph-volume zap` hoặc xoá container nếu chưa xác định đúng
   `deployment_id`/`cluster_id` và có approval phù hợp.
3. **FSID chỉ tạo một lần:** generate trước phase mutation, lưu bền vững,
   mọi retry/resume phải dùng lại FSID và artifact cũ.
4. **Unknown = block:** không xác định được process, port, Ceph cluster,
   OSD ownership hoặc runtime thì dừng, không đoán.
5. **Một action một target scope:** mọi Action, command preview, audit,
   progress, message queue và post-check phải chứa `cluster_id` và
   `deployment_id`.
6. **Không dùng global Settings cho deployment state:** `.env` chỉ giữ
   default/connection compatibility; trạng thái nhiều cụm phải nằm trong DB
   và được truyền explicit vào executor.
7. **Native và Docker là hai backend khác nhau:** không gọi profile Docker
   bằng đường đi `cephadm` hoặc bằng command native có cùng path mặc định.
8. **Không đánh dấu hoàn thành bằng unit test giả:** phải có acceptance evidence
   trên topology có hai cụm thật, shared MON, port/path khác nhau và failure
   injection.

## 4. Data model và deployment identity

### P0-01 — Deployment profile và cluster scope `[ ]`

Tạo model/migration hoặc mở rộng model hiện có để lưu tối thiểu:

```text
cluster_id
deployment_id
display_name
deployment_backend = native_systemd | docker_manual | cephadm
ceph_version
image_reference                  # bắt buộc với docker_manual
cluster_name                     # ceph hoặc cephB
fsid
config_dir
data_dir
mon_v1_port
mon_v2_port
mgr_port / rgw_ports             # nếu profile sử dụng
public_network
cluster_network
state = planned | preflighted | running | failed | ready | deleting
config_hash
monmap_hash
created_by / approved_by
created_at / updated_at
```

Thêm bảng scope theo node/daemon:

```text
deployment_node(deployment_id, host, hostname, roles, runtime, state)
deployment_osd(deployment_id, host, device, osd_id, osd_fsid, state)
deployment_artifact(deployment_id, artifact_type, path, sha256, created_at)
deployment_phase(deployment_id, phase_key, state, attempt, evidence, timestamps)
```

Acceptance criteria:

- Hai deployment A và B có thể cùng tồn tại trong DB mà không ghi đè nhau.
- FSID, config path, data path, ports và keyring path đều truy vấn được theo
  `deployment_id`.
- Không còn dùng một biến `settings.ceph_exec_mode` để quyết định backend cho
  mọi cụm.
- Migration có unique constraint phù hợp: FSID duy nhất; `(host, port)` không
  trùng trong cùng network namespace; `(host, config_dir)` không trùng nếu
  cùng backend.

### P0-02 — Scope propagation `[ ]`

Lan truyền `cluster_id`/`deployment_id` qua Dashboard route, cluster selector,
Action/approval, RabbitMQ message, Worker dispatch, executor context, Watcher
query/Incident/Metric/Log, backup/restore/upgrade/delete, Telegram/audit/event
timeline, cache key và lock key.

Không cho phép fallback im lặng về default cluster khi request đã chỉ rõ
`cluster_id` khác. Nếu scope thiếu ở action mutation, phải fail-closed.

## 5. Preflight và conflict detection

### P0-03 — Read-only inventory trước mutation `[ ]`

Trên từng host liên quan, chạy read-only inventory để phát hiện `cephadm ls`,
FSID/daemon, systemd `ceph-*`, Docker/Podman container, listener (`ss -ltnp`),
config/data directory, ownership, OSD/LVM/device state, hostname/FQDN, NIC/IP
route, package version và SSH capability.

Mỗi phát hiện phải lưu host, cluster/deployment identity nếu xác định được,
evidence command, `observed_at`, severity, conflict type và blocking reason.

### P0-04 — Conflict policy `[ ]`

Block deploy nếu config/data path, MON port, container name, systemd unit hoặc
OSD disk bị trùng; host có cluster nhưng profile chưa khai báo namespace;
native package version xung đột; hoặc không xác định được owner của state,
process và port. Preflight phải hiển thị cụm nào đang sở hữu tài nguyên và
cách xử lý, không trả lỗi chung chung “host busy”.

### P0-05 — Explicit destructive boundary `[ ]`

Xoá deployment, xoá config/data, zap/wipe OSD, remove unit/container và đổi
port/config phải là action ID riêng, có preview, approval và recheck. Cleanup
chỉ được nhận `deployment_id`; không dùng cleanup của deployment mới để xử lý
residue của deployment cũ.

## 6. Native/systemd backend

### P0-06 — Dựng cụm native có namespace riêng `[ ]`

Triển khai profile `native_systemd`: render `cluster_name`, FSID, config/data
path và MON address/port explicit; tạo monmap/keyring một lần rồi lưu hash;
tạo unit MON/MGR/OSD/RGW theo cluster name; cài đúng package version; kiểm tra
package conflict; không gọi `cephadm rm-cluster`; không xoá state ngoài scope.

Layout mặc định `/etc/ceph` và `/var/lib/ceph` phải được đánh dấu là tài
nguyên đang giữ. Cụm thứ hai chỉ được chạy khi preflight chứng minh namespace
và port tách biệt.

## 7. Docker manual backend

### P0-07 — Dựng cụm Docker thủ công `[ ]`

Triển khai backend riêng, không tái sử dụng command của `cephadm`:

- image reference được operator xác nhận;
- config/data path riêng như `/etc/cephB` và `/var/lib/cephB`;
- monmap/keyring đúng FSID B;
- MON/MGR `--net=host`, port và container name riêng;
- OSD `--privileged`, mount `/dev`, `/run/lvm`, config/data riêng;
- `ceph-volume lvm create` chỉ chạy trên disk đã preflight;
- lưu đúng `osd_id` và `osd_fsid`, không nhầm cluster FSID;
- sinh Compose/command manifest có checksum;
- không chạy `systemctl ceph-mon@...` hoặc ghi vào namespace cụm A.

Manifest phải sinh từ typed parameters, không nhận shell command tự do từ
người dùng hoặc AI.

## 8. Phase engine, idempotency và resume

### P0-08 — Checkpoint theo phase `[ ]`

Mỗi phase phải có state bền vững `pending -> running -> succeeded | failed |
blocked | needs_review`, kèm attempt, input/config fingerprint, command/
manifest hash, host result, artifact hash, FSID/OSD identity và timestamps.

Resume phải đọc checkpoint, inventory state thật, đối chiếu path/port/FSID/
artifact, skip phase đã đạt, chỉ retry phase an toàn, không generate FSID mới,
không tự wipe/reinitialize MON/OSD. State không khớp phải chuyển `BLOCKED`.

Acceptance bắt buộc: inject failure sau từng phase chính và resume mà không
tạo daemon/OSD duplicate.

## 9. Monitoring, backup và operational scope

### P0-09 — Multi-cluster runtime `[ ]`

Watcher chạy loop theo `cluster_id`; Dashboard forward cluster selection trên
tất cả trang Ceph; Worker chỉ xử lý Incident/Action đúng scope; backup/restore
lưu và kiểm tra FSID/cluster ID; upgrade/delete/convert/patch dùng đúng backend;
cache/lock/task key chứa deployment identity. Không dùng singleton global cho
`ceph_exec_mode`, keyring, container name hoặc config path.

### P0-10 — Delete/restore isolation `[ ]`

Phải chứng minh xoá B không dừng daemon A, restore B không ghi path A, backup
A/B không trộn keyring/FSID và action thiếu deployment ID bị từ chối. Unknown
ownership luôn block.

## 10. UI, approval và audit

### P0-11 — Deploy UI cho profile multi-cluster `[ ]`

Form phải có tên/ID cụm, backend, Ceph version hoặc image, FSID generate một
lần, config/data directory, MON/MGR/RGW ports, node/NIC/role, OSD disk,
preflight detail, command/manifest preview và approval/resume đúng deployment.

Audit append-only phải ghi actor, cluster/deployment ID, profile/version/image,
hosts, paths, ports, disks, preflight hash, approval, execution, post-check và
rollback/cleanup result.

## 11. Kiểm thử và nghiệm thu lab

### P0-12 — Test matrix `[ ]`

Unit/contract phải phủ config A/B, backend separation, port/path validation,
OSD identity, missing scope, resume và scoped cleanup. Integration phải phủ
inventory, conflict, failure injection, retry, stale approval và duplicate
prevention.

Live acceptance topology:

| Nhóm | Node |
|---|---|
| Shared MON | `10.20.1.150`, `10.20.1.249`, `10.20.1.253` |
| Data A | `10.20.1.34`, `10.20.1.64`, `10.20.1.54` |
| Data B | `10.20.1.83`, `10.20.1.78`, `10.20.1.1` |

Trình tự nghiệm thu: dựng A native; kiểm tra FSID/quorum/ports/OSD; dựng B
Docker trên shared MON + data B; kiểm tra `/etc/cephB`, `/var/lib/cephB`,
container, OSD ID/FSID; chạy workload độc lập; restart daemon B; xoá B; inject
lỗi và resume; reboot shared MON; xác nhận cả A và B quay lại đúng namespace.

Evidence bắt buộc gồm `ceph -s`, `ceph fsid`, `ceph mon dump`, `ceph osd tree`,
`ss -ltnp`, systemd/container state, checksum artifact, DB phase/audit rows và
before/after chứng minh delete B không ảnh hưởng A.

## 12. Thứ tự thực hiện

1. P0-01/P0-02: data model và scope propagation.
2. P0-03/P0-05: inventory, conflict guard và destructive boundary.
3. P0-08: checkpoint/resume/idempotency.
4. P0-06: native isolated backend.
5. P0-07: Docker manual backend.
6. P0-09/P0-10: monitoring, backup, restore và delete isolation.
7. P0-11: UI/approval/audit.
8. P0-12: regression, live lab acceptance và release gate.

## 13. Rollout và rollback

- Feature flag mặc định `false`.
- Luồng single-cluster hiện tại giữ nguyên nhưng phải block khi preflight thấy
  conflict shared-MON chưa được xác nhận.
- Canary trên lab disposable trước khi dùng topology shared MON.
- Migration backward-compatible, backup DB trước khi áp dụng.
- Rollback code không xoá Ceph state; chuyển action sang `BLOCKED/NEEDS_REVIEW`.
- Cleanup chỉ qua action riêng sau khi xác nhận đúng deployment ID.

## 14. Definition of Done

- [ ] Hai cụm A/B chạy đồng thời trên shared MON với layout và port riêng.
- [ ] Native và Docker manual dùng đúng backend, không dùng lẫn command.
- [ ] Không có path/port/FSID/unit/container/disk collision.
- [ ] Retry/resume không xoá hoặc tạo duplicate state.
- [ ] Monitoring, backup, restore, delete và audit đều scoped theo cluster.
- [ ] Failure injection sau từng phase đã resume thành công.
- [ ] Xoá B không ảnh hưởng A, có evidence trước/sau.
- [ ] Không còn cleanup toàn bộ `cephadm ls` hoặc `rm -rf` ngoài scope.
- [ ] Regression, live lab acceptance và post-deploy smoke đều đạt.
- [ ] Operator approval đã ký trước khi bật feature trên production.

## 15. Nhật ký thực hiện

| Ngày | Hạng mục | Trạng thái | Bằng chứng / việc tiếp theo |
|---|---|---|---|
| 2026-09-28 | Review bài lab shared MON + hai backend | `[ ] Bị chặn` | Baseline chỉ hỗ trợ single-cluster; thực hiện P0-01 đến P0-12 trước khi deploy thật |
