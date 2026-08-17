# Object Storage (RGW/S3) — kế hoạch triển khai và nhật ký bàn giao

Tài liệu này là nguồn theo dõi chung cho phạm vi **Object Storage** của
`ceph-ai`: RGW, S3 user/access key, bucket, object và multi-site. Người thực
hiện phải cập nhật trực tiếp file này khi hoàn thành một phần việc để người kế
tiếp biết chính xác trạng thái, bằng chứng kiểm thử và điểm cần làm tiếp.

## Quy ước bắt buộc

- `[ ]` Chưa làm.
- `[~]` Đang làm, mới hoàn thành một phần, hoặc đã có mã nhưng chưa đạt đủ
  tiêu chí nghiệm thu.
- `[x]` Đã merge mã, kiểm thử liên quan đạt và đã thêm một dòng vào **Nhật ký
  triển khai** bên dưới.
- Không đánh dấu `[x]` chỉ vì giao diện đã hiện; phải kiểm tra phân quyền,
  cluster scope, trạng thái lỗi và ít nhất các test có liên quan.
- Mọi thao tác ghi vào RGW phải hiển thị preview, mức rủi ro, target cluster và
  yêu cầu xác nhận. Xóa bucket/object, xoay hoặc vô hiệu access key là thao tác
  phá hủy; không được tự chạy khi chưa có xác nhận của người dùng.
- Không đưa secret key, session token, mật khẩu hoặc SSH key vào HTML, log,
  audit payload, chat prompt hay response API. Secret access key chỉ được trả
  về một lần ngay sau khi tạo/rotate qua response được bảo vệ.
- Tất cả route/API phải resolve cluster đang chọn. Không được fallback âm thầm
  sang cluster mặc định; cluster không hỗ trợ phải fail-closed với thông báo rõ.

## Hiện trạng đã có

- [~] **0. Access-log và bucket stats nền tảng**
  - [~] Cấu hình RGW node/container theo cluster ở `/bucket-access-log` đã có
    mã; cần audit test/regression.
  - [~] Xem access log của RGW node, lọc theo tên bucket đã có mã; cần audit
    test/regression.
  - [~] Hiện owner, thời điểm tạo, số object, dung lượng và quota khi truy vấn
    một bucket đã có mã; cần audit test/regression.
  - [~] Đã audit regression test, quyền operator read-only và giới hạn fan-out
    metadata; còn giữ `[~]` đến khi thay đổi được commit/merge.

## Thiết kế chung

### Phân quyền

| Vai trò | Quyền Object Storage |
|---|---|
| Operator | Xem overview, bucket/object metadata, metric, access log và health. |
| Object Storage Admin | Thao tác bucket, policy, quota, lifecycle, object và export report. |
| System Admin | Quản lý RGW endpoint/multi-site; tạo, disable và rotate S3 user/access key. |

Tận dụng `is_admin` hiện tại ở giai đoạn đầu nếu hệ thống chưa có role chi tiết,
nhưng route/service phải có một lớp capability riêng để có thể tách role sau này.

### Nguồn dữ liệu và service layer

- Dùng `radosgw-admin` cho quản trị RGW và metadata; S3-compatible API cho
  object browser/upload/download khi cần.
- Tạo service/adaptor đóng (không ghép shell từ input người dùng), validate tên
  bucket/user/key trước khi chạy và log command đã được redaction.
- Dữ liệu lâu dài như audit, lịch sử metric, policy snapshot và job phải có
  `cluster_id`; secret không được lưu DB dạng clear text.
- Read-only endpoint dùng timeout, giới hạn kết quả, pagination và lỗi riêng
  theo section để một RGW node lỗi không làm hỏng toàn trang.

## Các giai đoạn triển khai

### 1. Bucket Overview — ưu tiên P0 `[x] Hoàn thành 2026-08-17`

