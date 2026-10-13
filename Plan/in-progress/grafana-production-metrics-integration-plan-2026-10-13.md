# Kế hoạch tích hợp metrics production từ Grafana cho Ceph-AI

**Ngày lập:** 13/10/2026
**Trạng thái:** `IN-PROGRESS — VPN UI/HOST PREPARED`; chưa có profile/peer VPN, tunnel chưa kết nối, chưa có connector Grafana và chưa chạy query metrics production từ Ceph-AI.
**Phạm vi:** đánh giá và xây luồng lấy metrics read-only cho cụm CS-LAB từ hệ thống đang được trình bày qua Grafana; đo ảnh hưởng lên Grafana và datasource trước khi cho dữ liệu tham gia dashboard, RCA, cảnh báo hoặc AI.
**Không thuộc phạm vi:** sửa dashboard Grafana, thay datasource/Prometheus, thay scrape configuration, cấp quyền ghi, tự động remediation dựa trên dữ liệu mới, hoặc bật truy vấn production trước khi có owner approval.

## 1. Mục tiêu và quyết định cần đạt

Trả lời bằng bằng chứng bốn câu hỏi:

1. Ceph-AI có thể truy cập đúng API và lấy được đúng series/labels/timestamps cần dùng không?
2. Chi phí truy vấn làm tăng tải Grafana và backend datasource bao nhiêu trong giờ bình thường và giờ cao điểm?
3. Node Ceph-AI, PostgreSQL và retention hiện tại có chịu được dữ liệu mới mà không đầy đĩa/tăng trưởng DB mất kiểm soát không?
4. Metrics mới có đủ freshness, độ phủ, chất lượng và tương quan với Ceph topology để dùng trong sản phẩm không?

Kết quả cuối phải là một trong ba quyết định có operator/Grafana owner xác nhận:

- `GO`: cho phép bật theo quota/feature flag đã ký duyệt.
- `LIMITED-GO`: chỉ cho phép một allowlist metrics và giới hạn thấp; tiếp tục shadow.
- `NO-GO`: chưa dùng metrics production; lưu nguyên nhân, số liệu và điều kiện để đánh giá lại.

**Nguyên tắc:** mở được URL Grafana không đồng nghĩa Ceph-AI đã được tích hợp; query read-only không sửa dashboard nhưng vẫn tiêu thụ tài nguyên Grafana và datasource. Không có tuyên bố “zero impact”.

## 2. Hiện trạng đã xác minh trên server

Các dữ kiện dưới đây được kiểm tra read-only trong phiên lập kế hoạch; số đo thay đổi theo thời gian và không thay cho baseline mới ngay trước pilot.

| Hạng mục | Quan sát | Ý nghĩa |
|---|---|---|
| URL Grafana | `https://metrics.vccloud.vn:3000/dashboards/f/rkg-1HKiz/?orgId=1`; từ máy local trả về trang `/login`, chưa xác minh nội dung dashboard sau đăng nhập | URL là giao diện Grafana; phải xác định datasource/API và quyền service account riêng |
| Kết nối từ Ceph-AI | DNS trả `123.30.242.91`; server `10.3.55.213` gửi TCP SYN tới port 3000 nhưng không nhận phản hồi; firewall local có OUTPUT policy `ACCEPT`, firewalld không chạy. Public egress server tại thời điểm đo: `222.255.27.220` | Cần Grafana/network owner kiểm tra ACL/allowlist/route. Chưa chạy query metric nào từ server |
| Tích hợp hiện tại | Không tìm thấy Grafana connector, datasource UID/config hoặc `/api/ds/query` integration trong code runtime đã rà soát | Đây là tính năng mới; không được coi các node metrics hiện có là dữ liệu Grafana |
| Nguồn telemetry hiện tại | Node metrics lấy bằng collector/SSH; một số collector gọi Ceph CLI; Loki collector truy vấn Loki API trực tiếp | Nên giữ provenance riêng, tránh trộn nguồn và tránh thu thập trùng không cần thiết |
| Database Ceph-AI | PostgreSQL host `10.3.54.241` (không phải node Ceph-AI `10.3.55.213`) | Metrics được Ceph-AI persist nằm ở DB host này; tải ghi và dung lượng phải đo ở DB riêng |
| Dữ liệu CS-LAB hiện có | Tại lần kiểm tra: 162.167 `host_metric_samples`, 1.473 `volume_metrics`, 6 OSD distribution rows và 2 CRUSH structure snapshots | Baseline sơ bộ; row count không cho biết byte growth hay ingest rate |
| Dung lượng node ứng dụng | Lần đo 13/10: khoảng 14 GiB còn trống trên filesystem 148 GiB (91% đã dùng); RAM available khoảng 24 GiB/31 GiB | Disk node không phải DB disk; local image/log/artifact vẫn có nguy cơ chạm ngưỡng. Phải đo lại trước build/pilot |
| Retention hiện tại | `VolumeMetric` append-only; model ghi rõ chưa có automatic pruning và purge thủ công hiện có | Không được thêm sample dày vào bảng append-only mà chưa có retention/quota/job và dự báo storage |

