# Block Storage (Ceph RBD) — kế hoạch triển khai và nhật ký bàn giao

Tài liệu này là nguồn theo dõi chung cho phạm vi **Block Storage** của
`ceph-ai`: pool RBD, volume/image, snapshot, clone, backup, QoS, giám sát và
disaster recovery. Người thực hiện phải cập nhật trực tiếp file này khi hoàn
thành một phần việc để người kế tiếp biết chính xác trạng thái, bằng chứng kiểm
thử và điểm cần làm tiếp.

## Mục tiêu sản phẩm

- Cung cấp một nơi quản trị vòng đời volume Ceph RBD an toàn cho operator và
  administrator, từ tạo volume đến xóa hoặc khôi phục.
- Bảo đảm mọi thao tác đúng cluster, pool và tenant/project; không fallback âm
  thầm sang cluster mặc định.
- Cho phép quan sát dung lượng, hiệu năng, sức khỏe và quan hệ phụ thuộc của
  volume trước khi thay đổi.
- Mọi thao tác ghi đều có validation, preview, phân loại rủi ro, audit và trạng
  thái thực thi có thể kiểm chứng.
- Tích hợp được với OpenStack Cinder, Kubernetes CSI và các quy trình backup/DR
  mà không làm lộ credential.

## Ngoài phạm vi ban đầu

- Không thay thế trực tiếp control plane của OpenStack Cinder hoặc Kubernetes.
- Không cung cấp terminal/shell tự do từ Dashboard.
- Không tự động xóa volume, snapshot hoặc backup chỉ dựa trên đề xuất của AI.
- Không triển khai CephFS, RGW/S3 hoặc quản trị thiết bị OSD trong roadmap này.

## Quy ước bắt buộc

- `[ ]` Chưa làm.
- `[~]` Đang làm, mới hoàn thành một phần, hoặc đã có mã nhưng chưa đạt đủ tiêu
  chí nghiệm thu.
- `[x]` Đã merge mã, kiểm thử liên quan đạt và đã thêm một dòng vào **Nhật ký
  triển khai**.
- Không đánh dấu `[x]` chỉ vì giao diện đã hiện; phải kiểm tra quyền, cluster
  scope, lỗi backend, tính nhất quán dữ liệu và các test liên quan.
- Create/attach/resize/snapshot là thao tác ghi và phải có preview. Detach,
  rollback, flatten, restore và thay đổi replication/QoS là thao tác rủi ro cao
  cần xác nhận. Xóa volume/snapshot/backup phải yêu cầu nhập lại tên tài nguyên.
- Không chạy thao tác phá hủy khi chưa xác minh dependency, watcher/lock,
  snapshot/clone và trạng thái attachment.
- Không đưa Ceph keyring, SSH key, OpenStack credential, token hoặc mật khẩu vào
  HTML, log, audit payload, chat prompt hay response API.
- Tất cả route, job, metric và audit record phải có `cluster_id`. Không được
  fallback sang cluster mặc định; cluster/pool không hỗ trợ phải fail-closed.
- AI chỉ được chẩn đoán hoặc đề xuất action đóng. AI không được sinh shell tùy
  ý hoặc tuyên bố thao tác thành công nếu không có bằng chứng executor.

## Hiện trạng cần audit trước khi phát triển

- [~] **0.1 Volume inventory và thao tác RBD hiện có**
  - [x] Tự phát hiện các pool đã bật application `rbd`, có cluster selector và
    validate pool trước khi đưa input vào lệnh RBD.
  - [x] Có trang `/volumes` để tìm image theo tên từ dữ liệu iostat/lịch sử,
    xem IOPS, read/write latency, peak lịch sử và cờ bão hòa.
  - [x] Có trang `/trash`: tổng hợp used/logical size theo pool, liệt kê và phân
    trang RBD trash; xóa từng image tạo action RISKY chờ duyệt và có audit.
  - [~] Nút “Xóa tất cả” trash hiện admin-only và có confirm trình duyệt nhưng
    chạy `--force` ngay, bỏ qua Worker/approval. Luồng này không đạt nguyên tắc
    an toàn của roadmap và cần thay bằng job RISKY có xác nhận mạnh.
  - [ ] Chưa có inventory image thật từ `rbd ls/info/du/status`: không có size,
    features, watchers/locks, parent/child, snapshot count, owner/project hoặc
    attachment trên trang Volume.
  - [ ] Chưa có create/resize/attach/detach/rename/move/soft-delete/restore từ
    trash cho volume đang hoạt động.
  - [ ] Chưa có ánh xạ RBD image với Cinder volume hoặc Kubernetes PV/PVC.