- [x] **1.1 API danh sách bucket scoped theo cluster**
  - Trả bucket name, owner, creation time, số object, logical size, quota,
    versioning/object-lock state nếu RGW cung cấp.
  - Có search, sort, pagination, timeout và trạng thái empty/error riêng.
- [x] Đã có endpoint read-only `/api/object-storage/buckets` và
  `/api/object-storage/buckets/{bucket}`: tên bucket, owner, creation time,
  số object, logical size, quota, search theo tên, pagination 25 item và lỗi
  RGW; đã có sort và filter owner/quota/usage. Versioning/object-lock được
  chuẩn hóa khi `bucket stats` cung cấp và trả `unknown` minh bạch trên bản RGW
  không có field tương ứng.
- [x] **1.2 Trang `/object-storage/buckets`**
  - Table tổng quan, bộ lọc owner/quota/usage và deep link tới bucket detail.
  - Không hiển thị dữ liệu mẫu khi RGW trả danh sách rỗng.
- [x] Đã có table overview, tìm theo tên, pagination, empty/error state,
  filter owner/quota/usage, sort và deep link.
- [x] **1.3 Bucket detail read-only**
  - Metadata, quota, usage, policy/lifecycle/versioning summary, request/error
    trend và access-log lọc sẵn theo bucket.
- [x] Đã có bucket detail metadata/quota/placement/RGW node, trạng thái
  versioning/object-lock fail-soft, chỉ báo policy/lifecycle và link Access Log
  được lọc sẵn theo bucket; detail có request/error trend theo tối đa 12 giờ
  xuất hiện trong đoạn RGW log gần nhất.
- [x] **1.4 Kiểm thử và nghiệm thu**
  - Test empty/error/pagination, cluster scope, non-admin read-only capability
    và redaction.
- [x] Regression bao phủ empty/error, search/pagination/filter/sort, stats lỗi,
  detail/404, cluster phụ, operator read-only, activity fail-soft và giới hạn
  metadata fan-out. Suite liên quan chạy được trong `.venv`; còn giữ `[~]` đến
  khi thay đổi được commit/merge.

**Hoàn thành khi:** operator xem được danh sách và chi tiết bucket thật của
cluster đang chọn, không có fallback sample hoặc cross-cluster leak.

### 2. S3 Users và Access Keys — ưu tiên P0 `[~] Đang triển khai 2026-08-17`

- [x] **2.1 S3 user inventory/detail**: uid, display name, bucket quota,
  capabilities, trạng thái và metadata an toàn.
- [x] **2.2 Create/modify/disable S3 user**: đã có API preview/execute, admin
  RBAC, form UI hai bước, xác nhận UID, validation, command allowlist,
  audit persistence/viewer; access key được quản lý qua flow riêng ở mục 2.3.
- [x] **2.3 Access-key lifecycle**: đã có preview/execute tạo và revoke, admin
  RBAC, confirmation, audit redaction và secret chỉ trả một lần, không
  persistence/log. RGW không có trạng thái disable key độc lập trong adaptor
  hiện tại; rotate dùng flow an toàn tạo key mới rồi revoke key cũ riêng.
- [x] **2.4 Quota và capability editor**: đã có API preview/execute, quota
  scope allowlist, capability type/permission allowlist, giải thích tác động,
  admin RBAC, confirmation, audit và form UI hai bước.
- [~] **2.5 Test**: RBAC, secret redaction, action confirmation, cluster scope,
  rollback/error from RGW và audit trail.

**Hoàn thành khi:** system admin quản lý user/key an toàn, còn operator không
thể gọi API ghi dù cố gửi request trực tiếp.

### 3. Bucket Operations và Data Governance — ưu tiên P1 `[~] Đang triển khai 2026-08-17`