### 2.1 Workstream VPN — đường hầm mạng tới Grafana

Đây là **VPN ở cấp host/network để tạo đường hầm từ server Ceph-AI tới mạng chứa Grafana**. Trang Settings mới chỉ điều khiển host helper và nhận file `.ovpn`; OpenVPN không chạy trong Dashboard container hoặc trên máy người dùng. VPN chỉ là một phương án network path; cần so sánh với allowlist/route trực tiếp trước khi thêm tunnel vận hành.

**Tiến độ 13/10/2026:** OpenVPN 2.4.12 đã cài trên host `10.3.55.213`; giao diện Settings → Kết nối → VPN tới Grafana đã có ô chọn `.ovpn`, IP Grafana `/32`, trạng thái và nút kết nối/ngắt. Host helper socket tách riêng khỏi Podman, chỉ admin được gọi qua Dashboard. Profile được giới hạn 256 KiB, cần chứng chỉ/khóa nhúng, không cho script/plugin/route tùy ý; host lưu file quyền `0600`. Đã test validator/helper và status socket. Chưa tải profile thực, chưa tạo route/tunnel, chưa có kiểm chứng tới Grafana hoặc datasource. Dashboard đang chạy bản preview cục bộ; bản phát hành CI cần được đồng bộ để thay thế preview trước khi coi là production-ready.

**Điều chỉnh theo yêu cầu operator 13/10/2026 (candidate, chưa deploy):** Bỏ ô IP Grafana và route `/32` khỏi luồng cấu hình; chỉ tải `.ovpn`, nhập username/mật khẩu+OTP khi cần và mở tunnel `cephvpn0`. `--route-nopull` giữ nguyên default route, DNS và route của mạng Ceph. Trạng thái `connected` chỉ xác nhận systemd unit còn hoạt động và tunnel interface tồn tại, **không** chứng minh truy cập Grafana. Bước route tới Grafana và kiểm chứng end-to-end vẫn là công việc riêng, cần network owner duyệt trước khi bật. Không coi tunnel-only là hoàn thành tích hợp metrics.

**Quyết định kiến trúc:**