- [~] **0.2 Backup/restore volume hiện có**
  - [x] Có backup RBD thủ công và theo lịch; lần đầu full `rbd export`, các lần
    sau incremental `rbd export-diff` từ snapshot, có full-refresh cadence.
  - [x] Có backend SSH và S3, hai target cho cluster mặc định, một target riêng
    cho cluster phụ; hỗ trợ S3 Object Lock/immutable khi được cấu hình.
  - [x] Upload và download đều kiểm tra size/SHA-256 qua backend; restore áp full
    rồi các diff thành công theo thứ tự và dừng khi checksum/import lỗi.
  - [x] Retention tách full/incremental và bảo vệ full đang là base của chain;
    có backup metadata cụm, anomaly detection, AI summary, digest và cảnh báo
    failed/overdue.
  - [x] Có queue/history/progress Dashboard, Run now, cấu hình tracked image,
    backup theo cluster và restore drill vào scratch image với đối chiếu checksum.
  - [~] Restore production hiện ghi đè đúng image nguồn sau approval; chưa có UI
    restore sang volume mới mặc định, preflight attachment/watcher/capacity hay
    post-restore application check.
  - [~] Restore drill và digest mới chạy cho cluster mặc định; cron/retention count
    phần lớn dùng policy YAML toàn cục, chưa quản trị đầy đủ trên Dashboard.
  - [ ] Chưa có RBD mirroring, replication lag, planned failover/failback,
    fencing hoặc báo cáo RPO/RTO liên site.
- [~] **0.3 Benchmark và metric hiện có**
  - [x] Watcher lấy `rbd perf image iostat`, lưu `VolumeMetric`, phát hiện bão hòa
    theo rolling baseline/streak và tự mở/resolve Incident.
  - [x] Có biểu đồ lịch sử, peak toàn thời gian, giới hạn cửa sổ truy vấn và phân
    tích hiệu năng bằng AI từ evidence đã thu thập.
  - [x] Benchmark pool dùng scratch image riêng, fio sweep iodepth để tìm knee,
    admin-only, action RISKY chờ duyệt và có tiến độ/kết quả bền vững.
  - [x] Benchmark từ trong VM chỉ đọc, có validation IP/user/key path/device,
    action RISKY chờ duyệt và hiện khóa ở cluster phụ.
  - [x] Collector đã tách rolling state/mẫu gần nhất theo cluster, tự phát hiện
    pool và query bằng kết nối riêng của cluster phụ; `VolumeMetric` và Incident
    bão hòa đều được gắn/lọc theo `cluster_id`. Đã có regression test cô lập hai
    cluster; còn cần xác minh live trên cụm phụ thật.
  - [ ] Chưa thu thập throughput, queue depth, percentile latency, used/provisioned
    capacity hoặc freshness/stale state; chưa có QoS editor và capacity forecast.
- [ ] **0.4 Baseline kỹ thuật**
  - [x] Đã lập inventory API/schema/command RBD và khoảng trống chức năng trong
    tài liệu này ngày 2026-08-17.
  - [~] Bộ test tập trung đạt `239 passed`; còn 1 lỗi setup tại test đầu tiên do
    race khi lifespan đồng bộ default cluster trên SQLite in-memory, không phải
    assertion của Block Storage. Cần sửa hoặc cô lập lỗi rồi chạy lại để đóng
    regression baseline.
  - [ ] Chưa audit live trên Ceph thật và chưa kiểm tra toàn bộ Alembic/repository
    regression, do đó mục baseline vẫn để `[ ]`.

**Hoàn thành khi:** có báo cáo hiện trạng, ma trận capability và regression test
cho các luồng đang tồn tại; không đánh dấu tính năng cũ là hoàn thành chỉ dựa
trên việc đã có code.

## Backlog cho các tính năng chưa có

Đây là thứ tự triển khai thực tế rút ra từ audit. Mỗi work package phải bàn giao
đủ service/adaptor, API, UI, RBAC, audit, test và tài liệu; không tách UI thành
một “tính năng hoàn thành” nếu executor hoặc guard chưa có.

