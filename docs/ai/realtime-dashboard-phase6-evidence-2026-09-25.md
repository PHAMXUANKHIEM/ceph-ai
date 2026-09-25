# Realtime Dashboard Phase 6 — live evidence (2026-09-25)

## Phạm vi và môi trường

- Server: `10.3.55.213`, cluster `CS-LAB` (`ac23b8ff-e235-414c-bed8-06894f3dedd3`).
- Dashboard chạy trong container read-only, giới hạn 0,5 CPU và 512 MiB RAM.
- Watcher là một process duy nhất và dùng `WATCHER_POLL_INTERVAL_SECONDS=5`.
- Benchmark dùng Chromium thật, session đã xác thực và mở trang Dashboard thật;
  không dùng `context.request` để giả lập tab.

## Kết quả 1/5/10 tab

Script: `scripts/realtime_browser_test.mjs`.

| Số tab | Browser navigation p95 | Health API p95 | Health request/tab | WebSocket/tab |
|---:|---:|---:|---:|---:|
| 1 | 80,87 ms | 87,62 ms | 1 | 1 |
| 5 | 250,83 ms | 66,01 ms | 1 | 1 |
| 10 | 561,75 ms | 189,26 ms | 1 | 1 |

Kết luận:

- Ở lượt baseline này, health API đạt mục tiêu dưới 200 ms ở cả ba mức tải.
- Cold HTML navigation đồng thời chưa đạt mục tiêu dưới 200 ms ở 5/10 tab.
  Các tab dùng chung một Chromium/host nên còn chịu hàng đợi kết nối phía client;
  không ghi nhận mục này là đạt.
- Mỗi tab chỉ mở một document, một health request ban đầu và một WebSocket.
- Không có page reload định kỳ.

Metrics trong Dashboard sau benchmark:

- `/api/dashboard/health`: 20 request, 0 lỗi, p95 bucket 50 ms.
- `/`: 20 request, 0 lỗi, p95 bucket 50 ms; 19/20 request nằm trong bucket 50 ms.
- Tải burst của health API xấp xỉ 1,08 request/giây trong lượt benchmark 18,52 giây.
- Polling fallback tạo 3 health request trong 12 giây, tương đương 0,25 request/giây.

### Regression sau khi bật snapshot checksum

Lượt Chromium thứ hai chạy trên đúng image checksum đang phục vụ production:

| Số tab | Browser navigation p95 | Health API end-to-end p95 |
|---:|---:|---:|
| 1 | 180,91 ms | 60,93 ms |
| 5 | 164,99 ms | 165,64 ms |
| 10 | 516,03 ms | 278,70 ms |

Mỗi tab vẫn chỉ có một document, một health request và một WebSocket; fallback
vẫn đạt 3 health request/12 giây, cross-cluster WebSocket vẫn đóng `1008`.
Server-side metrics sau lượt đo cho `/api/dashboard/health` là p95 bucket
100 ms, 0 lỗi; các request benchmark quan sát tại server nằm trong khoảng
3–62 ms. Chênh lệch ở burst 10 tab nằm trong browser/HTTP connection queue,
nhưng vì số end-to-end đã vượt 200 ms nên tiêu chí 10-tab được ghi là **chưa
ổn định**, không xem lượt baseline đầu tiên là bằng chứng đạt lâu dài.

## Snapshot, collector và SSH

- Counter Dashboard trước benchmark: 4 SSH connection, 4 command; đây là
  cache warmup lúc service khởi động.
- Counter sau benchmark vẫn là 4/4: browser request không tạo thêm SSH/Ceph command.
- Sau lượt regression checksum, counter vẫn là 4 connection/4 command và
  `cache_integrity_failures_total=0`.
- `snapshot_collector` trong process Dashboard giữ `success_total=0`,
  `failure_total=0`, `commands={}`.
- Container Watcher có đúng một process `python -m watcher.main`; mở 10 tab
  không tạo collector mới.
- Trang bootstrap trực tiếp từ snapshot persisted; legacy Incident/Audit feed
  bị tắt bằng `DASHBOARD_LEGACY_FEED_ENABLED=false`.