- Chỉ triển khai VPN nếu network owner xác nhận direct allowlist/route không phù hợp hoặc yêu cầu private path. Không tự dựng VPN endpoint hay chọn peer thay cho owner mạng.
- Network owner cung cấp gateway/endpoint, protocol, peer identity, route/subnet Grafana private, DNS, MTU, NAT policy, source identity, port được phép, monitoring owner và quy trình thu hồi.
- Ưu tiên tunnel đã được tổ chức vận hành. Nếu chưa có chuẩn, WireGuard là ứng viên để đánh giá; dùng IPsec/OpenVPN nếu đó là hạ tầng hiện hữu. Chưa chốt protocol.
- Bắt buộc **split tunnel**; chỉ route đúng Grafana endpoint/private subnet được duyệt. Không thay default route `0.0.0.0/0`, không route Ceph networks, PostgreSQL, RabbitMQ hoặc SSH quản trị qua tunnel nếu chưa có yêu cầu riêng.
- Không hard-code `123.30.242.91` thành route dài hạn nếu dịch vụ có private endpoint/DNS động. Ưu tiên private DNS/endpoint và route do network owner xác nhận.
- Dashboard/Watcher chạy trong Podman. Phải chứng minh tunnel dùng được từ đúng container network/source path; host-only connectivity không đủ. Xác minh lại bridge/subnet thực tế trước khi đổi route/NAT.
- Private key chỉ lưu trong secret store hoặc file credential root-owned (`0600`); không đưa vào repo, Compose, `.env` chung, DB, logs hay ticket. Có expiry, rotation và revoke procedure.
- Tunnel được quản lý bởi systemd/network owner tách khỏi app containers; không cấp `NET_ADMIN` cho container và không thay DNS/routing trong image.

**Các bước VPN:**

1. [ ] Network owner xác nhận topology và luồng: `10.3.55.213` → VPN gateway → mạng Grafana → datasource; xác định return route và source IP mà Grafana nhìn thấy.
2. [ ] So sánh direct allowlist với VPN về blast radius, audit, HA và chi phí vận hành; ghi quyết định/owner. Chỉ chọn VPN khi có lý do rõ.
3. [ ] Lập route matrix allow/deny. Allow đúng Grafana service và port query; kiểm tra overlap với `10.20.1.0/24`, `10.3.52.0/22`, Podman bridges, DB/RabbitMQ/Ceph routes.
4. [ ] Diễn tập tunnel trên staging/non-production: handshake, DNS, MTU, route symmetry, reconnect, rekey, peer outage và kill switch.
5. [ ] Trước production change, ghi lại `ip route`, policy rules, DNS, firewall, interface addresses, connectivity và service health; xác nhận quyền truy cập console/out-of-band.
6. [ ] Cài cấu hình nhỏ nhất theo runbook đã duyệt; dùng systemd unit/secret permissions chặt, restart có backoff và health check peer. Không restart toàn bộ Ceph-AI chỉ để bật VPN.
7. [ ] Chứng minh split tunnel: Grafana đi qua tunnel; default Internet route, SSH quản trị, Ceph hosts, PostgreSQL `10.3.54.241` và RabbitMQ giữ nguyên đường đi.
8. [ ] Kiểm tra từ `dashboard-web` và `watcher` container theo đúng UID/network namespace: DNS, TCP, TLS hostname/certificate và API health. Chưa chạy query metric rộng ở bước này.
9. [ ] Inject lỗi tunnel drop/reconnect trong staging: connector phải backoff/circuit-break, không tự fallback sang đường direct chưa được duyệt, không treo Watcher hoặc làm chậm collector khác.
10. [ ] Canary có người theo dõi; lưu route diff, handshake evidence đã redact, packet-flow và service health trước/sau.

**Acceptance VPN:** tunnel ổn định theo SLO do network owner duyệt; API reachability đạt từ container; packet/route evidence xác nhận chỉ destination allowlisted qua tunnel; default route và dịch vụ Ceph/DB/RabbitMQ/SSH không suy giảm; TLS hợp lệ; không lộ key/token; rollback được diễn tập trên staging.

**Rollback/stop VPN:** dừng collector trước; disable unit/tunnel; gỡ đúng route/rule do VPN tạo; khôi phục DNS/route snapshot; xác minh SSH quản trị, Ceph, DB, RabbitMQ và app health; revoke key/peer nếu cần. Không dùng `ip route flush`, flush firewall, restart mạng toàn host hoặc bật full-tunnel để xử lý lỗi.