| Thứ tự | Work package | Phạm vi bắt buộc | Phụ thuộc | Exit gate |
|---:|---|---|---|---|
| 1 | **BS-01 Inventory read-only `[~]`** | `rbd ls/info/du/status`, volume detail, size/used, feature, watcher/lock, snapshot và parent/child; search/filter/pagination theo cluster/pool. | Không | Test default/secondary/inactive cluster, timeout/partial error và không cross-cluster. |
| 2 | **BS-02 Volume CRUD nền tảng** | Create, expand-only resize, rename, move-to-trash và restore-from-trash; quota/capacity/dependency preflight, idempotency và reconciliation. | BS-01 | Mọi write qua Worker; retry không tạo trùng; delete bị chặn khi busy/dependent. |
| 3 | **BS-03 Attachment** | Inventory watcher/lock và mapping consumer; attach/detach qua control plane được hỗ trợ, exclusive/shared guard và force-detach approval. | BS-01, Cinder/CSI discovery | Không mutate trực tiếp volume do Cinder/CSI quản lý; có post-check consumer. |
| 4 | **BS-04 Snapshot/Clone** | Snapshot thủ công + lịch/retention, restore-as-new mặc định, rollback in-place có guard, clone, dependency graph và flatten. | BS-01, BS-02 | Không xóa protected/parent snapshot; scheduler dedup; rollback yêu cầu detached. |
| 5 | **BS-05 Restore an toàn** | Chọn full/diff recovery point, restore sang volume mới, preflight capacity/compatibility, verify size/read và promote có approval. | BS-02, backup hiện có | Không ghi đè production mặc định; evidence ghi rõ chain và post-check. |
| 6 | **BS-06 QoS/Capacity** | Throughput, queue depth, latency percentile, used/provisioned/freshness; QoS template/diff/rollback; dự báo 80/90/95%. | BS-01 và metric schema mới | Counter reset/stale metric được xử lý; QoS unsupported fail-closed. |
| 7 | **BS-07 OpenStack/CSI mapping** | Cinder volume/instance/project và CSI StorageClass/PV/PVC/Pod mapping, orphan report và source-of-truth routing. | BS-01, credential/capability | Tenant isolation; không có mutation bypass control plane. |
| 8 | **BS-08 DR/RBD Mirror** | Peer/bootstrap, relationship inventory, lag/RPO, planned failover/failback, fencing và DR drill liên site. | BS-05, runbook/failure-domain policy | Test split-brain/fencing; không auto-failover từ một tín hiệu. |
| 9 | **BS-09 AI/Automation** | Stale/waste insight, performance diagnosis và recommendation cho resize/QoS/flatten/retention bằng evidence; action id đóng. | BS-01–BS-08 ổn định | Prompt injection/redaction/post-check test; AI không có shell hoặc đường bypass policy. |

### Kế hoạch phát hành gần nhất

- **Sprint 1 — BS-01:** ưu tiên inventory read-only vì mọi CRUD, snapshot,
  attachment và safety preflight đều cần cùng một dependency model.
- **Sprint 2 — BS-02:** create + expand + trash/restore, đồng thời thay nút force
  purge hiện tại bằng async RISKY job qua Worker.
- **Sprint 3 — BS-04 + BS-05:** snapshot/clone và restore-as-new; giữ restore ghi
  đè hiện tại sau feature flag cho tới khi luồng mới được nghiệm thu.
- **Sprint 4 — BS-03 + BS-07:** tích hợp attachment cùng Cinder/CSI để tránh xây
  một đường map/unmap trực tiếp sai source of truth.
- **Sprint 5 — BS-06:** mở rộng metric/QoS/capacity sau khi inventory và identity
  volume đã ổn định.
- **Sprint 6 — BS-08, sau đó BS-09:** DR trước automation AI; không tự động hóa
  một quy trình failover chưa có runbook và fencing test.

## Thiết kế chung

### Phân quyền

| Vai trò | Quyền Block Storage |
|---|---|
| Viewer | Xem inventory, topology, metric, health và lịch sử job. |
| Operator | Tạo/attach/detach/resize/snapshot/clone theo policy được cấp. |
| Storage Admin | Quản lý pool, QoS, replication, backup/restore và thao tác phá hủy. |
| System Admin | Cấu hình backend, credential, tích hợp Cinder/CSI và DR liên cluster. |

Giai đoạn đầu có thể ánh xạ các vai trò này vào `is_admin` hiện tại, nhưng
route/service phải dùng capability riêng để có thể tách RBAC mà không đổi API.