- [x] **3.1 Create bucket**: tên hợp lệ, owner, placement/storage class và
  quota tùy khả năng cluster; preview trước khi tạo.
  - Đã thêm live capability gate đọc `ceph versions`, map đúng release docs
    và xác nhận create bucket phải dùng S3 API (không dựng lệnh
    `radosgw-admin bucket create` không tồn tại). Mixed/unknown version
    fail-closed. Flow preview/execute validate tên DNS, owner và endpoint,
    hỗ trợ placement theo `zonegroup_api_name:placement_target`, tạo access
    key tạm nội bộ cho owner, gọi S3 `CreateBucket`, rồi luôn thu hồi key;
    secret không vào response/audit. Storage class được khai báo rõ là thuộc
    lúc ghi object, không phải tham số bucket-create. Quota bucket được giữ ở
    mục 3.2 để có preview/rollback độc lập, tránh báo create thất bại khi
    bucket thực tế đã tồn tại.
- [x] **3.2 Cập nhật quota, versioning và object-lock/retention** khi RGW hỗ
  trợ; capability detection phải ẩn/khóa tính năng không được hỗ trợ.
  - Capability được lấy sau live `ceph versions` và liên kết đúng tài liệu
    release. Bucket quota dùng allowlist `radosgw-admin quota
    set|enable|disable --quota-scope=bucket --bucket=...`; versioning và
    default retention dùng S3 API với credential tạm của owner. Object Lock
    chỉ có thể bật lúc CreateBucket; retention từ chối sớm nếu metadata xác
    nhận bucket không bật Object Lock, còn trạng thái metadata `unknown` được
    hiển thị trung thực và để RGW quyết định thay vì suy đoán.
- [x] **3.3 Lifecycle policy**: editor có schema validation, preview rule,
  dry-run số object bị ảnh hưởng và lịch sử thay đổi.
  - Editor hỗ trợ tối đa 100 rule với ID duy nhất, prefix, status,
    expiration, noncurrent expiration, abort multipart và transition qua
    allowlist storage class tương thích boto/RGW. Preview đọc policy hiện tại,
    quét có phân trang tối đa 1.000 current object để ước lượng theo prefix và
    tuổi, báo rõ giới hạn cũng như phần multipart/noncurrent không thể suy ra
    từ `ListObjectsV2`. Put/delete đều admin-only, xác nhận tên bucket, audit
    persistent và dùng credential owner tạm được thu hồi sau request.
- [x] **3.4 Bucket policy/ACL**: policy JSON validator, kiểm tra public access
  và diff trước/sau; mặc định deny public access.
  - Editor validate Version/Statement/Effect/Principal/Action/Resource, chỉ
    cho resource của bucket hiện tại, chặn interpolation RGW không hỗ trợ và
    action ngoài allowlist Reef. Action còn được gate theo live major version:
    các nhóm Notification/Replication/Public Access Block/Bucket Tagging và
    Object Lock yêu cầu Octopus 15+, Bucket Encryption yêu cầu Reef 18+.
    Preview đọc policy/ACL hiện tại và trả diff; public Principal hoặc canned
    ACL rộng yêu cầu xác nhận `PUBLIC:<bucket>`. Put/delete policy và set ACL
    đều admin-only, audit persistent, owner-matched và credential tạm.
- [ ] **3.5 Delete bucket**: hiển thị object count/size, yêu cầu nhập lại tên
  bucket; xóa non-empty phải là flow riêng có xác nhận rủi ro cao.
- [ ] **3.6 Test**: policy validation, confirmation, action audit, unsupported
  capability và không có lệnh free-form.

**Hoàn thành khi:** mọi thay đổi có preview/audit, và thao tác xóa không thể xảy
ra chỉ bằng một click hoặc qua request thiếu capability.

### 4. Object Browser — ưu tiên P1

- [ ] **4.1 Duyệt object theo prefix** có pagination, sort, search và hiển thị
  size/content type/last modified/version.
- [ ] **4.2 Xem object metadata, tags, version và retention state**.
- [ ] **4.3 Upload/download qua pre-signed URL ngắn hạn**; giới hạn size/type,
  không proxy file lớn qua Dashboard.
- [ ] **4.4 Delete/restore version** với confirmation, audit và policy check.
- [ ] **4.5 Test**: paging/token, prefix escaping, authorization, file-size
  limit, expired URL và destructive-action guard.

