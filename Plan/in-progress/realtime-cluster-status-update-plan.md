# Kế hoạch nâng cấp cập nhật trạng thái cụm theo thời gian thực

**Phạm vi:** Ceph AIOps Dashboard trên `10.3.55.213`

**Repo:** `/root/ceph-ai`

**Ngày lập kế hoạch:** 2026-09-14

**Trạng thái:** Đang triển khai theo vertical slice có test; RT-00 đến RT-06
đã có foundation, RT-07 còn một số trang cần chuẩn hóa UI, RT-08/RT-09 đang
được thực hiện theo inventory và source mapping.

---

## 1. Mục tiêu

Làm cho Dashboard cập nhật trạng thái cluster, health, Pools, PGs, CRUSH Map,
Nodes và các trang có dữ liệu cluster theo cách mượt như croit:

- Lần mở trang đầu tiên trả giao diện ngay, không chờ SSH/Ceph CLI hoàn tất.
- Dữ liệu mới được thu thập nền theo chu kỳ riêng, không để từng trang tự chạy
  lệnh Ceph.
- Khi dữ liệu đổi, giao diện cập nhật phần cần đổi, không reload toàn bộ HTML.
- Khi thao tác Create/Edit/Delete/CRUSH/Upgrade hoàn tất, trang nhận được event
  hoặc refresh ngay sau khi có post-check thành công.
- Luôn hiển thị thời điểm dữ liệu Ceph thực sự được thu thập, tuổi dữ liệu và
  trạng thái đang cập nhật/lỗi; không hiển thị thời gian giả chỉ dựa vào giờ
  render HTML.
- Khi Ceph hoặc một MON chậm/mất kết nối, giữ snapshot hợp lệ gần nhất và báo
  stale rõ ràng thay vì làm trắng bảng.
- Không làm tăng đột biến số lệnh SSH/`cephadm shell` khi mở nhiều tab hoặc có
  nhiều người dùng.

### Tiêu chí thành công cấp sản phẩm

| Tình huống | Mục tiêu |
|---|---:|
| Mở Dashboard khi đã có snapshot | HTML/API bắt đầu render dưới 200 ms ở p95 |
| Thay đổi health trong Ceph | UI phản ánh dưới 10 giây ở p95 |
| Thao tác đã được Worker xác nhận thành công | UI nhận event/refresh dưới 2 giây |
| Trang Pools/PGs/CRUSH sau khi mở | Không phát sinh lệnh Ceph riêng cho mỗi browser request |
| Ceph/MON tạm thời lỗi | Không mất snapshot hợp lệ; có `stale=true` và lỗi có cấu trúc |
| 10 tab cùng mở | Chỉ một collector refresh cho mỗi cluster/nhóm dữ liệu |

Các con số trên cần đo ở cluster thật trước khi chốt; không đánh dấu đạt chỉ
dựa trên cảm nhận khi bấm UI.

---

## 2. Kết luận từ việc đối chiếu với croit

Mã nguồn UI chính của croit không nằm trong các repo công khai của organization
GitHub. Repo công khai `mcp-croit-ceph` là adapter gọi REST API của croit, không
phải source frontend quản lý cluster. Vì vậy không nên khẳng định croit đang
dùng chính xác WebSocket/SSE nào cho toàn bộ UI nếu chưa có bằng chứng từ deployment.

Tuy nhiên, tài liệu công khai cho thấy mô hình của họ khác hiện trạng AIOps:

- Croit cung cấp REST API và OpenAPI tại deployment; client không cần tự mở SSH
  rồi chạy nhiều lệnh Ceph cho từng màn hình.
- Croit có collector/management service và kho metrics riêng. Từ v2503 họ dùng
  VictoriaMetrics/VictoriaLogs; blog của họ mô tả mục tiêu là giảm overhead và
  tăng khả năng mở rộng khi truy vấn metrics/logs.
- Adapter công khai của croit mô tả cache, field selection, filtering, default
  pagination và HTTP client session lâu dài.
- Croit có hook backend như `OnHealthDegrade`, `OnHealthImprove`,
  `OnHealthRecover`, `PostConfigChanged`, `PostCrushMapChange` và `OnAudit`.
  Đây là nền tảng tốt để phát event sau khi collector hoặc action thay đổi dữ liệu.
- Tài liệu S3 multisite của croit gọi các ô sync là “live tiles”, nhưng cũng nói
  rõ dữ liệu sync là bất đồng bộ và có timestamp `Last sync`; đây là pattern cần
  áp dụng: hiển thị snapshot + tuổi dữ liệu + trạng thái refresh, không giả vờ
  rằng mỗi ô đang đọc trực tiếp Ceph.

Tài liệu tham khảo:

- [Croit GitHub organization](https://github.com/croit)
- [MCP Croit Ceph README](https://github.com/croit/mcp-croit-ceph)
- [MCP Croit Ceph architecture](https://github.com/croit/mcp-croit-ceph/blob/main/ARCHITECTURE.md)
- [Croit New Metrics and Logging System](https://www.croit.io/blog/croits-new-metrics-and-logging-system)
- [Croit Hook Scripts](https://www.croit.io/docs/hook-scripts/overview)
- [Croit S3 Multisite monitoring](https://www.croit.io/docs/accessing-your-cluster/s3-multisite)

---

## 3. Hiện trạng AIOps cần giữ làm baseline

Đã kiểm tra source trên server ngày 2026-09-14.

### 3.1 Luồng trang Dashboard chính

- `dashboard/static/ceph-health/app.js` gọi `/api/dashboard/health` khoảng mỗi
  5 giây.
- Backend tại `dashboard/routes/incidents.py` chỉ schedule live refresh khi
  snapshot persistent cũ hơn khoảng 60 giây.
- Snapshot health live hiện batch các lệnh như:
  `ceph -s`, `ceph osd perf`, `ceph osd dump` và `ceph orch host ls`.
- Watcher mặc định poll mỗi 15 giây (`config/settings.py`).

Kết quả: browser gọi nhanh nhưng backend có thể trả cùng một snapshot trong gần
60 giây. Đây là lý do “thời gian cập nhật” và trạng thái thực tế có thể chậm.

### 3.2 Luồng Pools/PGs

- `dashboard/routes/pgs.py` dùng `shared.object_storage_cache.get_or_load`.
- Cache Pools/PGs là process-local; TTL mặc định hiện là 3.600 giây và stale
  TTL được truyền là 900 giây.
- Khi cache miss trong production, route trả fallback rỗng và schedule loader
  nền; trang `pools.html`/`pgs.html` dùng `meta refresh` 3 giây khi đang loading.
- `_query_pool_rows()` lấy nhiều payload Ceph rồi normalize thành bảng.
- Cluster dùng cephadm phải đi qua Paramiko SSH và `cephadm shell`; source đã
  ghi nhận shell tạm có overhead khoảng 2,5 giây, chưa tính kết nối, lock và
  thời gian chạy lệnh.

Kết quả: refresh 3 giây không làm query Ceph nhanh hơn; nó chỉ khiến browser
  tải lại HTML nhiều lần trong lúc một loader nền vẫn đang chạy.

### 3.3 WebSocket hiện tại

`dashboard/ws.py` có `/ws/incidents`, nhưng fingerprint chỉ gồm số lượng
Incident và `max(updated_at)` trong DB. Docstring cũng ghi rõ đây là polling DB
đơn giản, chưa có event bus từ Watcher/Worker trực tiếp vào Dashboard.

WebSocket này chưa phát thay đổi của:

- cluster health snapshot;
- pool/PG inventory;
- CRUSH map;
- node/service status;
- action/job progress.

### 3.4 Điểm đúng đang có và không được phá

- `shared/ceph_query_cache.py` đã có persistent stale-if-error cache, atomic write,
  lock và background refresh; có thể tái sử dụng thay vì tạo cache thứ ba.
- `dashboard/cache_warmup.py` đã warm một số inventory sau startup.
- `watcher/main.py` đã có vòng poll theo cluster và cơ chế heartbeat.
- Các action qua Worker có state machine, audit và post-check; realtime UI chỉ
  được báo thành công sau khi state backend đã xác nhận, không báo ngay sau khi
  gửi lệnh.
- Mọi dữ liệu phải tiếp tục scoped bằng `cluster_id`; không fallback âm thầm về
  cluster mặc định.

### 3.5 Repo và mã nguồn tham khảo đã chọn

Các repo dưới đây chỉ là nguồn tham khảo cho pattern; không copy nguyên kiến
trúc hoặc đưa thêm service khi vertical slice hiện tại chưa chứng minh cần thiết:

- [`sysid/sse-starlette`](https://github.com/sysid/sse-starlette): SSE production
  cho FastAPI/Starlette, heartbeat, disconnect và test patterns.
- [`Azure/fetch-event-source`](https://github.com/Azure/fetch-event-source):
  client SSE có custom headers, retry, visibility handling và error policy.
- [`AkshatSoni26/longshot`](https://github.com/AkshatSoni26/longshot): pattern
  replay-then-tail, sequence monotonic, Redis Streams + live tail và idempotency.
- [`centrifugal/centrifugo`](https://github.com/centrifugal/centrifugo): lựa
  chọn scale-out khi cần nhiều realtime gateway; chưa đưa vào dependency mặc định.
- [`ag2ai/faststream`](https://github.com/ag2ai/faststream): tham khảo adapter
  RabbitMQ/Redis/Kafka và AsyncAPI; chưa thay `shared/mq.py` trong lát cắt này.
- [`TanStack/query`](https://github.com/TanStack/query): cache/invalidation phía
  frontend; event chỉ làm invalidate/refetch snapshot, không đẩy payload lớn.

Nguồn mapping mutation nội bộ: [`docs/realtime/mutation-inventory.md`](../../docs/realtime/mutation-inventory.md).

---

## 4. Kiến trúc đích

### 4.1 Nguyên tắc chính

```text
                         ┌────────────────────────┐
                         │ Ceph cluster / MON/MGR  │
                         └────────────┬───────────┘
                                      │
                         1 collector / cluster
                                      │
                    ┌─────────────────▼─────────────────┐
                    │ Cluster Snapshot Service           │
                    │ - query theo tier                  │
                    │ - retry/fallback MON               │
                    │ - stale-if-error                    │
                    │ - generation + collected_at        │
                    └───────────────┬───────────────────┘
                                    │
                 ┌──────────────────┴──────────────────┐
                 │                                     │
       ┌─────────▼─────────┐                 ┌─────────▼─────────┐
       │ Persistent cache/ │                 │ Event broadcaster │
       │ snapshot store    │                 │ WebSocket/SSE      │
       └─────────┬─────────┘                 └─────────┬─────────┘
                 │                                     │
       ┌─────────▼─────────────────────────────────────▼────────┐
       │ Fast read-only Dashboard API                             │
       │ /api/dashboard/health, /api/pools, /api/pgs, /api/state  │
       └──────────────────────────┬──────────────────────────────┘
                                  │
                       Browser fetch + event update
```

Browser request chỉ đọc snapshot. Collector mới là nơi được phép gọi Ceph cho
inventory và trạng thái định kỳ.

### 4.2 Snapshot theo tier, không poll mọi thứ cùng tần suất

Không chạy toàn bộ lệnh nặng mỗi 5 giây. Chia dữ liệu như sau:

| Tier | Dữ liệu | Chu kỳ ban đầu | Nguồn gợi ý |
|---|---|---:|---|
| Critical | `ceph -s`, health, quorum, OSD up/down, PG warning | 5–10 s | batch nhỏ |
| Operational | pool config, usage, PG summary, service state | 15–30 s | batch riêng |
| Inventory | Pools/PG rows, CRUSH rules/tree, node inventory | 30–60 s | refresh nền |
| Metrics | IOPS, latency, bandwidth, time series | 10–15 s | Watcher/metric store |
| Logs | RGW/access/daemon logs, RCA | theo job/cửa sổ | không chạy trong health poll |

Chu kỳ phải cấu hình được trong `config/settings.py` và `.env`, ví dụ:

```text
DASHBOARD_STATE_ENABLED=true
DASHBOARD_HEALTH_POLL_SECONDS=10
DASHBOARD_OPERATIONAL_POLL_SECONDS=20
DASHBOARD_INVENTORY_POLL_SECONDS=60
DASHBOARD_SNAPSHOT_STALE_SECONDS=90
DASHBOARD_SNAPSHOT_MAX_STALE_SECONDS=900
DASHBOARD_EVENT_ENABLED=true
```

Không dùng một biến duy nhất cho mọi loại dữ liệu; nếu giảm health xuống 5 giây
thì không được vô tình chạy `ceph pg dump pgs` mỗi 5 giây.

### 4.3 Hình dạng snapshot chuẩn

Mỗi snapshot phải có envelope thống nhất:

```json
{
  "cluster_id": "...",
  "generation": 1842,
  "collected_at": "2026-09-14T08:15:10.123Z",
  "published_at": "2026-09-14T08:15:10.140Z",
  "collector": "watcher|dashboard-fallback",
  "age_seconds": 2.1,
  "stale": false,
  "refreshing": false,
  "last_error": null,
  "health": {...},
  "pools": {...},
  "pgs": {...},
  "crush": {...},
  "nodes": {...},
  "capabilities": {...}
}
```

Quy tắc:

- `collected_at` là thời điểm collector nhận dữ liệu Ceph, không phải thời điểm
  browser render.
- `generation` tăng nguyên tử sau mỗi snapshot hợp lệ.
- Một subsection lỗi không được xóa các subsection hợp lệ trước đó; ghi
  `partial_errors` theo subsection.
- `stale=true` khi quá freshness budget nhưng snapshot vẫn còn trong
  `max_stale_seconds`.
- Chỉ trả `UNKNOWN`/empty khi chưa từng có snapshot hoặc snapshot quá cũ, không
  biến lỗi tạm thời thành “cụm không có pool”.

### 4.4 Nơi lưu snapshot

Triển khai theo hai bước:

**Bước đầu:** dùng `shared/ceph_query_cache.py` làm persistent snapshot store,
nhưng bổ sung schema envelope, namespace theo cluster và metadata freshness.
Cache hiện tại đã có disk JSON, atomic replace và lock nên phù hợp để giảm rủi
ro khi rollout.

**Bước ổn định:** thêm bảng DB `ClusterSnapshot` hoặc `ClusterStateSnapshot`
để giữ bản mới nhất theo cluster, generation, collected_at, payload và error.
DB giúp audit/diagnostic dễ hơn; payload lớn như full CRUSH tree có thể tách
namespace/file cache và DB chỉ giữ index + checksum.

Không sử dụng `shared.object_storage_cache.py` cho snapshot trung tâm lâu dài
nếu vẫn chỉ process-local; nhiều worker/process sẽ nhìn thấy freshness khác nhau.

### 4.5 Event transport

Ưu tiên tái sử dụng `/ws/incidents` nhưng đổi thành event channel có namespace
cluster; nếu muốn giảm rủi ro tương thích, tạo endpoint mới `/ws/cluster-state`
và giữ `/ws/incidents` cho client cũ trong một giai đoạn.

Event tối thiểu:

```json
{
  "event": "snapshot_changed",
  "cluster_id": "...",
  "generation": 1842,
  "sections": ["health", "pools"],
  "collected_at": "2026-09-14T08:15:10.123Z"
}
```

Các event khác:

- `snapshot_refresh_started`
- `snapshot_refresh_failed`
- `action_state_changed`
- `crush_map_changed`
- `cluster_selection_changed` chỉ là client event, không phải Ceph event.

Không gửi toàn bộ payload qua WebSocket; event chỉ là invalidation/hint. Browser
nhận event rồi gọi API read-only để lấy snapshot mới, tránh payload lớn, tránh
leak dữ liệu cluster khác và dễ retry khi event bị mất.

Fallback bắt buộc: nếu WebSocket đóng, frontend polling API 5–10 giây; nếu API
lỗi, giữ dữ liệu cũ và hiện stale/error.

---

## 5. Kế hoạch triển khai theo work package

### RT-00 — Baseline và đo đạc trước khi sửa `[~]`

- [x] Ghi trạng thái dirty tree và container đang chạy.
- [x] Đo thời gian từng đường hiện tại bằng monotonic timer:
  - `/api/dashboard/health` cache hit;
  - live health refresh;
  - `/pools` cache hit/miss;
  - `_query_pool_rows()`;
  - mỗi lệnh batch/SSH/cephadm.
- [x] Ghi exec mode, cadence Watcher, cache threshold và stale TTL.
- [x] Ghi kích thước rows Pools/PGs và xác định đường normalize JSON.
- [~] Chưa chụp baseline với 1, 5 và 10 tab browser; sẽ đo sau khi có snapshot
  collector để không tạo tải SSH/Ceph vô ích trên baseline cũ.
- [x] Không thay đổi timeout hoặc poll interval chỉ để làm benchmark đẹp hơn.

**Bằng chứng:** xem
`Plan/realtime-cluster-status-baseline-2026-09-14.md`.

**Exit gate còn thiếu:** benchmark nhiều tab và số lần SSH/cephadm trong một
cửa sổ có tải browser. Baseline đơn request đã xác định được bottleneck chính:
Pools khoảng 6,5–7,1 giây, PGs khoảng 16,6–19,9 giây, health khoảng 6,9–8,9
giây trên `CS-LAB`.

### RT-01 — Tạo snapshot contract và shared store [~]

- [x] Tạo module mới tại `shared/cluster_snapshot.py`; không nhồi toàn bộ logic vào route.
- [~] Định nghĩa envelope snapshot bằng mapping contract; sẽ nâng lên
  `TypedDict`/dataclass nếu các section collector cần type chặt hơn.
- [x] Thêm `cluster_id` bắt buộc vào key/path của mọi snapshot.
- [x] Bổ sung read/write atomic và generation monotonic; lock file/memory lock
  hiện được kế thừa từ `shared.ceph_query_cache`.
- [x] Cache không còn chạy loader khi không lấy được lock; versioned publish
  báo lỗi rõ ràng nếu lock hoặc persistent write thất bại.
- [~] Hoàn thiện checksum và lock chống hai collector cùng refresh một cluster;
  phần scheduler/lifecycle này sẽ chốt ở RT-03.
- [x] Có hàm đọc/ghi nền tảng:

```python
read_snapshot(cluster_id)
publish_snapshot(cluster_id, sections, ...)
```

- [x] Bổ sung `mark_refreshing`, `is_refreshing` và `invalidate_snapshot`; trạng
  thái refresh được lưu riêng theo `cluster_id`, có TTL chống marker bị kẹt,
  và snapshot thành công tự đóng marker.
- [x] Không xóa snapshot cũ nếu publish mới lỗi; publish chỉ thay thế cache sau
  khi payload mới đã được tạo.
- [x] Không trả snapshot của cluster A cho request cluster B: key cache bắt buộc
  dùng `cluster_id` và đã có test isolation.
- [x] Thêm test cho generation tăng sau reset memory, stale metadata, cluster
  isolation, input threshold/timestamp không hợp lệ, disk write failure,
  lock unavailable và đọc được bản mới do process khác ghi.

**Bằng chứng RT-01 slice hiện tại:**

- File mới: `shared/cluster_snapshot.py`.
- Cache update: `shared/ceph_query_cache.py` với `store_versioned(...)`.
- Test: `tests/test_cluster_snapshot.py` và `tests/test_ceph_query_cache.py`.
- Lệnh kiểm tra trên server: `.venv/bin/pytest -q tests/test_cluster_snapshot.py tests/test_dashboard_health_api.py` → **17 passed**.

Các mục lifecycle/refresh lock còn lại không được đánh dấu hoàn tất cho đến
RT-02/RT-03.

**Exit gate:** unit test cache hit/miss, stale, error, lock contention,
concurrent writer, restart process và cluster isolation đạt.

### RT-02 — Collector một lần cho mỗi cluster [~]

- [x] Tạo collector riêng tại `watcher/cluster_snapshot_collector.py` dùng
  chung snapshot store.
- [~] Mỗi cluster chỉ có một vòng poll trong `run_all_clusters()` (default loop
  và một observed loop cho từng cluster); lock liên process/singleton đầy đủ
  vẫn chốt ở RT-03.
- [x] Tái sử dụng `query_cluster_health()`/
  `query_cluster_health_with()` và cơ chế ordering/fallback MON hiện có.
- [x] Critical tier dùng đúng health query đã có, không phát sinh thêm SSH/Ceph
  query riêng để publish snapshot.
- [x] Tách critical health khỏi các scan nặng; Pools/PGs/CRUSH/RBD iostat chưa
  bị kéo vào cadence health.
- [~] Ghi metrics nội bộ:
  - [x] `collection.duration_ms` trong snapshot;
  - [x] `collector_success_total`/`collector_failure_total`;
  - [x] `snapshot_age_seconds` được tính khi đọc snapshot;
  - [x] metrics chi tiết theo từng tier/command outcome trong
    `cluster_snapshot_collector.get_metrics()`.
- [x] Khi collector lỗi, ghi `last_error`/`partial_errors.health` trên payload
  cũ nếu còn snapshot; không ghi payload rỗng đè dữ liệu tốt.
- [~] Khi Ceph trả payload một phần, contract đã có `partial_errors`; health
  query hiện chỉ commit nguyên payload thành công, việc tách section partial
  sẽ làm cùng các tier Pools/PGs ở RT-05.
- [x] Không collect logs, RBD iostat, full PG dump hoặc CRUSH detail trong
  critical tier.

**Bằng chứng RT-02 hiện tại:**

- Test collector/store: **17 passed**.
- Test tích hợp default/observed Watcher loop: **2 passed**.
- Query live một lần trên cluster mặc định `ac23b8ff-e235-414c-bed8-06894f3dedd3`:
  `HEALTH_WARN`, 4 checks, publish generation `1`, read-back `stale=False`.
- Review fix: lỗi health chỉ cập nhật `last_error`/`last_attempted_at`, giữ
  nguyên `generation`, `published_at` và tuổi dữ liệu; collector có counters
  nội bộ `success_total`/`failure_total`.
- Review fix: `run_all_clusters()` giữ process singleton lock tại
  `CEPH_AI_RUNTIME_DIR/watcher.lock` (mặc định `/run/ceph-ai/watcher.lock`).
- Service `ceph-ai-watcher.service` hiện đang `disabled/inactive` từ trước;
  chưa tự bật vì việc bật sẽ kích hoạt polling và gửi alert thật.

**Exit gate:** chạy 10 phút trên cluster test; một cluster chỉ có một refresh
cho mỗi tier; không có loop chết; snapshot có `collected_at` tăng đều.

### RT-03 — Warmup và lifecycle

- [x] Warmup startup chỉ đọc/hydrate shared persistent cluster snapshot, không
  còn gọi page-specific loader hoặc phát sinh thêm Ceph query.
- [x] App startup không block chờ warmup; Dashboard trả UI ngay. Warmup hiện
  tại đã chạy daemon background thread và không chạy trong test.
- [x] Khi cluster mới active, supervisor tự đăng ký trong tối đa 5 giây; khi
  inactive, observed loop tự dừng qua lần refresh cấu hình kế tiếp.
- [x] Khi config cluster/credential/exec mode đổi, collector đọc lại cấu hình
  ở đầu poll kế tiếp; khi inactive supervisor gửi `stop_event` để loop thoát
  nhanh và không chờ hết chu kỳ sleep.
- [x] Khi Watcher restart, snapshot disk vẫn đọc được; UI hiện tuổi dữ liệu và
  trạng thái stale thay vì trắng trang.
- [x] Khi app có nhiều process/container, dùng lock liên process hoặc tách
  collector thành service độc lập; không dựa riêng vào `threading.Lock`.

**Bằng chứng RT-03 slice hiện tại:**

- `watcher/main.py` có `_run_observed_cluster_supervisor()` và registry chống
  tạo loop trùng; supervisor phát hiện cluster active mới mà không cần restart
  Watcher.
- Cluster loop vẫn đọc lại cấu hình ở mỗi poll, nên thay đổi node/credential/
  exec mode được áp dụng ở poll kế tiếp; supervisor truyền `stop_event` để
  dừng hợp tác khi cluster bị tắt/xoá.
- CRUD/toggle/edit/delete tại `dashboard/routes/clusters.py` không còn restart
  Watcher; chỉ restart Worker ở các đường cần đồng bộ backup scheduler.
- `dashboard/cache_warmup.py` đọc snapshot theo từng `cluster_id` trong daemon
  thread; nếu chưa có snapshot thì chỉ ghi log chờ Watcher, không tự chạy SSH.
  Lỗi đọc của một cluster được cô lập, không làm bỏ qua các cluster còn lại.
- Test `test_reconcile_observed_cluster_threads_tracks_add_deactivate_and_reactivate`:
  **pass**.
- Test `test_run_observed_cluster_loop_honors_stop_event_before_poll`: **pass**.
- Test subprocess `test_persisted_snapshot_is_readable_after_process_restart`:
  **pass**; a fresh Python process read the persisted snapshot from disk.
- Test warmup snapshot/no-Ceph/failure-isolation: **3 passed**.
- Regression snapshot/collector/process-lock: **19 passed**.

**Exit gate:** restart web, restart Watcher, restart cả stack và disable/enable
cluster đều không tạo hai collector cùng cluster.

### RT-04 — Chuyển Dashboard health sang snapshot

- [x] Bỏ `_load_dashboard_health_live()` khỏi đường request; collector gọi
  `collect_and_publish_health()` cho lần refresh explicit.
- [x] `/api/dashboard/health` chỉ đọc snapshot và trả:
  `collected_at`, `age_seconds`, `stale`, `refreshing`, `generation`.
- [x] Có endpoint `POST /api/dashboard/health/refresh` cho operator; trigger
  chạy nền, single-flight theo `cluster_id`, không block request.
- [x] Bỏ ngưỡng refresh 60 giây khỏi GET; freshness lấy từ snapshot collector
  và stale policy.
- [x] Frontend hiển thị:
  - `Đang đồng bộ dữ liệu cụm` khi refresh đang chạy;
  - `Cập nhật X giây trước` dựa trên `collected_at`;
  - `Dữ liệu đang cũ` khi stale;
  - lỗi lần refresh gần nhất nếu có.
- [x] Không thay `collected_at` bằng giờ render; UI dùng `age_seconds` do API
  tính từ thời điểm collector thu thập.

**Bằng chứng RT-04 slice hiện tại:**

- `dashboard/routes/incidents.py` đọc `shared.cluster_snapshot.read_snapshot`
  và không còn gọi Ceph trong GET `/api/dashboard/health`.
- `POST /api/dashboard/health/refresh` trả `202` ngay; worker nền gọi collector
  và giữ lock theo cluster.
- `ceph-health-dashboard/src/components/CephDashboard.tsx` hiển thị tuổi dữ
  liệu, generation, trạng thái syncing/stale và nút refresh.
- Regression API/collector/cache/warmup: **28 passed**; frontend build bằng
  Node 20/Vite: **passed**.

**Exit gate:** browser gọi API 5 giây nhưng Ceph collector chỉ chạy đúng cadence;
health đổi được phản ánh trong SLA; reload trang không làm tăng số query bất
thường.

### RT-05 — Chuyển Pools/PGs/CRUSH/Nodes sang read snapshot `[~]`

- [x] Tạo read model/API cho từng section:
  - `/api/pools?cluster_id=...`;
  - `/api/pgs?cluster_id=...`;
  - `/api/crush-map/tree?cluster_id=...`;
  - `/api/nodes/summary?cluster_id=...`.
- [~] API trả dữ liệu từ snapshot; pagination/filter hiện xử lý local ở frontend,
  phần query pagination/filter contract thống nhất sẽ hoàn thiện cùng RT-07.
- [x] Không pagination sau khi
  đã gọi Ceph trong request.
- [x] Giữ schema field hiện tại để giảm thay đổi UI; bổ sung metadata freshness.
- [x] Pools React bỏ `meta refresh`; dùng fetch API, giữ rows cũ trong lúc
  refresh và cập nhật bảng khi generation đổi.
- [~] PGs/CRUSH/Nodes đọc snapshot và không mở SSH trong request; hook freshness
  dùng chung và event handler sẽ hoàn thiện ở RT-06/RT-07.
- [x] Full CRUSH map chỉ refresh khi có event/action hoặc inventory tier; không
  lấy lại mỗi lần người dùng mở/đóng tab.
- [~] Filter/search/pagination hiện xử lý local snapshot, nên không chạm Ceph;
  cursor pagination cho payload lớn sẽ hoàn thiện ở lát cắt sau.

**Bằng chứng RT-05 slice hiện tại:**

- `shared/cluster_snapshot.py` có section snapshot độc lập với generation và
  freshness metadata.
- `watcher/cluster_snapshot_collector.py` có inventory tier Pools/PGs/CRUSH/Nodes,
  lock theo cluster và stale-if-error.
- Các API `/api/pools`, `/api/pgs`, `/api/crush-map/tree` và
  `/api/nodes/summary` đọc persistent snapshot; page routes production không
  tự mở Ceph query.
- Regression Pool/PG/CRUSH/Nodes/snapshot: **66 passed**; frontend build và
  JavaScript syntax checks: **passed**.

**Exit gate:** chuyển giữa Pools/PGs/CRUSH/Nodes không mở SSH; dữ liệu cũ hiển
thị ngay; snapshot mới tự thay đúng section.

### RT-06 — WebSocket/SSE event bus

- [x] Persistent event broadcaster metadata được lưu theo `cluster_id` trong
  `shared/cluster_events.py`; payload chỉ là invalidation hint, không chứa
  snapshot lớn hoặc secret.
- [x] Tạo endpoint `/ws/cluster-state`; auth dùng session HTTP, Vitastor bị
  chặn và `cluster_id` trên query phải khớp cluster đã chọn.
- [x] Collector/snapshot publisher phát `snapshot_changed` sau khi commit
  snapshot/section thành công.
- [x] Worker/DB publish `action_state_changed` sau mỗi transition state bền
  vững, sau commit và không sau rollback. Evidence: `shared/db.py` hooks and
  `tests/test_dashboard_ws.py` action-state commit/rollback tests.
- [x] CRUSH/Pool and related mutation scope publish sau post-check-confirmed
  `Incident.RESOLVED`, không publish “success” ngay khi mới enqueue. Evidence:
  resolved-incident mapping tests for Pool, CRUSH, RGW, and Deploy.
- [x] Debounce event liên tiếp trong khoảng 100–300 ms để một batch query chỉ
  tạo một lần refresh UI; server giữ event mới nhất, React coalesces trong
  150 ms, và legacy snapshot pages share one socket. Evidence: commit
  `38ecab34` and `tests/test_dashboard_ws.py`.
- [x] Client reconnect exponential backoff; HTTP polling vẫn là fallback khi
  WebSocket không được proxy hỗ trợ hoặc event bị mất.

**Exit gate:** mở DevTools vẫn thấy event/GET đúng cluster; đóng WebSocket
không làm UI đứng; event cluster B không xuất hiện ở tab cluster A.

### RT-07 — Chuẩn hóa frontend status/freshness

- [~] Health and Pools React pages plus PG/CRUSH/Nodes snapshot pages now use
  scoped event invalidation, hidden-tab handling, and HTTP fallback. Health/
  Pools use abortable sequence-safe reads; equivalent treatment for every
  remaining section is still open.
- [x] Tạo shared hook `useClusterSnapshotEvents()` với reconnect backoff,
  cluster filtering và no-payload event handling.
- [~] Các trạng thái phải phân biệt:
  - `loading`: chưa có snapshot;
  - `refreshing`: đang lấy snapshot mới nhưng vẫn có dữ liệu cũ;
  - `fresh`: trong freshness budget;
  - `stale`: quá budget nhưng còn snapshot dùng được;
  - `error`: refresh thất bại;
  - `unknown`: chưa từng có dữ liệu. React health/Pools implement the shared
    labels; remaining pages still need the same visual contract.
- [~] Không dùng `window.location.reload()` để cập nhật read-only state trong
  health, Pools, PGs, CRUSH và Nodes; mutation/progress pages still have
  intentional reloads.
- [~] Fetch có `AbortController`, tránh response cũ ghi đè response mới trong
  React health/Pools and sequence guards in migrated legacy reads; the remaining
  static pages still need abort wiring.
- [x] Không chạy polling khi tab hidden; khi tab visible lại fetch một lần cho
  các migrated snapshot pages.
- [~] Cập nhật timestamp bằng `collected_at` từ server cho migrated snapshot
  pages; remaining pages still need the shared freshness component.
- [~] Nút `Refresh` chỉ trigger background refresh và disable chống double click;
  health/Pools/CRUSH satisfy this, while remaining mutation pages are open.
- [x] Giữ giao diện cũ khi JS lỗi ở mức HTML snapshot tối thiểu hoặc báo lỗi rõ;
  server-rendered snapshot pages keep their initial table/status markup.

**Exit gate:** mọi trang có cùng cách hiển thị freshness, không còn refresh loop
3 giây và không nhấp nháy toàn trang.

**RT-06/RT-07 evidence (2026-09-19):**

- `tests/test_cluster_events.py`, `tests/test_cluster_snapshot.py`,
  `tests/test_dashboard_ws.py`: **18 passed**.
- Health/Pool/PG/CRUSH/Nodes/collector regression: **138 passed, 1 warning**
  in the current realtime gate; Node 20 static bridge syntax checks and the
  broader deterministic release suite also pass.
- Node 20 frontend type-check and production build: **passed**.
- Remaining: universal freshness badge, browser multi-tab proof, and
  event/load observability.

### RT-08 — Invalidation sau mutation và liên kết trạng thái

- [x] Liệt kê mọi mutation ảnh hưởng cluster state và map sang section/event;
  evidence: `docs/realtime/mutation-inventory.md` (2026-09-21).
  Các hạng mục còn lại trong RT-08 chỉ được đánh dấu sau khi producer/post-check
  tương ứng có test và evidence riêng:
  - pool create/edit/delete/protection;
  - CRUSH add/remove/reweight/rule change;
  - OSD/service restart;
  - upgrade/deploy;
  - RGW/S3 configuration;
  - node/cluster config.
- [x] Sau khi Worker nhận request: trạng thái UI là `queued/running`, chưa
  invalidate thành công; Action commit events đã phát đúng hai trạng thái.
  Evidence: `test_action_lifecycle_realtime_contract_reaches_postcheck_success`
  (2026-09-21).
- [x] Sau khi executor hoàn tất và post-check xác nhận thành công: ghi marker
  priority refresh theo cluster; Watcher đọc marker để chạy status/inventory
  collector sớm hơn cadence, không block request Dashboard. Evidence:
  `shared/cluster_snapshot.py`, `watcher/main.py`,
  `test_priority_refresh_marker_is_cluster_scoped_and_bounded` — **55 passed**
  (2026-09-21).
- [x] Chỉ chuyển `succeeded` sau post-check thấy state Ceph đã đổi; nếu chưa
  đổi thì giữ `verifying` và tiếp tục poll có giới hạn. Evidence: lifecycle
  contract test và `tests/test_incident_verification.py` (2026-09-21).
- [x] Snapshot post-check event payload chứa bounded `action_id`,
  `action_status`, `cluster_id`, `sections` và generation mới nếu đã có; không
  chứa SSH key/token/command secret. Evidence: commit `12906def` and
  `test_postcheck_snapshot_event_carries_bounded_action_metadata`.
- [x] Chuẩn hóa bounded contract cho `action_state_changed`: giữ nguyên
  `action_status` nội bộ và bổ sung `action_state` gồm `queued/running/
  verifying/succeeded/failed/rejected`; WebSocket forward contract và frontend
  type đã được cập nhật. Evidence: `tests/test_cluster_events.py`,
  `tests/test_dashboard_ws.py` — **24 passed** (2026-09-21).
- [ ] Nối contract `action_state_changed` vào mọi consumer/UI của các trang
  mutation/progress; không đánh dấu mục này chỉ vì payload đã chuẩn hóa.
- [~] Consumer Cluster Overview và Pools đã nhận `snapshot_refresh_failed` để
  hiển thị lỗi post-check và giữ snapshot cũ; `snapshot_changed` sẽ gỡ cảnh
  báo. Các trang mutation/progress còn lại vẫn cần migrate.
- [x] Nếu post-check thất bại sau `VERIFYING`, phát `snapshot_refresh_failed`
  với `action_state=failed`; snapshot tốt trước đó không bị invalidate. Evidence:
  `test_failed_postcheck_publishes_error_without_claiming_snapshot_success` —
  **39 passed** (2026-09-21).

**Exit gate:** thao tác thành công làm Pools/CRUSH/health cập nhật trong SLA;
thao tác thất bại không làm mất dữ liệu cũ hoặc báo thành công giả.

### RT-09 — Observability, giới hạn tải và vận hành

- [ ] Có dashboard/log cho query duration, stale age, collector lag, event
  reconnect, WebSocket client count và refresh error.
- [ ] Giới hạn concurrency theo cluster và toàn hệ thống.
- [ ] Circuit breaker/backoff khi MON/Cephadm lỗi liên tiếp.
- [x] Không retry đồng thời ở route, collector và browser theo cấp số nhân. Dashboard health chỉ đọc snapshot; refresh là POST async có lock/coalescing theo cluster; Watcher là owner duy nhất của Ceph health polling/retry; browser chỉ polling fallback, không tự retry Ceph.
- [x] Alert nếu snapshot quá `max_stale_seconds`, collector chết hoặc event bus
  không publish. Diagnostics admin trả alert bounded cho snapshot unavailable/stale,
  refresh error, Watcher heartbeat stale và event publish failure.
- [ ] Ghi correlation id từ browser request → event → collector/action.
- [ ] Kiểm tra disk cache growth; payload CRUSH/PG phải có giới hạn kích thước.

**Exit gate:** có thể trả lời từ log/metric: dữ liệu đang chậm do collector,
Ceph, SSH, cache hay browser; không suy đoán bằng timestamp giao diện.

---

## 6. Các file dự kiến phải sửa

Không sửa đồng loạt hoặc ghi đè các file đang dirty. Trước mỗi work package phải
đọc `git diff` của file đó và giữ nguyên thay đổi hiện có.

### Backend/shared

- `shared/ceph_query_cache.py`: envelope, freshness, generation, cross-process
  lock/publish hook nếu chọn cache file ở phase đầu.
- `shared/models.py`: `ClusterSnapshot` nếu chọn DB persistent ở phase ổn định.
- `alembic/versions/...`: migration cho snapshot metadata nếu cần.
- `watcher/main.py`: đăng ký collector theo cluster và cadence.
- `watcher/ceph_client.py`: batch/query helper, timeout, connection/lock metrics.
- File mới nên cân nhắc:
  - `watcher/cluster_snapshot.py`;
  - `shared/cluster_snapshot.py`;
  - `shared/cluster_events.py`.

### Dashboard API/WebSocket

- `dashboard/routes/incidents.py`: health API đọc snapshot, bỏ refresh 60 giây
  gắn với request.
- `dashboard/routes/pgs.py`: Pools/PGs đọc read model và giữ mutation route.
- `dashboard/routes/crush_map.py`: CRUSH read model + invalidation.
- `dashboard/routes/nodes.py`: summary đọc snapshot; metrics time series giữ
  đường riêng nếu cần.
- `dashboard/ws.py`: event channel, auth, reconnect/invalidation protocol.
- `dashboard/cache_warmup.py`: warm collector, không tạo loader riêng theo page.
- `dashboard/app.py`: lifecycle start/stop collector và broadcaster.

### Frontend/templates

- `dashboard/templates/index.html`.
- `dashboard/templates/pools.html`.
- `dashboard/templates/pgs.html`.
- `dashboard/templates/crush_map.html`.
- `dashboard/static/ceph-health/app.js` và source trong
  `ceph-health-dashboard/src/`.
- `dashboard/static/nodes.js`.
- Có thể tạo `dashboard/static/cluster_state.js` dùng chung cho các trang HTML
  không phải React.

### Tests

- `tests/test_ceph_client.py`.
- `tests/test_dashboard_pools.py`.
- `tests/test_dashboard_navigation.py`.
- `tests/test_dashboard_crush_map.py`.
- `tests/test_watcher_incident_flow.py`.
- Test mới cho snapshot, event auth, stale/cache và collector concurrency.

---

## 7. API contract đề xuất

### 7.1 Health snapshot

`GET /api/dashboard/health?cluster=<id>`

Response tối thiểu:

```json
{
  "cluster_id": "...",
  "generation": 1842,
  "collected_at": "2026-09-14T08:15:10.123Z",
  "age_seconds": 3.4,
  "stale": false,
  "refreshing": true,
  "last_error": null,
  "health": "HEALTH_OK",
  "osds": {"up": 3, "total": 3},
  "mons": {"up": 3, "total": 3},
  "servers": {"online": 3, "total": 3},
  "utilization": {...},
  "metrics": {...},
  "placement_groups": "OKAY"
}
```

`refreshing=true` không được làm mất các giá trị health/osd đang hiển thị.

### 7.2 Section inventory

`GET /api/cluster/snapshot?cluster=<id>&sections=health,pools`

Cho phép page lấy một response nếu nhiều section cùng cần; `sections` phải được
validate bằng enum phía server.

`GET /api/pools?cluster=<id>&page=1&page_size=10&query=...`

Response phải có:

```json
{
  "cluster_id": "...",
  "generation": 1842,
  "collected_at": "...",
  "stale": false,
  "refreshing": false,
  "items": [...],
  "page": 1,
  "page_size": 10,
  "total": 17,
  "partial_errors": []
}
```

### 7.3 Event WebSocket

`GET/WS /ws/cluster-state?cluster=<id>`

Handshake phải kiểm tra session, user và cluster selection. Server chỉ gửi event
metadata; client fetch API sau đó.

Nếu client gửi subscribe message, chỉ cho phép:

```json
{"sections":["health","pools","crush"]}
```

Không cho client gửi command Ceph, path, host, keyring hay arbitrary topic.

---

## 8. Test plan chi tiết

### 8.1 Unit test

- Snapshot read/write atomic.
- Generation tăng đúng và không lùi khi hai writer hoàn tất ngược thứ tự.
- Cache hit không gọi loader.
- Cache stale trả giá trị cũ và chỉ schedule một refresh.
- Refresh exception giữ snapshot cũ + `last_error`.
- Snapshot quá `max_stale` trả UNKNOWN có lý do.
- Cluster A không đọc file/row/cache key của cluster B.
- Một cluster chỉ có một collector refresh cho mỗi tier.
- Partial batch không xóa section hợp lệ.
- Clock/UTC serialization chuẩn; không trộn naive local time với UTC.

### 8.2 API test

- API trả nhanh khi collector đang bận.
- API có `collected_at`, generation, stale, refreshing.
- `cluster` sai hoặc cluster inactive fail-closed.
- Viewer không trigger mutation hoặc arbitrary refresh command.
- API không trả credential/SSH path/secret.
- Pagination/filter chỉ thao tác trên snapshot.
- Refresh manual trả 202/accepted hoặc trạng thái rõ, không block lâu.

### 8.3 WebSocket test

- Chưa login bị close 1008.
- User cluster A không nhận event cluster B.
- Collector event đúng sections/generation.
- Event burst được debounce.
- Disconnect/reconnect fetch bù một lần.
- Event mất không làm UI stale vô hạn vì polling fallback.
- Nhiều client không làm số collector tăng.

### 8.4 Frontend test

- Snapshot cũ vẫn render trong khi refresh.
- Response cũ đến sau không ghi đè response mới.
- `collected_at` thay đổi thì timestamp reset.
- Tab hidden không poll; visible lại fetch.
- WebSocket fail vẫn cập nhật bằng polling.
- Không còn `meta refresh` cho Pools/PGs.
- Không còn `window.location.reload()` cho read-only status refresh.
- Nút refresh không tạo double request.

### 8.5 Live benchmark read-only

Trên cluster lab, chạy ít nhất:

```bash
time curl -k -b /path/to/session-cookie \
  'http://127.0.0.1:8000/api/dashboard/health?cluster=<id>'

time curl -k -b /path/to/session-cookie \
  'http://127.0.0.1:8000/api/pools?cluster=<id>&page=1&page_size=10'
```

Đồng thời theo dõi:

```bash
podman logs --since 10m ceph-ai_dashboard-web_1
grep -E "collector|cephadm|snapshot|query_duration|event" /var/log/ceph-ai-*.log
```

Đo 1 tab, 10 tab, reload liên tục và MON failover. Không chạy mutation để
benchmark realtime.

---

## 9. Rollout an toàn

### Feature flag

Triển khai flag mặc định tắt ở lần đầu:

```text
DASHBOARD_CLUSTER_SNAPSHOT_ENABLED=false
DASHBOARD_CLUSTER_EVENTS_ENABLED=false
```

Khi collector và API mới đã được kiểm thử:

1. Bật collector nhưng UI vẫn đọc đường cũ, chỉ ghi metric so sánh.
2. Bật API snapshot cho Dashboard chính.
3. Bật Pools/PGs từng trang.
4. Bật WebSocket event.
5. Bật invalidation sau mutation.
6. Sau ít nhất một chu kỳ vận hành ổn định mới bỏ đường cũ.

### Rollback

- Tắt feature flag event/snapshot để route quay về cache cũ.
- Không xóa snapshot store trong rollback.
- Giữ schema migration backward-compatible; rollback code không được làm app
  không đọc được DB/cache cũ.
- Nếu collector làm tăng tải Ceph, tăng cadence về mức an toàn và tắt các tier
  nặng trước khi tắt toàn bộ.
- Sau rollback xác nhận health/action verification vẫn chạy, không chỉ xác nhận
  UI mở được.

### Điều kiện không được rollout

- Chưa có cluster isolation test.
- Chưa biết collector thực sự chạy bao nhiêu lệnh/phút.
- Chưa có stale/error state rõ.
- Chưa có post-check cho mutation.
- Chưa xử lý nhiều process/container.
- Chưa đo khi cephadm lock bị tranh chấp.

---

## 10. Thứ tự thực hiện khuyến nghị

1. **RT-00:** đo baseline.
2. **RT-01:** snapshot contract/store.
3. **RT-02:** collector critical health, chưa đụng UI.
4. **RT-03:** lifecycle/warmup/lock.
5. **RT-04:** Dashboard health đọc snapshot.
6. **RT-05:** Pools trước, sau đó PGs, CRUSH, Nodes.
7. **RT-06:** WebSocket event + polling fallback.
8. **RT-07:** UI freshness dùng chung.
9. **RT-08:** mutation invalidation/post-check event.
10. **RT-09:** metrics, alerting, giới hạn tải và dọn đường cũ.

Không nên bắt đầu bằng việc giảm `meta refresh` từ 3 giây xuống 1 giây hoặc
giảm TTL xuống vài giây. Cách đó chỉ nhân số request/SSH và làm cluster chậm
hơn; vấn đề cần giải quyết là data path, không phải tốc độ reload.

---

## 11. Checklist nghiệm thu cuối

- [ ] Mở 10 tab không tạo 10 collector.
- [ ] Trang mở ngay với snapshot gần nhất.
- [ ] Không còn request page reload định kỳ để cập nhật trạng thái.
- [ ] Pools/PGs/CRUSH/Nodes không gọi SSH trong browser request bình thường.
- [ ] Health thay đổi được phản ánh trong 10 giây ở lab benchmark.
- [ ] Action thành công chỉ phát event sau post-check.
- [ ] Action thất bại giữ snapshot cũ và báo lỗi.
- [ ] Timestamp hiển thị là `collected_at`, có timezone rõ và tuổi dữ liệu đúng.
- [ ] Ceph/MON chậm không làm bảng trắng hoặc biến thành “chưa có pool”.
- [ ] WebSocket mất kết nối vẫn có polling fallback.
- [ ] Cluster A/B không lẫn snapshot, event, cache hoặc action.
- [ ] Viewer/admin RBAC không thay đổi ngoài ý muốn.
- [ ] Không lộ credential trong API/event/log.
- [ ] Full test suite đạt; các test fail phải phân loại nguyên nhân, không bỏ qua.
- [ ] Có log/metric chứng minh p95 và query rate đạt mục tiêu.
- [ ] Đã ghi lại cách rollback và flag hiện đang bật/tắt.

---

## 12. Ghi chú khi bắt tay thực hiện

- Worktree trên server đang có nhiều thay đổi chưa commit. Không dùng
  `git reset --hard`, `git checkout --`, hoặc ghi đè hàng loạt file.
- Trước mỗi patch, chạy `git diff -- <file>` và giữ thay đổi của các hạng mục
  Pools/CRUSH/Object Storage đã làm trước đó.
- Frontend hiện build cần Node mới hơn Node mặc định trên server. Chuẩn bị
  artifact/build environment trước khi sửa bundle; không xem copy source là đã
  deploy frontend.
- Mọi thay đổi production phải có: test, build, restart đúng container, health
  check và kiểm tra HTTP/API sau restart.
- Ưu tiên một vertical slice hoàn chỉnh (health snapshot → API → event → UI)
  rồi mới nhân sang Pools/PGs/CRUSH. Đừng triển khai 4 cơ chế polling khác nhau.

**Điểm bắt đầu đề xuất:** đọc và review RT-00 đến RT-04 trước; sau khi chốt
schema snapshot và cadence health mới viết code. Đây là phần quyết định 80% độ
mượt của toàn bộ các trang còn lại.

## 13. Execution log

| Ngày | Work package | Kết quả | Evidence |
| --- | --- | --- | --- |
| 2026-09-21 | RT-08.1 mutation inventory | Hoàn thành; phân loại mutation theo section, lifecycle event và post-check owner | `docs/realtime/mutation-inventory.md` |
| 2026-09-21 | Source selection | Hoàn thành; chọn SSE + fetch-event-source + replay-then-tail làm pattern tham khảo | Mục 3.5 |
| 2026-09-21 | RT-08.2 action state contract | Hoàn thành contract bounded, giữ raw status, forward qua WebSocket và khai báo frontend type; backend realtime tests đạt | `tests/test_cluster_events.py`, `tests/test_dashboard_ws.py` — **24 passed** |
| 2026-09-21 | RT-08.3 lifecycle/post-check gate | Xác nhận event sequence `queued → running → verifying → succeeded`; không phát `succeeded` trước post-check | `tests/test_dashboard_ws.py`, `tests/test_incident_verification.py` — **38 passed** |
| 2026-09-21 | RT-08.4 failed post-check event | Phát `snapshot_refresh_failed` với bounded action metadata, giữ snapshot trước đó và không báo thành công giả | `shared/db.py`, `tests/test_dashboard_ws.py` — **39 passed** |
| 2026-09-21 | RT-08.5 priority collector refresh | Post-check success ghi marker persistent; Watcher ưu tiên status/inventory refresh và marker tự hết hạn | `shared/cluster_snapshot.py`, `watcher/main.py`, `tests/test_cluster_snapshot.py` — **55 passed** |
| 2026-09-21 | RT-08.5 reliability follow-up | Không đánh dấu marker đã xử lý khi auxiliary collector đang bị lock; lần sau sẽ retry | `watcher/main.py`, `tests/test_watcher_main.py` + `tests/test_remediation_watcher.py` — **54 passed** |
| 2026-09-21 | RT-08.7 post-check success gate | Chỉ `VERIFYING → RESOLVED` mới phát `action_state=succeeded` và priority refresh; manual resolution không báo success | `shared/db.py`, `tests/test_dashboard_ws.py` — **56 passed** |
| 2026-09-21 | RT-08.8 priority marker identity | Priority marker có `request_id` UUID; không phụ thuộc timestamp milliseconds để phân biệt các mutation liên tiếp | `shared/cluster_snapshot.py`, `watcher/main.py`, `tests/test_cluster_snapshot.py` — **94 passed** |
| 2026-09-21 | RT-08.9 Upgrade consumer | Trang Upgrade nhận event theo cluster và đọc lại progress/status ngay; polling cũ vẫn giữ làm fallback | `dashboard/templates/upgrade.html`, `dashboard/static/upgrade.js`, `tests/test_dashboard_upgrade.py` — **73 passed** |
| 2026-09-21 | RT-08.6 first UI consumers | Cluster Overview và Pools hiển thị failure event, giữ snapshot cũ và tự clear khi snapshot mới thành công; frontend type-check/build đạt bằng Node 22 tạm thời | `ceph-health-dashboard/src/useClusterSnapshotEvents.ts`, `CephDashboard.tsx`, `PoolsPage.tsx`, `dashboard/static/ceph-health/app.js` |
| 2026-09-21 | RT-08.10 legacy snapshot consumers | PGs, CRUSH Map và Nodes hiển thị cảnh báo post-check thất bại, giữ snapshot cũ và tự ẩn khi nhận `snapshot_changed`; polling/WebSocket fallback không đổi | `dashboard/static/app.js`, `pgs.js`, `crush_map.js`, `nodes.js` + legacy templates |
| 2026-09-21 | RT-08.11 Deploy consumer | Deploy Cluster subscribe event theo default cluster/action, poll progress ngay khi lifecycle đổi, hiển thị cảnh báo post-check và vẫn giữ polling fallback | `dashboard/routes/deploy_cluster.py`, `dashboard/templates/deploy_cluster.html`, `dashboard/static/deploy_cluster.js`, `tests/test_dashboard_deploy_cluster.py` — **41 passed** |
| 2026-09-21 | RT-09.1 freshness diagnostics | Admin diagnostics thêm snapshot freshness theo từng cluster: generation, collected/attempted time, age, stale, refreshing và last error; không lộ payload/credential | `dashboard/routes/system_health.py`, `tests/test_ceph_debug.py` — **4 passed** |
| 2026-09-21 | RT-09.2 bounded inventory payloads | PG/CRUSH snapshot có giới hạn byte và số item/node cấu hình được; payload bị cắt giữ metadata `payload_limits`, không làm mất toàn bộ snapshot | `config/settings.py`, `shared/cluster_snapshot.py`, `tests/test_cluster_snapshot.py` — **2 tests added** |
| 2026-09-21 | RT-09.3 MON circuit breaker | Health query có circuit breaker theo endpoint/credential/mode, cooldown + probe; khi MON lỗi liên tiếp không tiếp tục tạo SSH load, success reset state; diagnostics chỉ trả counter bounded | `config/settings.py`, `watcher/ceph_client.py`, `dashboard/routes/system_health.py`, `tests/test_ceph_client.py`, `tests/test_ceph_debug.py` |
| 2026-09-21 | RT-09.4 retry ownership | Route không chạy Ceph và không tự retry; refresh được coalescing theo cluster; retry/backoff chỉ nằm trong Watcher health query, còn browser giữ polling fallback không nhân retry; regression xác nhận GET không schedule refresh và POST dùng async refresh | `dashboard/routes/incidents.py`, `ceph-health-dashboard/src/components/CephDashboard.tsx`, `tests/test_dashboard_health_api.py` — **2 tests** |
| 2026-09-21 | RT-09.5 operational alerts | Admin diagnostics thêm event publish counters và alert bounded cho snapshot unavailable/stale, refresh error, Watcher heartbeat stale, event bus publish failure; không lộ payload/credential | `shared/cluster_events.py`, `dashboard/routes/system_health.py`, `tests/test_cluster_events.py`, `tests/test_ceph_debug.py` — **22 passed** |