### Kiến trúc và ranh giới thực thi

- Dashboard chỉ validate yêu cầu, tạo preview/job và hiển thị trạng thái;
  Worker là thành phần duy nhất được giữ credential và thực thi thao tác cluster.
- Dùng adaptor đóng cho `rbd`, Ceph API và OpenStack; mọi tham số cluster, pool,
  image, snapshot phải được validate, không nối shell từ input người dùng.
- Các thao tác dài chạy bất đồng bộ với state machine tối thiểu:
  `PENDING -> APPROVED -> RUNNING -> SUCCEEDED|FAILED|CANCELLED`.
- Job có idempotency key, actor, target, request id, command preview đã redaction,
  timestamps, progress, result/evidence và lỗi có cấu trúc.
- Capability detection theo phiên bản Ceph/backend quyết định tính năng nào
  được bật; UI không hiển thị thành công giả khi backend không hỗ trợ.

### Mô hình dữ liệu tối thiểu

- `BlockVolume`: cluster, pool, image id/name, size, provisioned/used bytes,
  format/features, owner/project, status và source backend.
- `VolumeAttachment`: volume, consumer type/id, host, device, mode, trạng thái và
  thời điểm attach/detach.
- `VolumeSnapshot`: volume, snapshot id/name, protected state, size, parent,
  retention class và trạng thái.
- `BlockStorageJob`: action type, actor, target, risk, approval, idempotency key,
  progress, result và error.
- `BlockStorageAuditEntry`: before/after hoặc preview diff đã redaction, evidence
  và correlation/request id.
- `BackupRecord`/`ReplicationRelationship`: source/destination, recovery point,
  checksum, RPO/RTO, retention và trạng thái kiểm chứng.

Inventory có thể đọc live và cache ngắn hạn; job/audit/backup/metric phải được
lưu bền vững. Không lưu secret dạng clear text.

## Các giai đoạn triển khai

### 1. Volume Inventory và Detail — ưu tiên P0 `[~] Đang triển khai BS-01`

- [~] **1.1 API danh sách volume scoped theo cluster/pool**
  - Trả name/id, pool, size, used/provisioned, format/features, trạng thái,
    attachment, snapshot count, owner/project và backend source.
  - Có search, filter, sort, cursor pagination, timeout và partial-error state.
  - Đã có API live `/api/volumes/{pool}/inventory` từ `rbd du`: image id,
    provisioned/used size, snapshot count, search, sort, page/page-size và
    `collected_at`; default/secondary cluster dùng đúng connection.
- [~] **1.2 Trang `/volumes`**
  - Bộ lọc cluster/pool/project/status/attachment, capacity summary và deep link.
  - Không dùng sample data khi backend trả rỗng hoặc lỗi.
  - Đã tích hợp card Inventory vào trang `/volumes`: filter, sort, bảng live,
    pagination, empty/error/freshness state và mở detail theo từng image.
- [~] **1.3 Volume detail read-only**
  - Metadata, feature, watchers/locks, attachment, parent/child, snapshot/clone,
    backup status, metric và audit gần nhất.
  - Đã có size/format/features, snapshot, parent, children/clone và watcher từ
    `rbd info/snap ls/status/children`; command phụ lỗi được degrade theo từng
    subsection và trả `partial_errors`, không làm mất metadata chính. Còn thiếu
    backup/audit summary.
- [~] **1.4 Pool overview read-only**
  - Pool type, replication/EC profile, logical/physical usage, image count,
    health, near-full state và capability RBD.
  - Đã có type, replica/min-size hoặc EC profile, PG/PGP, CRUSH rule, physical
    used/max-available/percent, object count và RBD capability từ pool detail +
    `ceph df detail`; còn thiếu health/near-full mapping trực tiếp.
- [~] **1.5 Kiểm thử và nghiệm thu**
  - Empty/error/pagination, input escaping, cluster isolation, stale cache,
    non-admin read-only và redaction.
  - Đã có 10 test cho parser, search/sort/pagination, secondary-cluster
    connection, input validation, read-only role, backend error, pool overview
    partial error và inactive-cluster fail-closed. Còn thiếu live Ceph.

**Hoàn thành khi:** operator xem được inventory thật và dependency của volume ở
cluster đang chọn, không có cross-cluster leak hoặc fallback sample.

### 2. Vòng đời Volume — ưu tiên P0

