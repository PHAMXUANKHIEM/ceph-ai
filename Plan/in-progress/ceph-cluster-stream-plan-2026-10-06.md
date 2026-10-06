# Kế hoạch: luồng dịch vụ cụm Ceph trên trang Stream

**Ngày lập:** 06/10/2026
**Trạng thái:** `IN-PROGRESS` — CS0–CS3 xong trên nhánh `ceph-stream`; CS4 (địa chỉ daemon, heartbeat chậm) xong trên `ceph-stream-cs4`; còn client thật (`ceph auth ls`, chờ operator) và CS5 (deploy).
**Liên quan:** trang `/stream` (luồng cấu hình hệ thống Ceph AI, `ceph-health-dashboard/src/components/InstallationStream.tsx`), `Plan/in-progress/architecture-impact-analysis-regression-plan.md`.
**Mục tiêu:** bên cạnh luồng của hệ thống Ceph AI, trang Stream có thêm một luồng tương tự cho **chính cụm Ceph**: các dịch vụ (MON, MGR, OSD, PG/pool, RGW, MDS), các host, **đường mạng** (public / cluster network) và các client, kèm trạng thái sống, để operator nhìn một chỗ thấy dữ liệu đi qua đâu và chỗ nào đang hỏng.

---

## 0. Nguyên tắc bắt buộc

- **Chỉ đọc, không SSH khi mở trang.** Trang chỉ đọc snapshot watcher đã lưu (`shared/cluster_snapshot.py`); không chạy lệnh Ceph theo request của trình duyệt. Thu thập thêm (nếu cần) nằm trong collector định kỳ, có circuit breaker và giới hạn concurrency sẵn có.
- **Không lộ bí mật:** không keyring, không token, không mật khẩu; địa chỉ IP nội bộ chỉ hiện cho admin (trang đã admin-only).
- **Không đoán:** dữ liệu thiếu hoặc snapshot cũ phải hiện là "chưa rõ"/"cũ (thời điểm)", không vẽ như đang khỏe. Mạng public/cluster lấy từ cấu hình Ceph, không suy từ dải IP.
- **Không làm hỏng luồng hiện có:** luồng hệ thống Ceph AI phải giữ nguyên giao diện và hành vi (kéo thả, zoom, minimap, bố cục đã lưu trong trình duyệt).
- **Theo cụm đang chọn** (`?cluster=`), giống mọi trang khác.

## 1. Hiện trạng (khảo sát 06/10/2026 trên CS-LAB)

Dữ liệu **đã có** trong snapshot (không cần SSH thêm):

| Section | Nội dung dùng được | Độ mới |
|---|---|---|
| `status` (`ceph -s` JSON) | health + checks (kể cả muted), MON quorum (`quorum_names`, `monmap.num_mons`), `osdmap` (num/up/in), `pgmap` (PG theo trạng thái, pool, object, dung lượng, I/O), `mgrmap` (active, standby, module, service dashboard/prometheus), `servicemap` (RGW daemon + host), `fsmap` (MDS) | ~1 phút |
| `nodes` | host, tên host, vai trò (MON/OSD/RGW), **mọi địa chỉ của host** (CS-LAB: mỗi host 2 địa chỉ `10.3.x` và `10.20.1.x`) | chu kỳ inventory |
| `crush` | root → host → OSD, rule, dung lượng/PG theo root | chu kỳ inventory (payload có thể bị cắt) |

Dữ liệu **chưa có**:

- `public_network` / `cluster_network` của Ceph: repo chỉ có ở form deploy/restore, chưa thu thập từ cụm đang chạy.
- Địa chỉ từng daemon (MON v1/v2, OSD public/cluster addr): `status` không có; cần `ceph mon dump` / `ceph osd dump` nếu muốn vẽ đúng daemon nào nghe trên mạng nào.
- Danh sách client (OpenStack Cinder/Glance, Kubernetes CSI RBD, S3 user): chỉ tình cờ thấy tên `client.*` trong health check AUTH_INSECURE; chưa có nguồn chính thức.

Component hiện tại: một file React ~580 dòng; phần vẽ (canvas, kéo thả, định tuyến cạnh tránh node, minimap, panel chi tiết) dùng chung được, nhưng nhãn nhóm, bộ lọc ("luồng lõi / tích hợp"), legend và phần ánh xạ kiến trúc đang gắn cứng cho luồng hệ thống.

## 2. Thiết kế đồ thị cụm Ceph

**Nhóm và node** (một node mỗi dịch vụ; host/mạng là node riêng):

| Nhóm | Node | Thông tin chính / trạng thái |
|---|---|---|
| CLIENT | Ceph AI (watcher/worker, qua SSH chỉ-đọc / khóa mutation) · OpenStack (RBD) nếu đã cấu hình · S3 client nếu có RGW | cấu hình đã lưu |
| CONTROL PLANE | MON quorum · MGR (active/standby, module) · MDS nếu `fsmap` có | quorum n/n, leader; MGR available |
| GATEWAY | RGW (số daemon, host) | daemon trong servicemap |
| DATA | OSD (up/in) · PG & pool (active+clean %, degraded) · Dung lượng | theo `osdmap`/`pgmap` |
| HOST & MẠNG | mỗi host (vai trò, địa chỉ) · Public network · Cluster network | host đủ vai trò; mạng theo cấu hình Ceph |