## 3. Kiến trúc và ranh giới dữ liệu cần chọn

Dashboard URL không phải endpoint metric ổn định. Cần chọn một trong hai đường sau sau khi hỏi Grafana owner:

### Lựa chọn A — truy vấn qua Grafana datasource API

- Dùng endpoint API query datasource phù hợp với phiên bản Grafana đang chạy, datasource UID cụ thể và service account token read-only/query-only.
- Ưu điểm: giữ datasource/network credential phía Grafana; phù hợp nếu Prometheus/VictoriaMetrics không được expose cho Ceph-AI.
- Tác động: request đi qua Grafana và tiếp tục gây tải query trên datasource; phải đo cả hai lớp.
- Không dùng cookie phiên đăng nhập người dùng, token cá nhân hoặc scrape HTML/dashboard JSON làm luồng metrics.

### Lựa chọn B — truy vấn trực tiếp backend metrics

- Chỉ chọn nếu owner cung cấp endpoint/API chính thức, TLS, auth read-only và network policy rõ ràng (ví dụ Prometheus-compatible API).
- Ưu điểm: giảm xử lý proxy ở Grafana.
- Lưu ý: **không loại bỏ tải query lên Prometheus/VictoriaMetrics**; phải đo backend tương tự.

### Contract dữ liệu

Trước khi ghi database, xác định rõ:

- Cluster ID cố định của CS-LAB; ánh xạ label `cluster`, `host`, `daemon_type`, OSD/pool theo topology thật. Không suy luận bằng vị trí trong danh sách.
- Metric name, đơn vị, type (counter/gauge/histogram), query, label allowlist, nguồn, scrape interval, timestamp UTC và semantics khi metric missing/reset.
- Quy tắc chống duplicate, out-of-order, stale, gap, cardinality tăng đột biến và data-quality classification.
- Dữ liệu tối thiểu cần lưu cho từng consumer. Ưu tiên aggregate/snapshot; không lưu nguyên mọi series/điểm chỉ vì API trả về.
- Quyết định dùng model hiện có chỉ khi semantics khớp. Nếu không, tạo contract/table riêng có provenance và retention; không nhét metric tùy ý vào `HostMetricSample` hoặc `VolumeMetric`.

## 4. Pha triển khai và gates

### Phase 0 — quyền truy cập, owner và baseline (chưa query production)

- [ ] Xác định Grafana owner, datasource owner, production change approver, giờ cao điểm và kênh rollback.
- [ ] Ghi Grafana version/edition, datasource type/UID, backend endpoint, auth mode, HA topology, retention, scrape interval, query timeout/concurrency hiện tại.
- [ ] Thu thập baseline trong cùng cửa sổ đo: Grafana p50/p95 API/query latency, request/error rate, CPU/RSS, queue/concurrency; backend query p95, CPU/RAM, active series, query samples/bytes; dashboard p95/load failures.
- [ ] Ghi node Ceph-AI CPU/RAM/disk/inode và PostgreSQL connection/CPU/RAM/disk/DB size/write latency/table bytes/WAL growth.
- [ ] Xuất metric inventory cần thiết; mỗi mục phải có consumer rõ ràng (UI/RCA/forecast), owner, query và lý do không dùng metric hiện có.
- [ ] Đặt ngưỡng stop cụ thể cùng Grafana owner trước pilot. Ngưỡng mặc định đề xuất để review: dừng nếu p95 dashboard/query tăng >10% so với baseline tương đương hoặc 5xx/timeouts tăng rõ rệt; owner có thể đặt ngưỡng chặt hơn.

**Gate 0:** không truy vấn production cho tới khi có owner, baseline, metric allowlist, ngưỡng stop và phê duyệt read-only.

### Phase 1 — network/API và quyền tối thiểu