- [ ] **2.1 Create volume**
  - Chọn cluster/pool, tên, dung lượng, provisioning, feature và owner/project;
    validate quota/capacity/name trước khi tạo.
- [ ] **2.2 Resize volume**
  - Chỉ cho phép mở rộng trong giai đoạn đầu; hiển thị quota/capacity sau resize
    và cảnh báo filesystem/guest phải được mở rộng riêng.
- [ ] **2.3 Attach/detach**
  - Hỗ trợ consumer đã đăng ký; kiểm tra exclusive/shared mode, watcher/lock,
    multipath và trạng thái consumer trước thao tác.
- [ ] **2.4 Rename/move/copy theo capability**
  - Preview downtime, dung lượng và dependency; copy/move là async job có tiến độ.
- [ ] **2.5 Delete và recycle policy**
  - Mặc định soft-delete/trash với thời hạn khôi phục; hard-delete yêu cầu xác
    nhận nâng cao và chặn khi còn attachment/snapshot/clone/backup dependency.
- [ ] **2.6 Idempotency và reconciliation**
  - Retry không tạo volume hoặc attachment trùng; watcher đối soát job với trạng
    thái cluster sau timeout/restart.
- [ ] **2.7 Test**
  - Quota/capacity race, duplicate request, busy/locked image, partial failure,
    inactive cluster, RBAC, CSRF và destructive-action guard.

**Hoàn thành khi:** vòng đời volume vận hành end-to-end qua Worker, có audit và
không báo thành công trước khi đã xác minh trạng thái thực tế.

### 3. Snapshot, Clone và Image Template — ưu tiên P0

- [ ] **3.1 Snapshot thủ công** với tên, mô tả, retention class và consistency
  type (`crash-consistent` trước; `application-consistent` khi có agent/hook).
- [ ] **3.2 Snapshot policy** theo lịch, timezone, số bản giữ và capacity guard;
  scheduler có dedup, retry và missed-run handling.
- [ ] **3.3 Restore/rollback**
  - Khuyến nghị restore thành volume mới; rollback in-place là rủi ro cao, yêu
    cầu volume detached và xác nhận mất dữ liệu sau recovery point.
- [ ] **3.4 Clone và flatten**
  - Hiển thị parent/child graph, protected snapshot và ước tính thời gian/dung
    lượng trước flatten.
- [ ] **3.5 Template/image workflow**
  - Tạo volume từ snapshot/template và chuyển volume thành template read-only
    theo policy.
- [ ] **3.6 Test**
  - Scheduler/timezone, protected snapshot, dependency graph, rollback guard,
    concurrent clone/flatten, retention và audit.

**Hoàn thành khi:** snapshot/clone không thể làm đứt dependency ngoài ý muốn và
restore luôn tạo bằng chứng recovery point đã sử dụng.

### 4. Backup, Restore và Disaster Recovery — ưu tiên P0/P1 `[~] Đã có nền tảng`

- [~] **4.1 Chuẩn hóa backup policy**
  - Full/incremental, lịch, retention, destination, encryption và bandwidth
    limit; hiển thị RPO dự kiến và lần backup thành công gần nhất.
- [~] **4.2 Backup integrity**
  - Checksum/manifest, chain validation, trạng thái immutable nếu backend hỗ trợ
    và cảnh báo chain bị thiếu.
- [~] **4.3 Restore workflow**
  - Restore sang volume mới theo mặc định, chọn cluster/pool đích, validate
    capacity/compatibility và kiểm tra đọc sau restore.
- [ ] **4.4 Replication liên cluster/site**
  - Theo dõi lag, recovery point, health, planned failover, failback và fencing;
    không tự failover chỉ dựa trên một tín hiệu.
- [ ] **4.5 DR drill không ảnh hưởng production**
  - Tạo bản restore cô lập, chạy kiểm tra, ghi thời gian thực tế và dọn tài nguyên
    theo approval.
- [ ] **4.6 Test**
  - Broken incremental chain, destination full, retry/resume, checksum mismatch,
    split-brain/fencing, cross-cluster credentials và RPO/RTO reporting.

**Hoàn thành khi:** có thể chứng minh backup khôi phục được, không chỉ chứng minh
job copy đã chạy, và mỗi failover/failback có runbook cùng audit đầy đủ.

### 5. Hiệu năng, QoS và Capacity — ưu tiên P1 `[~] Đã có nền tảng metric/benchmark`