**Cạnh (luồng):** client → MON "lấy cluster map"; client → OSD "đọc/ghi dữ liệu (public network)"; S3 client → RGW → OSD "object"; MGR ↔ MON "trạng thái / module"; OSD ↔ OSD "replication / recovery (cluster network)"; host → mạng "gắn vào"; Ceph AI → MON "ceph status"; Ceph AI → host "SSH".

**Trạng thái màu** (mới cho luồng Ceph): `ok` / `warn` / `error` / `unknown` lấy từ health check và số liệu (ví dụ OSD down → OSD `error`; PG degraded → PG `warn`; snapshot cũ → mọi node `unknown` + nhãn thời điểm). Cạnh qua thành phần lỗi đổi kiểu nét. Panel chi tiết: số liệu, health check liên quan (mã + thông điệp), link tới trang sẵn có (`/nodes`, `/pools`, `/crush-map`, `/alerts`).

## 3. Gói việc

### CS0 — Thu cấu hình mạng Ceph (watcher)
- [x] `watcher/inventory_queries.collect_network_config` + section `nodes.networks` (`518f2ac1`). Giá trị thật CS-LAB: `public_network = 10.20.1.0/24`, `cluster_network` trống; `10.3.x` chỉ là mạng quản trị — đoán theo IP sẽ sai.
- [x] Gắn địa chỉ host vào mạng theo CIDR (trong read model CS1); cluster_network trống ⇒ "replication đi chung public network".
- [x] Test (`tests/test_ceph_network_config.py`): CIDR hợp lệ/nhiều giá trị/IPv6/rác bị loại, lệnh lỗi không làm mất section.

### CS1 — Read model và API
- [x] `shared/ceph_topology.py` (`81fcd836`): health snapshot (detail, bỏ check đã mute) + `status` + `nodes` + `crush` + `pools`; client suy từ ứng dụng của pool (inventory pool nay giữ `applications`); `OSD_SLOW_PING_TIME_FRONT/BACK` tô mạng public/cluster.
- [x] `/stream` nhúng `ceph_topology`; `GET /api/stream/ceph-topology` (admin).
- [x] Test (`tests/test_ceph_topology.py`, 9): khỏe, OSD down chỉ đỏ đúng host, PG degraded + mất quorum, mạng chậm, check mute, snapshot cũ/thiếu ⇒ unknown, client theo ứng dụng pool, không lộ secret, admin-only.

### CS2 — Tách phần vẽ dùng chung (frontend)
- [x] `StreamCanvas.tsx` tách nguyên phần vẽ; tham số: nhóm, cạnh, bố cục mặc định, khóa lưu, nhãn trạng thái, bộ lọc, legend, loại cạnh, vị trí nhãn nhóm, nhãn link, phần chi tiết bổ sung (`d6dd45be`).
- [x] `InstallationStream` dùng `StreamCanvas`, cùng khóa bố cục `ceph-ai:installation-stream:layout:v2`.
- [x] Hồi quy: render Chromium headless trên dữ liệu thật — tab hệ thống vẫn 16 node / 22 cạnh / 3 nhóm (3·4·9), không lỗi JS; 836 test liên quan pass.

### CS3 — Luồng cụm Ceph
- [x] `CephClusterStream.tsx` + `StreamPage.tsx` (tab, `#ceph` trong URL). Bố cục **trái → phải theo cột** (client → control plane → gateway/data → host → mạng) — bản đầu xếp theo hàng bị thu còn 46% và chỉ chiếm 1/3 khung nên đã đổi.
- [x] Màu `ok/warn/error/unknown`, tóm tắt health/quorum/OSD/PG/dung lượng/mạng, cảnh báo snapshot cũ, health check liên quan trong panel chi tiết, link "Mở trang".
- [x] Tự làm mới 30 giây, dừng khi tab ẩn.
- [x] Build Node 20; render Chromium headless: tab Ceph 13 node / 21 cạnh / 5 nhóm, không lỗi JS, không tràn ở 1440 và 390 px.

### CS4 — Mở rộng
- [x] Địa chỉ từng daemon: section `nodes.daemons` từ `ceph mon dump` + `ceph osd dump` **gộp một lần SSH** (`collect_daemon_addresses`), chỉ giữ tên, địa chỉ v1/v2 (bỏ nonce, địa chỉ không đúng dạng bị loại), up/in; lỗi lệnh không làm mất section. CS-LAB: ~1,9 KB/snapshot.
  - Host: từng `mon.X` và `osd.N` kèm địa chỉ public/cluster; MON: địa chỉ từng MON, đánh dấu MON ngoài quorum; OSD: danh sách OSD out.
  - OSD down: health (1 phút) vẫn là nguồn chính; `osd dump` (chu kỳ inventory) chỉ bổ sung tên OSD khi detail của health bị cắt, để dump cũ không vẽ đỏ OSD đã lên lại.
  - Mạng: số daemon lắng nghe, cạnh MON → public ghi port (`:3300, :6789`); địa chỉ daemon nằm ngoài `public_network`/`cluster_network` ⇒ mạng đó `warn` kèm danh sách địa chỉ.