- [ ] Network owner chọn direct allowlist/route hoặc VPN workstream §2.1. Nếu chọn VPN, hoàn tất VPN acceptance và xác nhận container path trước API query; không bật đồng thời hai đường nếu không có lý do/approval.
- [ ] Xác nhận TLS certificate/hostname hợp lệ; không tắt certificate verification.
- [ ] Tạo service identity riêng cho Ceph-AI, quyền chỉ query datasource/metrics cần thiết; token lưu trong secret store, có expiry/rotation/revoke procedure.
- [ ] Xác nhận service identity không thể sửa/xóa dashboard, datasource, alert rule, user hoặc folder.
- [ ] Thử health/metadata query nhỏ, sau đó một query allowlisted trong khoảng thời gian ngắn; redaction token/URL secrets khỏi log.
- [ ] Ghi lại request ID, endpoint class, duration và sample count; không ghi credentials hoặc payload production nhạy cảm.

**Gate 1:** query đọc thành công, quyền ghi bị từ chối, TLS hợp lệ, token rotation/revoke đã thử ở môi trường kiểm soát.

### Phase 2 — thiết kế quota và storage trước khi code ingest

- [ ] Chốt metric allowlist và query templates tĩnh; không cho model/LLM tự tạo arbitrary PromQL/SQL trên production.
- [ ] Giới hạn range, step/resolution, `maxDataPoints`, series/label count, bytes/response, timeout và số query đồng thời.
- [ ] Mặc định đề xuất cho canary: tối đa 1 request đồng thời, tối đa 3 query allowlisted mỗi chu kỳ, range chỉ đủ bao phủ vài scrape intervals, response cap 300 points/query; hiệu chỉnh sau khi biết scrape interval/query semantics.
- [ ] Rate-limit theo source/datasource/cluster; jitter lịch poll; cache kết quả; exponential backoff khi 429/5xx/timeout; circuit breaker và kill switch.
- [ ] Chỉ persist normalized sample/snapshot cần dùng. Ước tính rows/day và bytes/day từ pilot; đặt DB/table budget, retention tự động, rollup/downsampling và purge rehearsal trước khi mở rộng.
- [ ] Dự báo DB growth, WAL growth và replication/backup overhead; xác định owner của PostgreSQL host `10.3.54.241`.
- [ ] Tạo feature flag riêng `grafana_metrics_enabled`, mặc định OFF; consumer/AI flag riêng để ingest shadow không làm thay đổi cảnh báo/remediation.

**Gate 2:** có design review về quota, schema, retention, cost/storage forecast, privacy và rollback; không còn bảng append-only thiếu retention.

### Phase 3 — connector và kiểm thử offline

- [ ] Viết adapter tách biệt cho Grafana API hoặc Prometheus-compatible API; xác thực schema response và datasource errors.
- [ ] Unit test với fixture có metric rỗng, counter reset, NaN/Inf, label explosion, timestamps cũ/tương lai, duplicate, partial response, 401/403/429/5xx, timeout và TLS failure.
- [ ] Test limits: query không thể vượt time range/series/points/response cap; không nhận query tự do từ prompt.
- [ ] Test quyền chỉ đọc; test redact secrets trong exception/log/audit.
- [ ] Test restart/reconnect/cache expiration, circuit breaker, rate limit và flag OFF; flag OFF phải chặn request mới.
- [ ] So sánh giá trị adapter với panel/query chính thức cho cùng time range; giải thích mọi khác biệt về step, aggregation, timezone và missing data.
- [ ] Kiểm tra data-quality gate: stale/gap/insufficient coverage phải được đánh dấu, không chuyển thành giá trị 0 hoặc “healthy”.

**Gate 3:** offline tests pass, query budget enforcement chứng minh được và đối chiếu panel không còn sai lệch chưa giải thích.

### Phase 4 — production shadow canary có giới hạn