- [~] **5.1 Metric volume/pool**: IOPS read/write, throughput, latency percentile,
  queue depth, used/provisioned bytes và sampling freshness.
- [~] **5.2 Dashboard lịch sử**: range/zoom, top consumer, so sánh baseline và
  liên kết sự kiện deploy/resize/snapshot với biến động hiệu năng.
- [ ] **5.3 QoS policy**: IOPS/throughput limit và burst nếu backend hỗ trợ;
  preview tác động, template theo workload và rollback cấu hình.
- [ ] **5.4 Capacity forecasting**: dự báo mốc 80/90/95%, thin-provisioning risk,
  replica/EC overhead và failure-domain reserve.
- [~] **5.5 Benchmark an toàn**
  - Chỉ chạy trên volume test hoặc có xác nhận rõ; giới hạn tải/thời gian, không
    benchmark volume production đang attach mặc định.
- [~] **5.6 Alerting**: latency/IOPS anomaly, quota/capacity threshold, stuck job,
  stale metric và noisy-neighbor candidate; dedup/resolve lifecycle.
- [ ] **5.7 Test**: counter reset, missing/stale metric, percentile, threshold,
  timezone, QoS unsupported/rollback và benchmark guard.

**Hoàn thành khi:** dashboard phân biệt rõ dữ liệu mới/cũ, logical/physical
capacity và không áp QoS hoặc benchmark sai target.

### 6. Tính sẵn sàng, tính toàn vẹn và Pool Governance — ưu tiên P1

- [ ] **6.1 Health/dependency view** từ volume tới pool, PG, OSD và failure
  domain; chỉ ra degraded/undersized/inconsistent state có ảnh hưởng.
- [ ] **6.2 Replication/EC policy inventory** và kiểm tra độ bền so với failure
  domain; thay đổi policy chỉ qua migration plan riêng.
- [ ] **6.3 Watcher/lock và stale attachment remediation**
  - Chẩn đoán read-only trước; mọi unlock/force-detach là action rủi ro cao.
- [ ] **6.4 Data integrity workflow**
  - Thu thập checksum/scrub evidence khi có thể, liên kết incident và runbook;
  không tự repair khi chưa có policy và approval.
- [ ] **6.5 Pool lifecycle**
  - Create/configure/delete pool với preflight, PG/autoscaler recommendation,
    application tag `rbd`, quota và xác nhận tài nguyên phụ thuộc.
- [ ] **6.6 Test**: degraded cluster, insufficient replica/failure domains,
  stale lock, pool non-empty, unsupported EC/RBD feature và action guard.

**Hoàn thành khi:** hệ thống chặn thao tác làm giảm độ bền dưới policy và không
force-unlock/delete pool chỉ từ một tín hiệu quan sát.

### 7. Tích hợp OpenStack, Kubernetes và giao thức — ưu tiên P1/P2

- [ ] **7.1 OpenStack Cinder mapping**
  - Hiển thị project/volume/attachment/instance, đối soát orphan hai chiều và
    giữ OpenStack là source of truth cho tài nguyên do Cinder quản lý.
- [ ] **7.2 Kubernetes CSI mapping**
  - Ánh xạ StorageClass/PV/PVC/Pod tới RBD image; không mutate trực tiếp image do
    CSI quản lý nếu chưa đi qua control plane Kubernetes.
- [ ] **7.3 Boot-from-volume và image service**
  - Hiển thị dependency Glance/Cinder/VM, bảo vệ volume boot và snapshot đang dùng.
- [ ] **7.4 Multipath/NVMe-oF/iSCSI** nếu sản phẩm hỗ trợ gateway
  - Inventory gateway/path/session, health và controlled reconnect/failover.
- [ ] **7.5 API/CLI/IaC contract**
  - API versioning, OpenAPI, idempotency, webhook/job status và ví dụ Terraform
    hoặc SDK; không tạo một đường thực thi bỏ qua policy của Dashboard.
- [ ] **7.6 Test**: orphan mapping, deleted consumer, multi-attach, control-plane
  outage, tenant isolation và eventual consistency.

**Hoàn thành khi:** thao tác đối với volume được quản lý bởi control plane ngoài
luôn đi qua source of truth tương ứng và không làm lệch metadata.

### 8. AI Diagnosis và Automation an toàn — ưu tiên P2