- [ ] (chờ operator: `ceph auth ls` trả cả key) Client thực tế từ `ceph auth ls` (chỉ tên entity, không key) và session RBD/RGW nếu có nguồn chỉ-đọc phù hợp.
- [x] Heartbeat chậm (`OSD_SLOW_PING_TIME_FRONT/BACK`): mạng tương ứng `warn`, chi tiết liệt kê tối đa 5 cặp OSD chậm (+ số còn lại).

### CS6 — Tab "Luồng AI" (operator yêu cầu 06/10/2026)
- [x] `shared/ai_flow.py` + `GET /api/stream/ai-flow` (admin): 16 bước phát hiện → incident → thu bằng chứng → chẩn đoán → policy/preflight → duyệt/autopilot → thực thi → xác minh → Case Memory/Trust/online learning/Failure Lab, kèm số liệu 24 giờ từ DB và báo cáo Failure Lab mới nhất; bước có số liệu xấu tô `warn`/`error`. Chỉ đọc, không chạy lệnh Ceph; lỗi đọc số liệu không làm hỏng trang (tab bị khóa).
- [x] `AiFlowStream.tsx` (tái dùng `StreamCanvas`, bố cục theo cột), tab thứ ba `#ai`, tự làm mới 30 giây. Render Chromium trên số liệu thật: 16 bước, 18 cạnh, không lỗi JS, không tràn ngang ở 1440/390 px.
- Số liệu thật ngày 06/10 đã chỉ ra: thu bằng chứng = 0 dù có 49 incident; 5/7 lần thực thi lỗi; 30 action chờ duyệt; 1 case đã xác minh; Failure Lab 2/8.

### CS5 — Kiểm thử và phát hành
- [ ] Chạy thử read model trên dữ liệu production trước khi báo xong.
- [ ] Browser acceptance: tab Cụm Ceph hiện đủ nhóm, không lỗi JS, không tràn ngang ở 390/1280/1920 px.
- [ ] Full test, quality gate (so `origin/main`), budget, CI xanh rồi deploy.

## 4. Rủi ro và cách giảm

| Rủi ro | Giảm thiểu |
|---|---|
| Tách component làm hỏng luồng hệ thống đang dùng | CS2 tách riêng, không đổi giao diện; test + browser check trước khi thêm luồng Ceph |
| Snapshot cũ khiến sơ đồ "trông khỏe" | Trạng thái `unknown` + thời điểm snapshot khi quá hạn |
| Cụm lớn (nhiều host/OSD) làm sơ đồ rối | Gom OSD theo host; bố cục tự động theo cột; thu gọn nhóm |
| Thêm lệnh Ceph vào collector | 2 lệnh `config get` + `mon dump`/`osd dump` (một lần SSH), đều chỉ-đọc, cùng chu kỳ inventory, lỗi không chặn section |
| Lộ địa chỉ nội bộ | Trang admin-only; không đưa keyring/token |

## 5. Định nghĩa hoàn thành

- Tab "Cụm Ceph" trên `/stream` hiển thị đúng dịch vụ, host, public/cluster network và client đã cấu hình của cụm đang chọn, với trạng thái khớp `ceph -s` của cùng thời điểm.
- Luồng hệ thống Ceph AI không đổi giao diện; test và browser check cũ vẫn đạt.
- Không có lệnh Ceph nào chạy theo request của trình duyệt.
- Full test, quality gate, budget, CI xanh; deploy và kiểm tra trên production.

## 6. Quyết định của operator (06/10/2026)

1. OSD **gom theo host** (danh sách OSD và OSD down nằm trong chi tiết host).
2. **CS4 để sau.**
3. Tự làm mới **30 giây**.

## 7. Nhật ký thực hiện

| Ngày | Hạng mục | Kết quả | Bằng chứng | Trạng thái |
|---|---|---|---|---|
| 06/10/2026 | Khảo sát | Snapshot `status`/`nodes`/`crush` đủ cho dịch vụ và host; thiếu public/cluster network và địa chỉ daemon | khảo sát CS-LAB | Done |
| 06/10/2026 | CS0–CS3 | Thu mạng Ceph, read model + API, tách StreamCanvas, tab Cụm Ceph | `518f2ac1`, `81fcd836`, `d6dd45be`; render Chromium | Done (chờ CS5) |
| 06/10/2026 | CS4 | Địa chỉ daemon, OSD out, địa chỉ ngoài mạng cấu hình, cặp OSD heartbeat chậm | nhánh `ceph-stream-cs4`; 26 test; chạy read model trên snapshot + dump thật CS-LAB (11 node / 18 cạnh vì bản lưu không có section pools, MON :3300/:6789, mỗi OSD public+cluster trên 10.20.1.x) | Done (chờ CS5) |
