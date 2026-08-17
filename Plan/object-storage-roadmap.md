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

### 1. Bucket Overview — ưu tiên P0 `[~] Đang triển khai 2026-08-16`

- [~] **1.1 API danh sách bucket scoped theo cluster**
  - Trả bucket name, owner, creation time, số object, logical size, quota,
    versioning/object-lock state nếu RGW cung cấp.
  - Có search, sort, pagination, timeout và trạng thái empty/error riêng.
- [~] Đã có endpoint read-only `/api/object-storage/buckets` và
  `/api/object-storage/buckets/{bucket}`: tên bucket, owner, creation time,
  số object, logical size, quota, search theo tên, pagination 25 item và lỗi
  RGW; đã có sort và filter owner/quota/usage. Versioning/object-lock được
  chuẩn hóa khi `bucket stats` cung cấp và trả `unknown` minh bạch trên bản RGW
  không có field tương ứng.
- [~] **1.2 Trang `/object-storage/buckets`**
  - Table tổng quan, bộ lọc owner/quota/usage và deep link tới bucket detail.
  - Không hiển thị dữ liệu mẫu khi RGW trả danh sách rỗng.
- [~] Đã có table overview, tìm theo tên, pagination, empty/error state,
  filter owner/quota/usage, sort và deep link.
- [~] **1.3 Bucket detail read-only**
  - Metadata, quota, usage, policy/lifecycle/versioning summary, request/error
    trend và access-log lọc sẵn theo bucket.
- [~] Đã có bucket detail metadata/quota/placement/RGW node, trạng thái
  versioning/object-lock fail-soft, chỉ báo policy/lifecycle và link Access Log
  được lọc sẵn theo bucket; detail có request/error trend theo tối đa 12 giờ
  xuất hiện trong đoạn RGW log gần nhất.
- [~] **1.4 Kiểm thử và nghiệm thu**
  - Test empty/error/pagination, cluster scope, non-admin read-only capability
    và redaction.
- [~] Regression bao phủ empty/error, search/pagination/filter/sort, stats lỗi,
  detail/404, cluster phụ, operator read-only, activity fail-soft và giới hạn
  metadata fan-out. Suite liên quan chạy được trong `.venv`; còn giữ `[~]` đến
  khi thay đổi được commit/merge.

**Hoàn thành khi:** operator xem được danh sách và chi tiết bucket thật của
cluster đang chọn, không có fallback sample hoặc cross-cluster leak.

### 2. S3 Users và Access Keys — ưu tiên P0

- [ ] **2.1 S3 user inventory/detail**: uid, display name, bucket quota,
  capabilities, trạng thái và metadata an toàn.
- [ ] **2.2 Create/modify/disable S3 user** với preview, validation và audit.
- [ ] **2.3 Access-key lifecycle**: tạo, disable/enable, rotate và revoke;
  secret chỉ hiển thị một lần, không persistence/log.
- [ ] **2.4 Quota và capability editor** theo allowlist; giải thích tác động
  quyền trước khi submit.
- [ ] **2.5 Test**: RBAC, secret redaction, action confirmation, cluster scope,
  rollback/error from RGW và audit trail.

**Hoàn thành khi:** system admin quản lý user/key an toàn, còn operator không
thể gọi API ghi dù cố gửi request trực tiếp.

### 3. Bucket Operations và Data Governance — ưu tiên P1

- [ ] **3.1 Create bucket**: tên hợp lệ, owner, placement/storage class và
  quota tùy khả năng cluster; preview trước khi tạo.
- [ ] **3.2 Cập nhật quota, versioning và object-lock/retention** khi RGW hỗ
  trợ; capability detection phải ẩn/khóa tính năng không được hỗ trợ.
- [ ] **3.3 Lifecycle policy**: editor có schema validation, preview rule,
  dry-run số object bị ảnh hưởng và lịch sử thay đổi.
- [ ] **3.4 Bucket policy/ACL**: policy JSON validator, kiểm tra public access
  và diff trước/sau; mặc định deny public access.
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
| 2026-08-17 | 1.1–1.4 | Đang làm | Thêm filter owner/quota/usage, sort, pagination giữ filter, capability versioning/object-lock fail-soft, chỉ báo policy/lifecycle, deep link Access Log điền sẵn bucket, request/error trend, operator read-only, giới hạn metadata fan-out và redaction credential trong lỗi RGW. | `.venv/bin/pytest -q tests/test_rgw_access_log.py tests/test_dashboard_object_storage.py tests/test_dashboard_bucket_access_log.py`: 54 passed; `git diff --check` sạch. | Chưa commit; pha 1 đủ regression liên quan nhưng giữ `[~]` theo quy ước đến khi commit/merge. |

## Ghi chú bàn giao

- Không ghi đè thay đổi chưa commit của người khác; luôn kiểm tra `git status`
  trước khi làm.
- Khi thêm migration, kiểm tra Alembic head hiện tại và nối migration đúng head.
- Lỗi hạ tầng hoặc test ngoài phạm vi phải ghi rõ trong nhật ký, không che bằng
  việc đánh dấu `[x]`.
- Khi capability RGW phụ thuộc phiên bản Ceph, UI/API phải nêu rõ không hỗ trợ
  thay vì giả vờ thay đổi đã được áp dụng.