**Hoàn thành khi:** object browser vận hành được trên bucket lớn mà không tải
toàn bộ object list vào memory hoặc làm lộ credential S3.

### 5. Observability, Alerting và Reporting — ưu tiên P1

- [ ] **5.1 Thu thập metric RGW/bucket/user**: requests, bytes in/out, 4xx/5xx,
  latency và quota usage; chọn Prometheus nếu sẵn có, fallback collector
  read-only có retention.
- [ ] **5.2 Dashboard trend/top consumers** theo bucket, user, endpoint và
  thời gian; filter phải giữ cluster scope.
- [ ] **5.3 Alert rules**: quota 80/90/95%, 5xx spike, access denied spike,
  hot bucket và access bất thường; deduplicate/resolve lifecycle qua Telegram.
- [ ] **5.4 Export report CSV/JSON** có giới hạn quyền và không chứa secret.
- [ ] **5.5 Test**: threshold, dedup, timezone, metric missing/stale và
  cross-cluster alert isolation.

**Hoàn thành khi:** cảnh báo chỉ gửi khi transition thật, có link về đúng bucket
và cluster, không spam khi metric nguồn gián đoạn.

### 6. RGW và Multi-site Health — ưu tiên P2

- [ ] **6.1 RGW service overview**: daemon up/down, endpoint reachability,
  frontend config/version và capacity dependency.
- [ ] **6.2 Multi-site topology**: realm, zonegroup, zone, master state và
  replication/sync lag nếu triển khai.
- [ ] **6.3 Read-only diagnostic**: command output chuẩn hóa, evidence snapshot,
  hướng dẫn xử lý; không tự sửa topology.
- [ ] **6.4 Controlled remediation (chỉ sau khi có policy riêng)**: action đóng,
  preview, approval và audit; không mở shell tùy ý.
- [ ] **6.5 Test**: topology missing, failover state, timeout và action guard.

**Hoàn thành khi:** trạng thái RGW/multi-site có thể quan sát được mà không làm
thay đổi topology hay replication ngoài ý muốn.

### 7. Hardening, Documentation và phát hành — ưu tiên P0 cho từng pha

- [ ] **7.1 RBAC/capability review** cho mọi API mới; thêm CSRF và rate limit
  nếu route ghi chưa được framework bao phủ.
- [ ] **7.2 Audit viewer**: actor, cluster, target, preview/diff, result,
  request id; có redaction và retention policy.
- [ ] **7.3 Runbook**: cấu hình RGW, credential handling, khôi phục key,
  lifecycle/policy rollback, incident response và giới hạn tương thích Ceph.
- [ ] **7.4 Test matrix/release gate**: default cluster, secondary active,
  secondary inactive, RGW unavailable, empty cluster, operator và admin.
- [ ] **7.5 Cập nhật navigation, API docs và tài liệu này** trước khi đóng từng
  pha.

## Thứ tự thực hiện được khuyến nghị

1. Hoàn tất audit mục 0, sau đó làm 1 (read-only Bucket Overview).
2. Làm 2 (S3 Users/Keys) với audit và capability trước UI thao tác.
3. Làm 3 (governance) rồi 4 (object browser).
4. Làm 5 song song sau khi schema metric ổn định.
5. Chỉ làm 6 sau khi phần read/write cơ bản có test hồi quy đầy đủ.

## Cách cập nhật khi làm việc

Khi bắt đầu một mục, đổi checkbox cha thành `[~]`. Khi hoàn thành:

1. Đánh dấu từng mục con đạt tiêu chí thành `[x]`.
2. Đánh dấu mục cha `[x]` chỉ khi toàn bộ tiêu chí hoàn thành đã đạt.
3. Thêm một dòng vào bảng dưới: ngày, mã mục, thay đổi, lệnh/test thực tế,
   commit/PR và việc còn lại.
