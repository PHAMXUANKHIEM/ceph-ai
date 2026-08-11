# Chuyển đổi cụm Ceph từ ceph-deploy (systemd) sang cephadm

Tài liệu này mô tả tính năng **Convert to Cephadm** trong ceph-aiops — chuyển
một cụm Ceph đang chạy `ceph-deploy`/gói cài truyền thống (daemon chạy trực
tiếp qua systemd) sang mô hình quản lý `cephadm` (daemon chạy trong
container, do orchestrator quản lý), **tại chỗ, không tạo lại cụm, không
đụng tới dữ liệu OSD**.

Nguồn: `dashboard/routes/convert_cluster.py`,
`worker/executor/cluster_deploy.py` (phần "Chuyển đổi cụm systemd ->
cephadm"), `worker/executor/commands.py`, `worker/policy/action_policy.yaml`.

## 1. Tại sao cần tính năng này

ceph-aiops hỗ trợ 2 kiểu triển khai cụm:

- **cephadm** (`CEPH_EXEC_MODE=cephadm`) — daemon chạy trong container, có
  orchestrator (`ceph orch ...`).
- **ceph-deploy / gói truyền thống** (`CEPH_EXEC_MODE=none`) — daemon chạy
  trực tiếp qua systemd (`ceph-mon@<id>.service`, ...), không có
  orchestrator.

Nhiều tính năng khác trong ceph-aiops (nâng cấp qua `ceph orch upgrade`,
patch, ...) chỉ chạy tốt/đơn giản trên cụm kiểu `cephadm`. Convert to
Cephadm cho phép nâng cấp một cụm `ceph-deploy` cũ lên mô hình `cephadm`
mà không cần build lại từ đầu.

## 2. Phạm vi và giới hạn (đọc trước khi dùng)

- **Chỉ MON/MGR/OSD.** Không xử lý RGW/MDS — nếu cụm có node RGW, daemon RGW
  vẫn tiếp tục chạy dưới systemd như cũ sau khi chuyển đổi xong, và sẽ
  **không** xuất hiện trong `ceph orch ps`.
- **Một chiều duy nhất: systemd → cephadm.** Không có chiều ngược lại
  (cephadm → systemd) — Ceph không có lệnh chính thức nào hỗ trợ điều này,
  và việc này đã bị loại khỏi phạm vi tính năng theo quyết định của vận
  hành viên khi yêu cầu tính năng này (xem comment trong
  `action_policy.yaml`).
- **Chỉ nhận cụm `CEPH_EXEC_MODE=none`.** Cụm chạy `docker`/`podman` (daemon
  chạy container thủ công, không phải cephadm) có layout khác, `cephadm
  adopt --style legacy` không được thiết kế cho kiểu đó — không đủ điều kiện
  chuyển đổi. Cụm đã ở `cephadm` thì coi như đã chuyển đổi rồi.
- **CHƯA được kiểm chứng trực tiếp trên một cụm systemd thật** trong phiên
  làm việc xây dựng tính năng này (khác với hầu hết pha khác trong
  `cluster_deploy.py`, vốn đã được sửa lỗi qua nhiều lần chạy thật). Đây là
  bản triển khai lần đầu theo đúng quy trình `cephadm adopt` chính thức của
  Ceph — **nên thử trên một cụm không quan trọng trước khi dùng cho
  production**.
- Luôn được phân loại **RISKY** (`action_policy.yaml`), luôn yêu cầu **duyệt
  thủ công** — không có ngoại lệ tự động duyệt.

## 3. Điều kiện để bắt đầu

Trang `/convert-cluster` chỉ cho phép đề xuất khi:

1. `CEPH_EXEC_MODE=none` (cụm ceph-deploy/gói truyền thống thuần).
2. Có ít nhất cấu hình sẵn node trong ceph-aiops (`configured_nodes()` khác
   rỗng).
3. Có ít nhất 1 node MON và 1 node MGR trong cấu hình.
4. Cụm phải xác định được **một phiên bản Ceph duy nhất đang chạy** (query
   trực tiếp cụm qua `ceph_client.summarize_cluster_versions`) — phiên bản
   này được dùng để chọn image cephadm sẽ dùng khi adopt, **không cho vận
   hành viên tự chọn tay** (tránh mismatch âm thầm giữa phiên bản đang chạy
   thật và phiên bản được ghi nhận).
   - Ngoại lệ: nếu cụm đang ở trạng thái **chuyển đổi dở dang** trước đó
     (MON/MGR đã adopt, OSD thì chưa) khiến `ceph versions` báo nhiều phiên
     bản, hệ thống vẫn cho phép tiếp tục **miễn là bản thân các MON đồng ý
     với nhau về đúng một phiên bản** (MON được adopt đầu tiên trong quy
     trình, nên phiên bản của MON chính là phiên bản đích cả cụm cần hội tụ
     về). Chỉ từ chối hẳn khi ngay cả các MON cũng không thống nhất phiên
     bản với nhau.
5. Không có đề xuất dựng/xoá/chuyển đổi cụm nào khác đang chờ duyệt hoặc đã
   duyệt (khoá lẫn nhau giữa các action thuộc nhóm
   `cluster_deploy_action_ids`).

## 4. Luồng thao tác trên Dashboard

1. Vận hành viên vào `/convert-cluster` → xem bản tóm tắt cụm hiện tại
   (danh sách MON/MGR/OSD) và bấm **"Đề xuất chuyển đổi"**.
2. Hệ thống tạo một `Incident` (mã `CONVERT_CLUSTER_TO_CEPHADM`) +
   `Action` (`action_id=convert_cluster_to_cephadm`, trạng thái
   `PENDING_APPROVAL`), kèm bản kế hoạch chi tiết (xem mục 5) và lệnh xem
   trước.
3. Vận hành viên gõ đúng địa chỉ IP của node MON đầu tiên (`confirm_text`)
   để xác nhận, rồi bấm **Duyệt**. Dashboard chỉ đổi `Action.status` sang
   `APPROVED` — **không tự thực thi gì cả**.
4. Worker (`worker/llm/router_client.py::poll_approved_actions`) phát hiện
   Action `APPROVED`, gọi `worker/executor/cluster_deploy.run()` để chạy
   tuần tự các pha bên dưới qua SSH tới từng node.
5. Trang `/convert-cluster/progress` cho phép theo dõi tiến độ real-time
   theo từng pha, từng host.
   ngay lập tức, không chạy tiếp.

## 5. Chi tiết 10 pha thực thi

Thứ tự pha bám sát đúng quy trình adoption chính thức của Ceph: kiểm tra sức
khoẻ → cài cephadm khắp nơi → adopt MON → adopt MGR → bật orchestrator (cần
MGR đã adopt và đang chạy) → phân phối khoá SSH của orchestrator → đăng ký
host → adopt OSD (cuối cùng, chỉ sau khi orchestrator + host inventory đã
sẵn sàng) → verify cuối.

| # | Bước (`step_key`) | Mô tả | % |
|---|---|---|---|
| 1 | `ssh_check` | Kiểm tra SSH tới từng node + lấy hostname (dùng cho nhãn `ceph orch host add` sau này) | 5 |
| 2 | `health_precheck` | `ceph -s` — dừng lại nếu cụm đang `HEALTH_ERR` | 10 |
| 3 | `install_cephadm` | Cài binary `cephadm` trên **mọi** node (nếu chưa có) | 25 |
| 4 | `adopt_mons` | `cephadm adopt --style legacy` cho từng MON | 40 |
| 5 | `adopt_mgrs` | `cephadm adopt --style legacy` cho từng MGR | 50 |
| 6 | `enable_orchestrator` | Bật module `cephadm`, chuyển orchestrator backend, sinh khoá SSH cephadm | 60 |
| 7 | `distribute_ssh_key` | Phân phối khoá công khai của cephadm tới `authorized_keys` mọi node (kể cả MON đầu tiên) | 70 |
| 8 | `register_hosts` | `ceph orch host add <hostname> <ip>` cho mọi node | 80 |
| 9 | `adopt_osds` | `cephadm adopt --style legacy` cho từng OSD (theo từng host) | 95 |
| 10 | `verify` | `ceph -s` + kiểm tra thực tế từng daemon đã được cephadm quản lý chưa | 100 |

### 5.1. Bước 1 — Kiểm tra SSH + hostname

```bash
true                              # xác nhận SSH tới được
hostname -f 2>/dev/null || hostname
```

### 5.2. Bước 2 — Kiểm tra sức khoẻ trước khi chuyển đổi

Dùng chung logic `ceph -s` với các action khác — nếu `HEALTH_ERR` thì dừng
lại, không chuyển đổi cụm đang có sự cố.

### 5.3. Bước 3 — Cài `cephadm` trên mọi node

```bash
command -v cephadm >/dev/null 2>&1 || \
  (curl -fsSL https://download.ceph.com/rpm-<codename>/el9/noarch/cephadm \
    -o /usr/local/bin/cephadm && chmod +x /usr/local/bin/cephadm)
```

`<codename>` được suy ra từ phiên bản Ceph phát hiện được ở bước đề xuất
(vd. `18.2.x` → `reef`). Dùng script Python độc lập tải qua `curl` (giống
cách `deploy_cluster_cephadm` cài `cephadm` cho `first_mon`) thay vì cài qua
package manager của OS — vì package `cephadm` trên `download.ceph.com` không
chắc có sẵn đúng bản cho mọi hệ điều hành/node. **Không** tự cài
docker/podman — giả định container runtime đã có sẵn trên node (cephadm tự
nó yêu cầu điều này).

### 5.4. Bước 4–5 — Adopt MON, rồi MGR

Với mỗi MON/MGR, hệ thống:

1. Kiểm tra `cephadm ls --no-detail` trên host đó — nếu daemon loại này đã
   có `style == "cephadm:v1"` thì coi như **đã chuyển đổi từ trước**, bỏ
   qua (giúp thao tác **resumable**, xem mục 6).
2. Nếu chưa, dò systemd unit thật đang chạy (`ceph-mon@*` /
   `ceph-mgr@*`) để lấy đúng `<id>` Ceph đang biết tới daemon đó — **không**
   giả định `<id>` bằng `hostname` của node (đúng với cụm do chính
   ceph-aiops dựng bằng ceph-deploy, nhưng không chắc đúng với một cụm
   systemd có sẵn từ trước mà ceph-aiops chưa từng dựng).
3. Chạy:

```bash
cephadm --image quay.io/ceph/ceph:v<version> adopt --style legacy \
  --name mon.<id>          # hoặc mgr.<id>
```

`--image` luôn được **ghim rõ ràng** theo đúng phiên bản đang chạy thật
(`action_params["version"]`), **không để cephadm tự chọn mặc định** — nếu
không ghim, cephadm sẽ lấy bản build MỚI NHẤT được gắn tag cho codename đó
trên `quay.io`, có thể mới hơn bản đang chạy thật trên các daemon chưa
adopt. Một lần chuyển đổi thật đã gặp đúng lỗi này: MON/MGR bị adopt lên
`17.2.8` trong khi OSD vẫn còn native ở `17.2.5`, khiến `ceph versions` bị
lệch vĩnh viễn cho tới khi nâng cấp thủ công OSD.

### 5.5. Bước 6 — Bật orchestrator cephadm

Chạy trên MON đầu tiên:

```bash
ceph mgr module enable cephadm --force
ceph orch set backend cephadm
ceph cephadm generate-key || true
```

`--force` là do chính Ceph khuyến nghị khi gặp lỗi
`Error ENOENT: all mgr daemons do not support module 'cephadm'` — đây là
race điều kiện đã biết (cache năng lực module của MON chưa kịp cập nhật
ngay sau khi MGR vừa được adopt/khởi động lại). An toàn ở đây vì hệ thống
vừa tự adopt chính MGR này, biết chắc nó là thật.

`ceph cephadm generate-key` đảm bảo tồn tại cặp khoá SSH riêng của
orchestrator — khi `cephadm bootstrap` (dựng cụm mới) việc này tự động xảy
ra, nhưng adoption không bao giờ gọi `bootstrap`, nên bước này thay thế
tương đương.

### 5.6. Bước 7 — Phân phối khoá SSH của cephadm

Lấy khoá công khai bằng `ceph cephadm get-pub-key` rồi thêm vào
`~/.ssh/authorized_keys` của **mọi** node, **kể cả MON đầu tiên**:

```bash
mkdir -p /root/.ssh && chmod 700 /root/.ssh
grep -qxF '<pubkey>' /root/.ssh/authorized_keys 2>/dev/null || \
  echo '<pubkey>' >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
```

Lưu ý: khác với dựng cụm mới (`cephadm bootstrap` tự cấp quyền cho chính nó
trên host của nó), adoption **không** tự làm việc này — nếu bỏ qua MON đầu
tiên, bước 8 (`ceph orch host add`, chạy TRONG container orchestrator trên
MON đầu tiên) sẽ SSH ngược lại chính host đó và thất bại với
`Permission denied`.

### 5.7. Bước 8 — Đăng ký host với orchestrator

```bash
ceph orch host add <hostname> <ip>
```

Chạy cho **mọi** node kể cả MON đầu tiên (bootstrap tự đăng ký host của
chính nó, nhưng adoption thì không). `<hostname>` chỉ là nhãn hiển thị của
orchestrator (`ceph orch host ls`), không liên quan tới `<id>` daemon
mon/mgr ở bước 4–5.

### 5.8. Bước 9 — Adopt OSD

Với mỗi node có role OSD:

1. `ceph-volume lvm list --format json` — key ở cấp cao nhất của JSON trả
   về chính là các OSD id thật sự có mặt trên host đó (không cần đối chiếu
   ngược với `ceph osd tree`).
2. Loại trừ những id đã `cephadm ls` báo là `style == "cephadm:v1"` từ trước
   (resumable, giống bước 4–5).
3. Với từng id còn lại:

```bash
cephadm --image quay.io/ceph/ceph:v<version> adopt --style legacy \
  --name osd.<id>
```

OSD luôn được adopt **sau cùng** (đúng thứ tự Ceph khuyến nghị), và hệ
thống **không bao giờ tự bịa/tái gán OSD id** — chỉ adopt đúng những id mà
`ceph-volume` thật sự báo cáo có trên host.

### 5.9. Bước 10 — Verify sau chuyển đổi

Ngoài `ceph -s` (giống bước 2), hệ thống còn tự kiểm tra **độc lập** với dữ
liệu trạng thái các bước trước:

- Mọi node có role MON: `cephadm ls` phải thấy ít nhất 1 `mon.*` với
  `style=cephadm:v1`.
- Mọi node có role MGR: tương tự với `mgr.*`.
- Mọi node có role OSD: mọi id trả về từ `ceph-volume lvm list` phải nằm
  trong tập `cephadm ls` báo `style=cephadm:v1`.

Nếu bất kỳ điều kiện nào sai, action **thất bại rõ ràng** ở bước này — cố
tình không tin tưởng mù quáng vào trạng thái "done" của các bước trước
(xem mục 6, lỗi `legacy` vs `cephadm:v1` đã từng gặp thật).

## 6. Idempotent / resumable — chạy lại an toàn khi thất bại giữa chừng

Toàn bộ tính năng được thiết kế để **chạy lại được** nếu một lần chuyển đổi
thất bại giữa chừng (vd. lỗi ở bước 6 sau khi MON+MGR đã adopt xong):

- Mỗi lần adopt (MON/MGR/OSD) đều kiểm tra `cephadm ls --no-detail` trước —
  daemon nào đã có `style=cephadm:v1` thì bỏ qua, không adopt lại.
- Việc dò systemd unit (`_discover_systemd_daemon_id`) chỉ chạy khi daemon
  **chưa** được cephadm quản lý — vì sau khi adopt, unit systemd gốc không
  còn tồn tại nữa (đã bị đổi tên trong quá trình adopt), dò lại sẽ ra
  "không tìm thấy unit" một cách gây hiểu lầm.
- Nếu vận hành viên tự tay hoàn tất các bước còn lại rồi chạy lại tính năng,
  các bước đã xong sẽ được nhận diện đúng là "đã chuyển đổi từ trước" thay
  vì báo lỗi.

**Lưu ý quan trọng khi diễn giải "cephadm ls":** lệnh này liệt kê **mọi**
daemon Ceph phát hiện được trên host, kể cả daemon **chưa** được cephadm
quản lý (daemon systemd thuần sẽ hiện `"style": "legacy"` cùng tên thật
`"name": "osd.<id>"`). Chỉ `style == "cephadm:v1"` mới tính là **đã** adopt
— đây là một lỗi thật đã từng xảy ra trong production (xem mục 8).

## 7. Sau khi hoàn tất

Nếu toàn bộ 10 bước `status=done`:

- `CEPH_EXEC_MODE` trong file `.env` được tự động ghi thành `cephadm` (danh
  sách node MON/MGR/OSD giữ nguyên — chuyển đổi không thêm/bớt node nào).
- Nếu việc ghi file `.env` thất bại (hiếm), action **vẫn được coi là
  THÀNH CÔNG** — vì bản thân cụm đã chuyển đổi xong và khoẻ mạnh
  (verify đã pass); vận hành viên có thể vào trang **Cài đặt** để cập nhật
  `CEPH_EXEC_MODE` bằng tay.

## 8. Các lỗi thật đã gặp và cách đã khắc phục (lịch sử sửa lỗi, 2026-07-28)

Ghi lại từ comment mã nguồn — hữu ích khi debug một lần chuyển đổi thất bại
thật:

1. **`cephadm ls --format json` không hợp lệ.** `cephadm ls` không nhận cờ
   `--format json` (luôn tự in JSON) — cờ sai khiến mọi lệnh gọi thất bại,
   và code cũ nuốt lỗi này thành "chưa adopt gì cả" một cách âm thầm, khiến
   toàn bộ cơ chế resumable mất tác dụng. Đã sửa: dùng `--no-detail`.
2. **Đánh giá sai daemon đã adopt qua `name` thay vì `style`.** OSD chưa
   adopt vẫn xuất hiện trong `cephadm ls` với đúng tên `osd.<id>` (chỉ khác
   ở `style=legacy`) — nếu chỉ so khớp theo tên, hệ thống coi nhầm là "đã
   chuyển đổi" và bỏ qua luôn bước adopt thật, trong khi `ceph versions`
   vẫn báo lẫn phiên bản. Đã sửa: chỉ tính `style == "cephadm:v1"`.
3. **Không ghim `--image` khi adopt.** Không ghim khiến cephadm tự chọn
   bản build mới nhất trên `quay.io` cho codename đó — có thể mới hơn bản
   thật đang chạy trên các daemon chưa adopt, làm `ceph versions` lệch
   vĩnh viễn. Đã sửa: luôn `--image quay.io/ceph/ceph:v<version-đang-chạy>`.
4. **`ceph mgr module enable cephadm` (không cờ) thất bại ngay sau
   `adopt_mgrs`** với `Error ENOENT: all mgr daemons do not support module
   'cephadm', pass --force to force enablement` — race điều kiện đã biết
   giữa cache năng lực module của MON và MGR vừa được adopt/khởi động lại.
   Đã sửa: thêm `--force` (đúng như thông báo lỗi của Ceph khuyến nghị).
5. **Bỏ sót phân phối khoá SSH cho chính MON đầu tiên.** Giả định sai rằng
   `cephadm bootstrap` tự cấp quyền cho chính host của nó — đúng với
   bootstrap, nhưng **adoption không bao giờ chạy bootstrap**, nên MON đầu
   tiên chưa từng có khoá cephadm trong `authorized_keys` của chính nó.
   Bước `ceph orch host add` (chạy TRONG container orchestrator, cần SSH
   ngược ra mọi host kể cả chính MON đầu tiên) thất bại với
   `Error EINVAL: Failed to connect to <mon> ... Permission denied`. Đã
   sửa: phân phối khoá cho **mọi** node, không loại trừ MON đầu tiên.
6. **Bước verify tin tưởng mù quáng trạng thái "done" của các bước
   trước.** Lỗi #2 ở trên từng khiến MỌI bước adopt báo "đã chuyển đổi từ
   trước" (dù thực tế còn native), toàn bộ action hoàn tất với status=done
   trên mọi bước — và vì `run()` sau đó **luôn** ghi
   `CEPH_EXEC_MODE=cephadm` ngay khi hết các bước mà không kiểm tra lại,
   cấu hình sai này bị ghi vào `.env` dù cụm thực chưa hoàn tất chuyển đổi.
   Hậu quả: mọi lần thử "Convert to Cephadm" SAU đó bị chặn ngay từ đầu với
   lý do "cụm hiện tại đã chạy cephadm rồi", dù một phần daemon vẫn còn
   native — vận hành viên phải vào **Cài đặt** tự tay đổi lại
   `CEPH_EXEC_MODE=none` mới thử lại được. Đã sửa: bước `verify` giờ tự
   truy vấn lại thực tế từng daemon (không dựa vào trạng thái các bước
   trước) ngay trước khi `.env` được ghi, để một lỗi tương tự trong tương
   lai thất bại ngay tại đây thay vì âm thầm ghi cấu hình sai.

## 9. Các file liên quan trong mã nguồn

| File | Vai trò |
|---|---|
| `dashboard/routes/convert_cluster.py` | Route Dashboard: trang, đề xuất (`propose`), theo dõi tiến độ (`progress`) |
| `dashboard/templates/convert_cluster.html` | Giao diện: tóm tắt cụm, xác nhận gõ IP MON, theo dõi tiến độ theo pha/host |
| `worker/executor/cluster_deploy.py` | Toàn bộ logic 10 pha + bảng `_PHASES_BY_ACTION_ID["convert_cluster_to_cephadm"]` + `run()` |
| `worker/executor/commands.py` | `_convert_cluster_to_cephadm_preview_command` — sinh câu lệnh xem trước hiển thị trước khi duyệt |
| `worker/policy/action_policy.yaml` | Xếp `convert_cluster_to_cephadm` vào nhóm `cluster_deploy_action_ids`, luôn RISKY, luôn cần duyệt |
| `dashboard/routes/actions.py` | Endpoint Duyệt/Từ chối chung cho mọi Action (Dashboard chỉ đổi status, Worker mới thực thi) |
| `tests/test_dashboard_convert_cluster.py`, `tests/test_cluster_deploy.py` | Test cho route và cho từng pha (bao gồm các test hồi quy ứng với mục 8) |