- [ ] Chạy ingest read-only với flag consumer OFF; không tạo Incident, Action, Telegram, forecast promotion hoặc remediation.
- [ ] Chọn một cụm và danh sách metric tối thiểu; một chu kỳ lấy mẫu được operator duyệt. Bắt đầu dưới quota Phase 2.
- [ ] Đo đồng thời baseline và canary: số query/min, p95/p99, points/series/bytes, Grafana CPU/RSS, datasource query latency/CPU/RAM, dashboard p95/errors, Ceph-AI CPU/RSS, PostgreSQL write latency/connections/storage/WAL.
- [ ] Bắt đầu ở thời lượng ngắn được duyệt; nếu ổn định mới kéo dài tối thiểu 24 giờ qua giờ cao điểm. Mở rộng lên 7 ngày chỉ sau khi owner duyệt và DB growth forecast đạt.
- [ ] Dừng ngay nếu vượt ngưỡng stop, query lặp bất thường, response/cardinality vượt cap, DB growth vượt budget, freshness không đạt hoặc dashboard người dùng bị suy giảm.
- [ ] Lưu báo cáo baseline/canary bất biến: code/image identity, config fingerprint đã redact, thời gian, query count/shape, p95, tải trước/sau, errors, storage delta, quyết định owner.

**Gate 4:** Grafana owner và Ceph-AI operator ký xác nhận impact nằm trong budget; không có incident/alert/remediation do shadow ingestion.

### Phase 5 — dùng trong Ceph-AI theo từng consumer

- [ ] UI chỉ hiển thị metric kèm source, collected_at, age/quality và trạng thái stale/error.
- [ ] RCA chỉ dùng metric sau khi xác minh label mapping với topology host/OSD/pool; giữ lại truy vấn/evidence IDs để truy nguyên.
- [ ] Forecast/anomaly chỉ chạy shadow trước; tính precision/recall/false-positive và so sánh baseline hiện tại, không bật active alert chỉ vì đã có dữ liệu.
- [ ] Từng consumer có flag độc lập, rollback độc lập và audit nguồn dữ liệu.
- [ ] Không cho metric Grafana một mình cấp quyền hành động risky/destructive; policy/approval hiện hành vẫn giữ nguyên.

**Gate 5:** mỗi consumer có acceptance riêng; không gộp “data fetch works” thành “AI diagnosis validated”.

### Phase 6 — vận hành, retention và scale

- [ ] Cảnh báo connector stale, query errors, rate-limit, circuit open, cardinality/response-cap hit, DB ingest lag và disk/DB growth.
- [ ] Dashboard vận hành cho metrics integration: freshness, sample coverage, query count/latency/error, bytes/points, cache hit, DB rows/bytes/day.
- [ ] Runbook pause/disable, token revoke/rotate, egress ACL rollback, datasource/API outage, restore DB và purge retention.
- [ ] Soak tối thiểu 7 ngày ở mức quota dự kiến; chứng minh không tăng trưởng storage vô hạn, không rò connection, không backlog và không làm dashboard production chậm vượt budget.
- [ ] Chỉ scale query rate/series/cluster theo change approval riêng và đo lại từng nấc.

## 5. Bộ chỉ số và acceptance criteria

### Tính đúng và hữu ích

- 100% metric allowlist có owner, unit, query, labels, scrape interval, consumer và data-quality policy.
- Với mẫu đối chiếu đã chọn, giá trị/aggregation khớp query nguồn trong tolerance được owner định nghĩa; mọi sai khác có nguyên nhân ghi nhận.
- Không biến `missing`, stale, timeout hoặc permission denied thành 0/healthy.
- Thời gian dữ liệu tối đa (`max_age`) và coverage tối thiểu được xác định cho từng consumer.

### Ảnh hưởng Grafana/datasource