## Snapshot integrity, atomic lock và partial failure

- Mỗi cache record mới có checksum `sha256:<digest>` tính trên JSON canonical
  của `value`. Reader kiểm tra bằng constant-time comparison; record bị sửa
  hoặc hỏng checksum bị từ chối và tăng metric
  `cache_integrity_failures_total`.
- Record cũ chưa có checksum vẫn đọc được để rollout/rollback không làm mất
  snapshot hiện hữu.
- Test 8 process ghi đồng thời xác nhận file JSON cuối hợp lệ, checksum đúng và
  generation nhận đủ dãy 1–8. Việc ghi vẫn dùng temp file + `os.replace`, còn
  read-modify-write được bảo vệ bằng `flock` liên process.
- Snapshot production của `CS-LAB` sau deploy có checksum hợp lệ (generation
  34246 tại thời điểm kiểm tra).
- Khi health hoặc một inventory section refresh lỗi, payload tốt gần nhất,
  `collected_at` và generation được giữ nguyên; response bổ sung
  `last_attempted_at`, `last_error` và `partial_errors`. Nếu chưa từng có dữ
  liệu tốt, section được đánh dấu unavailable thay vì trả một danh sách rỗng
  gây hiểu nhầm.

## Freshness thực tế

Sau warmup, ba snapshot liên tiếp có `collected_at`:

- `04:30:39.524Z`
- `04:30:48.607Z` — cách 9,083 giây
- `04:30:57.027Z` — cách 8,420 giây

Khoảng publish tương ứng là 8,485 và 8,212 giây. Steady-state đạt yêu cầu cập
nhật health trong tối đa 10 giây ở lần đo này. Lần đầu ngay sau restart chậm
hơn do warmup; chưa tuyên bố startup p95 đạt SLA.

### Soak freshness và sửa scheduler

Script tái sử dụng: `scripts/realtime_snapshot_soak.mjs`. Script dùng browser
session thật, timeout từng request, kiểm tra cluster ID, document reload,
WebSocket, generation, stale/error, API p95 và collection/publication gap.

Soak 180 giây trước khi sửa scheduler có 89/89 request thành công nhưng phát
hiện collection gap p95/max **38,171 giây**. Nguyên nhân là các collector phụ
chậm chạy đồng bộ trong critical-health loop. Sau khi chuyển các scan device,
node, BlueStore, OSD latency, CRUSH, capacity và capability sang background,
gap giảm còn **21,410 giây**, nhưng vẫn có 10 sample mang refresh error do
auxiliary query tranh remote cephadm lock với health.

Fix cuối cùng giữ MON sticky đang phục vụ health làm tuyến dự phòng cuối cho
auxiliary cephadm query. Query phụ ưu tiên các MON còn lại và vẫn fallback đủ
danh sách khi lỗi; không mở thêm cephadm shell đồng thời trên cùng node.

Kết quả soak 180 giây sau fix:

- 89/89 request thành công; 0 stale, 0 refresh error.
- API p95 **88,3 ms**, max **154,4 ms**.
- 22 generation changes; collection gap p95 **8,473 giây**, max **8,563 giây**.
- Một document, một WebSocket, 35 frame; không page reload.

Lượt xác nhận 60 giây có publication gap p95/max **8,294 giây**, API p95
**69,6 ms**, 0 stale/error và không reload. Như vậy thời điểm snapshot mới
được publish tới Dashboard nằm dưới 10 giây trong hai lượt soak sau fix.
Sau toàn bộ benchmark/soak, Dashboard process ghi nhận 538 health request,
0 lỗi, server-side p95 bucket 50 ms; SSH counter vẫn giữ 4 connection/4
command từ warmup và Dashboard collector command map vẫn rỗng.

## WebSocket, fallback và isolation

- Khi ép đóng WebSocket: một document duy nhất, 3 health poll trong 12 giây;
  fallback hoạt động mà không reload trang.