4. Nếu bị chặn, giữ `[~]`, ghi blocker và không đánh dấu hoàn thành giả.

## Nhật ký triển khai

| Ngày | Mục | Trạng thái | Thay đổi / bằng chứng | Kiểm thử | Commit / việc tiếp theo |
|---|---:|---|---|---|---|
| 2026-08-16 | Kế hoạch | Hoàn thành | Tạo roadmap, tiêu chí an toàn, thứ tự triển khai và quy trình bàn giao. Baseline access-log đã được rà mã nguồn nhưng chưa audit test trong roadmap này. | Chưa chạy — tài liệu kế hoạch | Bắt đầu từ 0.4 và 1.1 |
| 2026-08-16 | 1.1–1.4 | Đang làm | Thêm Object Storage Bucket Overview/Detail read-only, API scoped theo cluster, RGW bucket-list/stats adaptor an toàn, search tên, pagination, empty/error state, per-row stats degradation và điều hướng. Thêm regression suite. | `python3 -m py_compile` route/adaptor/test và `node --check dashboard/static/app.js` đạt; `git diff --check` sạch. `pytest` chưa chạy: Python thiếu pytest/FastAPI và ensurepip nên không tạo được venv tạm. | Chưa commit. Cài `python3-venv` hoặc cung cấp môi trường test rồi chạy `tests/test_dashboard_object_storage.py` + hồi quy RGW; hoàn tất filter/sort và detail enrichment. |
| 2026-08-17 | 1.1–1.4 | Hoàn thành | Thêm filter owner/quota/usage, sort, pagination giữ filter, capability versioning/object-lock fail-soft, chỉ báo policy/lifecycle, deep link Access Log điền sẵn bucket, request/error trend, operator read-only, giới hạn metadata fan-out và redaction credential trong lỗi RGW. | `.venv/bin/pytest -q tests/test_rgw_access_log.py tests/test_dashboard_object_storage.py tests/test_dashboard_bucket_access_log.py`: 54 passed; `git diff --check` sạch. | Commit `699560f`, đã push `main`. |
| 2026-08-17 | 2.1 | Đang làm | Thêm S3 user inventory/detail read-only, search/pagination, cluster scope và allowlist response loại bỏ toàn bộ key material. | `.venv/bin/pytest -q tests/test_dashboard_object_storage_users.py tests/test_dashboard_object_storage.py tests/test_rgw_access_log.py tests/test_dashboard_bucket_access_log.py`: 60 passed; `node --check dashboard/static/app.js` và `git diff --check` sạch. | Chưa commit; tiếp theo thiết kế preview/audit cho 2.2. |
| 2026-08-17 | 2.2 | Đang làm | Thêm create/modify/suspend/enable qua command allowlist; API và UI bắt buộc preview, admin RBAC và xác nhận lại UID. Structured audit log không chứa credential. | Suite Object Storage liên quan: 65 passed; `node --check dashboard/static/object_storage_users.js`, `node --check dashboard/static/app.js` và `git diff --check` sạch. | Chưa commit; còn audit persistence/viewer trước khi đóng 2.2. |
| 2026-08-17 | 2.2 audit | Đang làm | Thêm bảng audit riêng scoped theo cluster, fail-closed nếu không tạo được audit record, lưu success/failure/request ID và viewer/API admin-only. | Object Storage + migration regression: 74 passed; Alembic có một head `c8d41f7a2e90`; JS syntax và `git diff --check` sạch. | Chưa commit; sau khi merge có thể đóng 2.2 và chuyển sang 2.3. |
| 2026-08-17 | 2.3 | Đang làm | Thêm tạo/revoke S3 access key với preview, admin RBAC, confirmation, audit redaction và UI one-time secret. Rotate là flow hai bước create rồi revoke để tránh mất quyền truy cập ngoài ý muốn. | User/key + adaptor + migration regression: 46 passed; JS syntax và `git diff --check` sạch. | Chưa commit; cần kiểm chứng capability trên RGW thật và hoàn thiện test cluster phụ trước khi đóng 2.3. |
| 2026-08-17 | 2.4 | Đang làm | Thêm API và UI hai bước cho quota set/enable/disable, capability add/remove với allowlist, effect preview, admin RBAC, confirmation, audit và cluster scope. | User/settings + adaptor regression: 44 passed; JS syntax và `git diff --check` sạch. | Chưa commit; sau khi merge chuyển sang regression gate 2.5. |
| 2026-08-17 | 2.5 | Đang làm | Bổ sung direct-write RBAC cho toàn bộ preview/execute API, key action cluster phụ và redaction credential ở cả HTTP error/audit failure. | S3 user/key/settings + adaptor + migration: 56 passed; JS syntax và `git diff --check` sạch. | Chưa commit; sau khi merge có thể đóng pha 2 và chuyển sang 3.1 Create Bucket. |
| 2026-08-17 | 3.1 | Hoàn thành | Thêm create bucket hai bước qua S3 API, live Ceph release gate, DNS/endpoint/owner validation, optional Reef placement constraint, admin RBAC, persistent audit và credential tạm được thu hồi sau request. Không giả lập storage class ở bucket-create; quota chuyển sang 3.2. | Object Storage regression: 89 passed; JS syntax và `git diff --check` sạch. | Chưa commit; tiếp theo 3.2 quota/versioning/object-lock capability editor. |
| 2026-08-17 | 3.2 | Hoàn thành | Thêm editor quota/versioning/default retention hai bước; Object Lock tại CreateBucket; live release gate, owner check, temporary-key cleanup, admin RBAC và audit. Tuân theo giới hạn Reef: không bật Object Lock muộn. | Object Storage regression: 92 passed trước test retention cuối; Python/JS syntax và `git diff --check` sạch. | Chưa commit; tiếp theo 3.3 Lifecycle policy. |
| 2026-08-17 | 3.3 | Hoàn thành | Thêm lifecycle JSON editor schema đóng, preview policy trước/sau, dry-run current object giới hạn 1.000, put/delete qua S3, owner check, audit và temporary-key cleanup. Transition chỉ nhận storage class allowlist tương thích SDK. | Test Lifecycle riêng: 2 passed; Object Storage route/adaptor: 54 passed; chạy lại full regression trước commit. | Chưa commit; tiếp theo 3.4 Bucket policy/ACL. |
| 2026-08-17 | Capability audit | Hoàn thành | Bổ sung matrix theo live major version và lý do tối thiểu: placement từ Jewel 10, lifecycle/versioning baseline xác minh ở Mimic 13, lifecycle transition/storage class từ Nautilus 14, Object Lock từ Octopus 15. UI hiển thị phiên bản thực tế, disable option không hỗ trợ; preview/execute vẫn chặn server-side để chống bypass. | Thêm regression Mimic cho API capability và server-side reject Object Lock/Transition; chạy lại full suite trước commit. | Chưa commit; duy trì fail-closed cho mixed/unknown release. |
| 2026-08-17 | 3.4 | Hoàn thành | Thêm Bucket Policy/ACL editor, Reef action allowlist, per-action version gate, same-bucket resource validation, public detection, diff, strong confirmation, audit và temporary-key cleanup. | Route regression 27 passed; chạy full Object Storage suite trước commit. | Chưa commit; tiếp theo 3.5 Delete Bucket. |

## Ghi chú bàn giao

- Không ghi đè thay đổi chưa commit của người khác; luôn kiểm tra `git status`
  trước khi làm.
- Khi thêm migration, kiểm tra Alembic head hiện tại và nối migration đúng head.
- Lỗi hạ tầng hoặc test ngoài phạm vi phải ghi rõ trong nhật ký, không che bằng
  việc đánh dấu `[x]`.
- Khi capability RGW phụ thuộc phiên bản Ceph, UI/API phải nêu rõ không hỗ trợ
  thay vì giả vờ thay đổi đã được áp dụng.