- [ ] **8.1 AI inventory insight**: stale/unattached volume, snapshot quá hạn,
  clone chain sâu, backup trễ và capacity waste với evidence cụ thể.
- [ ] **8.2 Chẩn đoán hiệu năng**: tương quan volume/pool/OSD/host, phân biệt
  contention, capacity pressure và lỗi consumer; hiển thị confidence.
- [ ] **8.3 Recommendation có mô phỏng** cho resize, QoS, flatten, retention và
  placement; không tự thực thi từ nội dung chat.
- [ ] **8.4 Closed-loop action** chỉ cho action id allowlist, policy SAFE/RISKY,
  approval, timeout, post-check, rollback và audit.
- [ ] **8.5 Test**: prompt injection từ metadata, credential redaction, JSON xấu,
  hallucinated target/command, stale evidence và post-check failure.

**Hoàn thành khi:** mọi kết luận AI dẫn về evidence thật và mọi thay đổi vẫn đi
qua cùng RBAC/policy/executor như thao tác thủ công.

### 9. Hardening, vận hành và phát hành — gate bắt buộc cho từng pha

- [ ] **9.1 Security review**: RBAC/capability, CSRF, rate limit, input validation,
  secret redaction, encryption at rest/in transit và KMS/key rotation.
- [ ] **9.2 Audit viewer**: actor, cluster, pool/volume, preview/diff, approval,
  kết quả, request id và retention/export policy.
- [ ] **9.3 SLO và observability nội bộ**: API/job latency, queue depth, failure
  rate, stuck job, backend timeout, scheduler health và alert ownership.
- [ ] **9.4 Runbook**: create/resize/attach, busy image, snapshot dependency,
  backup chain, restore, failover/failback, capacity incident và credential loss.
- [ ] **9.5 Test matrix/release gate**
  - Default cluster, secondary active/inactive, Ceph unavailable/degraded, pool
    full, empty inventory, viewer/operator/admin và backend version supported/
    unsupported.
- [ ] **9.6 Upgrade/rollback và migration**
  - Một Alembic head, backward-compatible deployment, worker restart recovery,
    feature flag và rollback plan cho từng pha.
- [ ] **9.7 Tài liệu và UX**
  - Navigation, API docs, terminology, timezone/unit, accessibility, empty/error/
    loading state và cập nhật roadmap trước release.

## Lộ trình phát hành đề xuất

| Mốc | Phạm vi | Kết quả bàn giao |
|---|---|---|
| M0 — Baseline | Mục 0 | Audit hiện trạng, capability matrix và regression baseline. |
| M1 — Read-only | Mục 1 | Inventory/detail/pool overview đúng cluster, không mutation. |
| M2 — Core MVP | Mục 2 và 3 | Create/resize/attach/detach, snapshot/restore/clone an toàn. |
| M3 — Data protection | Mục 4 | Backup có integrity check, restore drill và DR visibility. |
| M4 — Operations | Mục 5 và 6 | Metric, QoS, capacity, health và pool governance. |
| M5 — Ecosystem | Mục 7 | Cinder/CSI mapping và API contract ổn định. |
| M6 — Intelligence | Mục 8 | AI diagnosis/recommendation và action đóng có post-check. |

Mục 9 là release gate xuyên suốt, không phải việc dồn lại ở cuối. Nên hoàn
thành một vertical slice nhỏ gồm service, API, UI, RBAC, audit và test trước khi
mở rộng sang slice tiếp theo.

## Chỉ số thành công

- 100% thao tác ghi có actor, cluster, target, preview và kết quả audit được.
- 0 trường hợp fallback/cross-cluster trong regression suite.
- 100% thao tác phá hủy bị chặn khi còn dependency hoặc thiếu xác nhận bắt buộc.
- Tỷ lệ job thành công và thời gian xử lý được đo theo từng action; job timeout
  được reconciliation thay vì để trạng thái không xác định.
- Backup được tính đạt SLO chỉ khi có integrity check và restore drill gần nhất.
- Inventory/metric luôn công bố `collected_at` và trạng thái stale.

## Cách cập nhật khi làm việc

Khi bắt đầu một mục, đổi checkbox cha thành `[~]`. Khi hoàn thành:

1. Đánh dấu từng mục con đạt tiêu chí thành `[x]`.
2. Đánh dấu mục cha `[x]` chỉ khi toàn bộ tiêu chí hoàn thành đã đạt.
3. Thêm một dòng vào bảng dưới: ngày, mã mục, thay đổi, lệnh/test thực tế,
   commit/PR và việc còn lại.