- Cluster ID ngoài session scope bị WebSocket đóng với code `1008`.
- Deployment hiện chỉ có một active cluster, nên chưa thể chạy live A/B mà
  không tạo hoặc kích hoạt cluster thứ hai. Isolation A/B hiện được bao phủ bởi
  regression tests; live A/B vẫn là điều kiện chưa hoàn tất.
- RBAC/session guard không bị bỏ qua: WebSocket disabled flag, unauthenticated,
  revoked user và cross-cluster rejection đều nằm trong regression suite.

## Rollback và feature flags

Các flag production hiện tại:

```text
DASHBOARD_LEGACY_FEED_ENABLED=false
DASHBOARD_CLUSTER_EVENTS_ENABLED=true
DASHBOARD_API_RATE_LIMIT_RESERVATION_SIZE=10
WATCHER_POLL_INTERVAL_SECONDS=5
```

Rollback an toàn:

1. Đặt `DASHBOARD_CLUSTER_EVENTS_ENABLED=false` để WebSocket đóng bằng `1013`;
   browser tự chuyển sang polling snapshot có xác thực.
2. Đặt `DASHBOARD_LEGACY_FEED_ENABLED=true` nếu cần khôi phục feed HTML cũ.
3. Tăng `WATCHER_POLL_INTERVAL_SECONDS` về 10 hoặc 15 nếu critical collector
   làm tăng tải Ceph; các snapshot hợp lệ gần nhất vẫn được giữ.
4. Đặt `DASHBOARD_API_RATE_LIMIT_RESERVATION_SIZE=1` để quay lại một DB lock cho
   mỗi API request mà không đổi limit tổng.
5. Nếu scheduler/MON reservation gây regression, recreate riêng Watcher bằng
   tag `ceph-ai:realtime-pre-mon-reservation-20260925`; tag trước toàn bộ Phase
   6 là `ceph-ai:realtime-pre-aux-20260925`. Dashboard không cần recreate.

Snapshot store không bị xóa trong bất kỳ bước rollback nào.

## Verification

- `121 passed, 3 warnings` cho cluster snapshot, Watcher, WebSocket, rate limit,
  Dashboard health và navigation.
- `35 passed, 1 warning` cho checksum, legacy compatibility, atomic lock và
  snapshot contract ngay sau khi bổ sung integrity check.
- `48 passed, 3 warnings` cho lượt regression cuối của cache + snapshot +
  Dashboard health, gồm cả hai trường hợp giữ last-good data khi refresh lỗi.
- `84 passed, 3 warnings` cho Watcher + cache/snapshot/Dashboard health sau
  khi tách collector phụ khỏi critical-health loop.
- `170 passed, 1 warning` cho toàn bộ Ceph client + Watcher, gồm regression
  MON reservation và background dispatch production.
- Toàn bộ test Dashboard Chat + navigation đã qua sau thay đổi lazy loading.
- `node --check dashboard/static/chat_widget.js` đạt.
- Dashboard và Watcher đều healthy, restart count bằng 0 sau lần deploy cuối.
- Dashboard giữ image digest
  `3bb46dd29704df11af7b08d957950a0147b5daf94b54b44d593fcc912b841a13`;
  Watcher chạy image `d6dbf670c72ebf9131781d7c69d43d0a7f8631ee5cd3273a88d951b18129ed07`.
  Cả hai healthy/restart count 0; Watcher có đúng một process ứng dụng
  (`python -m watcher.main`).

## Phần chưa đóng

- Cold HTML navigation và health API end-to-end p95 dưới 200 ms chưa ổn định
  ở burst 10 tab; server-side health route vẫn đạt p95 bucket 100 ms.
- Live A/B isolation chưa chạy vì production chỉ có một active cluster.
- Full repository test suite chưa chạy; kết quả trên là regression suite đúng
  phạm vi Realtime Dashboard.
- Soak 3 phút đã đạt nhưng vẫn cần cửa sổ vận hành dài hơn (ít nhất vài giờ,
  sau đó 30 ngày cho SLO) để bao phủ nhiều chu kỳ Ceph chậm/lỗi.