- Query count/rate luôn nằm dưới quota đã duyệt; không arbitrary query, không unbounded range/cardinality.
- Trong canary/soak, dashboard p95 và datasource query p95 không vượt ngưỡng stop so với baseline cùng thời điểm; ngưỡng đề xuất ban đầu là regression 10%, phải được owner phê duyệt trước khi chạy.
- Không có tăng 5xx/timeout/429 kéo dài; không chiếm hết concurrency/query queue; nếu có thì kill switch dừng poll trong một chu kỳ.
- Có báo cáo định lượng p50/p95/p99, CPU/RSS, active queries, samples/bytes scanned và hiệu ứng dashboard.

### Ảnh hưởng Ceph-AI/PostgreSQL

- CPU/RSS, DB connection pool, write latency, WAL và bytes/day nằm trong budget đã duyệt.
- Có retention tự động và kiểm thử purge/rollup; dung lượng có dự báo ít nhất 30 ngày, không dùng disk free 14 GiB của app node làm proxy cho DB server.
- Restart/reconnect không tạo trùng sample hoặc backlog query; cắt kết nối không ảnh hưởng các collector/worker hiện có.

### An toàn

- Token read-only, có owner, expiry, rotation và revoke; không xuất hiện trong logs/artifacts.
- Kiểm chứng API không làm thay đổi dashboard/datasource/alert configuration.
- Tắt flag hoặc revoke token làm connector ngừng query; phần còn lại của Ceph-AI tiếp tục hoạt động.

## 6. Rủi ro và biện pháp

| Rủi ro | Hậu quả | Giảm thiểu / stop condition |
|---|---|---|
| Query broad/high cardinality | Tăng CPU/RAM và query queue của Prometheus; dashboard chậm | Query templates allowlist, label filters, points/series cap, rate limit, owner-approved stop threshold |
| Polling quá dày hoặc nhiều replica | Tạo query storm và tải lặp | Single-leader/lease cho collector, jitter, cache, giới hạn query global theo datasource |
| Mất nhãn cluster/host hoặc label đổi | Gán metric sai node/cluster, RCA sai | Contract/version label mapping, topology reconciliation, quarantine unknown labels |
| Chỉ đo Grafana nhưng bỏ backend | Bỏ sót tải thực lên Prometheus/VictoriaMetrics | Đo cả Grafana lẫn datasource; owner cung cấp backend telemetry |
| PostgreSQL/table append-only tăng liên tục | Tăng WAL/backup/DB storage | Ước lượng trước, rollup, retention, byte budget, purge test và alert DB growth |
| Service account quá quyền | Thay đổi dashboard/data source hoặc lộ dữ liệu khác | Credential riêng read-only, scope theo datasource/org nếu hỗ trợ, revoke test |
| Gắn nhãn sai metric như khỏe mạnh | False negative/alert đóng nhầm | Quality states, shadow first, không auto-resolve/action; golden query set |
| Outbound path chưa thông | Connector lỗi lặp, log noise | Owner xử lý ACL cho egress IP `222.255.27.220`; retry có backoff/circuit breaker |

## 7. Rollback/disable plan

1. Tắt `grafana_metrics_enabled` trước; xác minh request count về 0 trong tối đa một chu kỳ.
2. Tắt consumer flags RCA/forecast/UI nếu vấn đề nằm ở downstream, nhưng giữ audit evidence.
3. Revoke service token; nếu cần thu hồi network allowlist, phối hợp owner để không ảnh hưởng người dùng Grafana.
4. Giữ DB samples để điều tra cho tới khi owner duyệt purge; chỉ purge bằng retention procedure có backup/row-count evidence.
5. Nếu DB/schema thay đổi, rollback theo migration/backup policy đã duyệt; không xóa cột/table đang có consumer khác.
6. Ghi thời gian stop, lý do, query cuối, dashboard/backend impact và người phê duyệt.

## 8. Phụ thuộc và câu hỏi cần owner trả lời