4. Nếu bị chặn, giữ `[~]`, ghi blocker và không đánh dấu hoàn thành giả.
5. Nếu thay đổi thiết kế/rủi ro, cập nhật quyết định trong roadmap trước khi
   tiếp tục code để người sau không phải suy đoán.

## Nhật ký triển khai

| Ngày | Mục | Trạng thái | Thay đổi / bằng chứng | Kiểm thử | Commit / việc tiếp theo |
|---|---:|---|---|---|---|
| 2026-08-17 | Kế hoạch | Hoàn thành | Tạo roadmap Block Storage, ranh giới an toàn, kiến trúc, các pha, tiêu chí nghiệm thu và quy trình bàn giao. Hiện trạng code chỉ được ghi là cần audit, chưa công nhận hoàn thành. | Chưa chạy — tài liệu kế hoạch | Bắt đầu từ mục 0; lập inventory API/schema/test hiện có trước khi sửa mã. |
| 2026-08-17 | 0.1–0.4 | Đang làm | Audit route, model, Watcher, Worker và policy hiện có. Xác nhận nền tảng volume performance, RBD trash, full/incremental backup, retention, checksum, restore và restore drill; ghi rõ các khoảng trống CRUD/snapshot/clone/QoS/DR và hai rủi ro multi-cluster metric + force purge. | Test tập trung: `239 passed, 1 error`; lỗi ở fixture lifespan/SQLite in-memory trước assertion đầu tiên. | Sửa/cô lập lỗi setup, chạy lại baseline; ưu tiên bỏ force purge trực tiếp và hoàn thiện inventory read-only. |
| 2026-08-17 | Fix metric multi-cluster | Hoàn thành code + test tập trung | Tách rolling state và last-poll sample theo cluster; thêm discovery RBD pool/query iostat bằng connection cluster phụ; persist `VolumeMetric.cluster_id`; scope lifecycle Incident bão hòa; nối collector vào observed-cluster loop. Thêm backlog BS-01–BS-09 cho phần còn thiếu. | `tests/test_volume_monitor.py`: 16 passed; 2 test integration Watcher volume path: 2 passed. Lượt suite rộng hơn được dừng sau `40 passed` vì test kế tiếp đi vào SSH Paramiko chậm; không có failure trước khi dừng. | Chưa kiểm chứng live trên Ceph phụ; tiếp theo BS-01 và thay force purge trực tiếp. |
| 2026-08-17 | BS-01 | Đang làm | Thêm adaptor `rbd du/info/snap ls/status/children`, inventory/detail API scoped theo cluster và card UI live có search/sort/pagination/freshness/capacity summary/dependency detail. | 6 test mới đạt; `py_compile` + `node --check` đạt. Một lượt trước gặp đúng race fixture SQLite lifespan đã ghi ở baseline và test đó đạt khi chạy lại. | Bổ sung pool overview sâu, partial-error detail, inactive-cluster test và kiểm chứng live trước khi đóng BS-01. |
| 2026-08-17 | BS-01 Pool Overview | Đang làm | Thêm pool durability/capacity overview từ `ceph osd pool ls detail` + `ceph df detail`; Volume Detail degrade riêng snapshot/watcher/children và công bố `partial_errors`; request chỉ rõ cluster inactive fail-closed, không rơi về default. | Nhóm BS-01: `10 passed`; `py_compile`, `node --check`, `git diff --check` đạt. | Còn health/near-full và live Ceph; sau đó đóng BS-01 và sang BS-02. |

## Ghi chú bàn giao

- Không ghi đè thay đổi chưa commit của người khác; luôn kiểm tra `git status`
  trước khi làm.
- Khi thêm migration, chạy `alembic heads`, nối đúng head hiện tại và bảo đảm
  chỉ còn một head trước khi bàn giao.
- Tách rõ lỗi hạ tầng/test ngoài phạm vi; không dùng chúng để che việc chưa có
  bằng chứng nghiệm thu.
- Mọi feature phụ thuộc phiên bản Ceph/OpenStack/CSI phải có capability detection
  và trạng thái `unsupported` rõ ràng.
- Ưu tiên API/service/executor và policy đúng trước; UI không được gọi lệnh cluster
  trực tiếp hoặc tạo một đường tắt bỏ qua Worker/audit.