- [ ] Dashboard này dùng datasource nào, datasource UID/type/version là gì?
- [ ] Backend thật là Prometheus, Thanos, Mimir, VictoriaMetrics hay loại khác? Endpoint query chính thức nào được phép dùng?
- [ ] Scrape interval, retention, series cardinality, HA/query frontend và quota hiện tại là bao nhiêu?
- [ ] Metric/label nào được phép rời monitoring network và được persist trong DB Ceph-AI?
- [ ] Service account read-only do ai cấp/quản lý/rotate? Có thể giới hạn theo datasource/folder/org không?
- [ ] Egress ACL của `222.255.27.220` tới Grafana/API đã được phép chưa? Ai sở hữu firewall phía đích và gateway `10.3.52.1`?
- [ ] Grafana owner chấp nhận regression budget nào cho p95, CPU/RAM, query queue và error rate?
- [ ] DB owner chấp nhận retention/rollup/storage budget nào cho metrics mới?
- [ ] Pilot có thể chạy trong cửa sổ nào và ai có quyền yêu cầu dừng ngay?

## 9. Thứ tự ưu tiên và trạng thái

| Ưu tiên | Công việc | Trạng thái |
|---|---|---|
| P0 | Xác nhận owner, datasource/backend, query budget và baseline; chưa query production | Chưa làm |
| P0 | Mở đúng egress ACL, xác thực TLS và cấp query-only service identity | Đang chờ network/Grafana owner |
| P1 | Chốt contract/allowlist/quota/schema/retention và storage forecast | Chưa làm |
| P1 | Adapter read-only + unit/security/limit tests + đối chiếu query panel | Chưa làm |
| P1 | Shadow canary production, đo impact đồng thời trên Grafana/backend/Ceph-AI/PostgreSQL | Chưa làm |
| P2 | Cho UI/RCA/forecast dùng lần lượt, từng feature flag và acceptance riêng | Chưa làm |
| P2 | Soak 7 ngày, retention/rollback/runbook và scale approval | Chưa làm |

**Điều kiện chuyển `Plan/completed`:** có API/data-source query thành công từ runtime Ceph-AI; production shadow/soak evidence được lưu; impact nằm trong budget được owner duyệt; security/retention/rollback gates đóng; từng consumer có kiểm chứng riêng. Chỉ có code hoặc test mock không đủ.

## 10. Evidence log

- 13/10/2026 — `openvpn-2.4.12-2.el8` và `pkcs11-helper` đã cài bằng DNF trên `10.3.55.213`; `ceph-ai-grafana-vpn-control.socket` hoạt động, status trả `installed=true`, `configured=false`, `state=disconnected`. Không có profile/route/tunnel được bật.
- 13/10/2026 — 17 test helper/restart liên quan qua; Dashboard preview cục bộ và API/status socket hoạt động. Preview chưa nằm trong image digest CI đã duyệt, nên sẽ mất khi stack khởi động lại bằng digest cũ.
- 13/10/2026 — code review tại server `/root/ceph-ai`: chưa thấy Grafana connector; thấy node metric SSH collectors và Loki HTTP API collector.
- 13/10/2026 — cấu hình database được truy xuất theo cách chỉ in backend/hostname: PostgreSQL tại `10.3.54.241`; CS-LAB row counts được đọc từ database.
- 13/10/2026 — từ host `10.3.55.213`, DNS resolve `metrics.vccloud.vn` thành `123.30.242.91`; TCP SYN tới port 3000 rời `eth1` nhưng không nhận response; host OUTPUT policy accept và firewalld inactive. Kết luận chỉ ra ngoài-host/network/destination ACL cần điều tra; chưa chứng minh chính xác thiết bị nào drop.
- 13/10/2026 — từ local client URL trả HTTP 200 sau redirect tới `/login`; chỉ chứng minh trang đăng nhập truy cập được, không xác thực dashboard data/API.
- Chưa có query Grafana/Prometheus nào được chạy từ Ceph-AI; không có số đo tải production hoặc kết luận capacity.
