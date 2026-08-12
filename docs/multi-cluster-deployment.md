# Giám sát nhiều cụm Ceph

**2026-08-07 — CẬP NHẬT: đã có multi-cluster observability (Phase 1) ngay
trong 1 instance.** Trang **Clusters** (`/clusters`, admin) cho phép thêm
cụm Ceph thứ 2/3/... để **quan sát** — Watcher tự chạy thêm 1 vòng lặp giám
sát riêng cho mỗi cụm (health check + Incident feed + heartbeat), Dashboard
có bộ chọn cụm ngay trên trang chính (`/`, góc phải thanh nav, chỉ hiện khi
có ≥ 2 cụm). Đây là cách **khuyến nghị** để xem nhiều cụm cùng lúc — không
cần dựng thêm server/checkout nào nữa cho nhu cầu quan sát thuần tuý.

**Giới hạn quan trọng của Phase 1** (đọc kỹ trước khi thêm cụm thứ 2):
chỉ phần **quan sát** (health/Incident feed) là multi-cluster. Chẩn đoán AI,
Telegram (Duyệt/Từ chối) **vẫn chỉ áp dụng cho cụm mặc định** (cụm cấu hình
qua `.env`/trang Cài đặt) — Worker chủ động **bỏ qua** (không chạy lệnh SSH
nào) đối với Incident đến từ cụm khác, đây là biện pháp an toàn bắt buộc
(SSH creds/lệnh của Worker chỉ đúng cho cụm mặc định). Các cụm thêm qua
`/clusters` chỉ nhận được: health status, Incident feed (không có log SSH
đính kèm — `log_excerpt` luôn rỗng), heartbeat. Không có: remediation tự
động, backup, patch, upgrade, phê duyệt Telegram cho các cụm này.

Chi tiết kiến trúc/thiết kế: `shared/models.py::Cluster`'s docstring,
`shared/clusters.py`, `watcher/main.py::run_observed_cluster_loop`,
`worker/main.py::_handle_message`'s cluster-scope guard.

**2026-08-11 — sửa lỗi cụm đã chọn bị "quên" khi điều hướng:** trước bản vá
này, chọn cụm 2 trên Dashboard rồi bấm sang trang khác (Volumes, Cluster
Metrics...) và quay lại Dashboard sẽ âm thầm rơi về cụm mặc định — bộ chọn
cụm chỉ là query param `?cluster=` trên `/`, không có gì lưu lại lựa chọn,
và không link/nav nào trong app forward tham số đó. Đã sửa bằng cách lưu
`selected_cluster_id` vào session (cùng cơ chế session đăng nhập sẵn có,
`dashboard/app.py`'s `SessionMiddleware`) — xem
`dashboard/routes/incidents.py::_resolve_selected_cluster`. Quay lại `/`
không kèm `?cluster=` giờ tiếp tục đúng cụm đã chọn lần gần nhất thay vì
rơi về mặc định; `?cluster=` tường minh trên URL vẫn luôn thắng session
(bookmark cũ vẫn hoạt động y hệt trước).

Lưu ý: bản vá này **chỉ** khắc phục việc bộ chọn cụm trên Dashboard bị mất
khi điều hướng. Nó KHÔNG làm cho Volumes/Cluster Metrics/Backup/... trở
nên multi-cluster — các trang đó vẫn luôn hiển thị dữ liệu của cụm mặc
định bất kể Dashboard đang chọn cụm nào, đúng giới hạn Phase 1 đã mô tả ở
trên.

Nếu cần MỌI tính năng (remediation/backup/patch/upgrade/Telegram) đa cụm —
chưa làm, đây là phase sau, khối lượng lớn hơn nhiều (xem phần cuối tài
liệu này). Phần còn lại của tài liệu (dưới đây) mô tả hướng **thay thế**:
chạy nhiều instance riêng biệt, mỗi instance 1 cụm — vẫn hợp lệ nếu bạn cần
MỌI tính năng đa cụm ngay (mỗi instance có Worker/backup/patch/upgrade đầy
đủ cho cụm của riêng nó), đổi lại phải tự vận hành N bộ service/DB/RabbitMQ
riêng thay vì 1 bộ chọn cụm trong 1 Dashboard.

---

## (Thay thế) Chạy nhiều instance riêng, mỗi instance 1 cụm

ceph-aiops **cốt lõi vẫn** thiết kế 1 instance quản lý đúng 1 cụm Ceph theo
Phase 1 ở trên), không phải giới hạn UI mà nằm ở tầng kiến trúc:

- `config/settings.py` là một `Settings()` singleton nạp từ **1 file
  `.env`** — `ceph_mon_nodes`, SSH key, exec mode, RBD pools... chỉ có 1 bộ
  giá trị cho toàn app.
- `shared/models.py` — không bảng nào (`Incident`, `Action`, `Backup`,
  `CrushSnapshot`, `NodeMetric`...) có cột `cluster_id`. Mọi dữ liệu ngầm
  định thuộc về "cụm hiện tại".
- Watcher/Worker/Dashboard là 3 tiến trình chạy vòng lặp trên đúng cụm đó,
  không có khái niệm chọn cụm trong UI.
- `dashboard/routes/deploy_cluster.py`, `delete_cluster.py`,
  `convert_cluster.py`, `restore_cluster.py` dùng để **triển khai/thay thế
  cụm mà instance này đang quản lý** — không phải multi-cluster monitoring.

Biến toàn bộ này thành multi-tenant thật (1 instance giám sát N cụm, có bộ
chọn cụm trên UI) đòi hỏi thêm `cluster_id` vào hơn chục bảng DB, viết lại
mọi route/monitor/SSH-exec/RabbitMQ routing/worker loop để lọc theo cụm —
một khối lượng việc rất lớn, rủi ro cao cho một app đang chạy production.

**Hướng đã chọn (2026-08-07): chạy nhiều instance riêng biệt, mỗi instance
1 cụm.** Đây là hướng chi phí thấp, rủi ro thấp, và một phần đã có sẵn
trong kiến trúc — field `cluster_name` (trang **Alert Telegram**, xem
[telegram-alerts.md](telegram-alerts.md) mục "Tên cụm") đã tồn tại chính là
để nhiều instance cùng gửi cảnh báo vào 1 chat Telegram mà vẫn phân biệt
được cụm nào báo.

## Mỗi instance cần những gì riêng

| Thứ cần tách | Cấu hình ở đâu | Bắt buộc? |
|---|---|---|
| Thư mục checkout riêng | `git clone` vào 1 thư mục mới, ví dụ `ceph-aiops-clusterb` cạnh `ceph-aiops` | Bắt buộc nếu chạy chung 1 server |
| `.env` riêng | Copy `.env.example`, điền `CEPH_MON_NODES`/SSH key/... của cụm B | Luôn bắt buộc |
| Database riêng | `DATABASE_URL` trỏ file SQLite khác, ví dụ `sqlite:///./ceph_aiops_clusterb.db` | Luôn bắt buộc |
| RabbitMQ namespace riêng | `RABBITMQ_URL` dùng **vhost khác** cho mỗi cụm, ví dụ `amqp://guest:guest@localhost/clusterb` — hàng đợi `incidents` (`shared/mq.py::QUEUE_NAME`) là tên cố định, dùng chung 1 vhost sẽ khiến 2 instance tranh nhau đọc cùng 1 hàng đợi | Bắt buộc nếu chạy chung 1 RabbitMQ broker |
| Port Dashboard riêng | `scripts/deploy/deploy.local.env` (server-local, không commit) — set `DASHBOARD_PORT=8001` cho instance thứ 2 | Bắt buộc nếu chạy chung 1 server |
| Tên cụm hiển thị trên Telegram | Trang **Alert Telegram** → "Tên cụm" (hoặc `CLUSTER_NAME` trong `.env`) | Khuyến nghị nếu nhiều cụm gửi chung 1 chat |

Nếu mỗi cụm chạy trên **server riêng** (mô hình phổ biến nhất — mỗi cụm
Ceph một máy giám sát cạnh nó) thì chỉ cần `.env` riêng + `cluster_name`
riêng là đủ, không cụm nào đụng port/DB/RabbitMQ của cụm khác vì chúng ở 2
máy khác nhau — làm theo README.md bình thường trên mỗi máy.

## `scripts/deploy/restart_services.sh` đã an toàn cho nhiều instance/1 máy

Trước đây script này dùng `pkill -f "python -m watcher.main"` — chạy 2
checkout trên cùng máy thì restart 1 checkout sẽ **giết nhầm** tiến trình
của checkout kia (cùng câu lệnh, khác thư mục, `pkill -f` không phân biệt
được), và cả 2 ghi đè lên cùng file log `/var/log/ceph-aiops-watcher.log`.

Đã sửa (2026-08-07): script giờ dùng đường dẫn tuyệt đối tới interpreter
trong `.venv` của chính checkout đó (`$REPO_DIR/.venv/bin/python`) để lọc
`pkill`, và đặt tên file log theo tên thư mục checkout
(`/var/log/<tên-thư-mục>-watcher.log`). Với checkout hiện tại tên
`ceph-aiops`, đường dẫn log giữ nguyên y hệt trước — không đổi gì cho
deployment đang chạy. Một checkout thứ 2 đặt tên `ceph-aiops-clusterb` sẽ
tự động có log riêng (`ceph-aiops-clusterb-watcher.log`...) và không bao
giờ kill nhầm tiến trình của checkout đầu, không cần cấu hình gì thêm.

Vẫn cần tự đặt `DASHBOARD_PORT` khác nhau qua
`deploy.local.env` của từng checkout — script không tự đoán được port
trống.

## Nút "Khởi động lại Worker/Watcher" trên trang Settings cũng đã sửa

Phát hiện thêm khi rà kiến trúc cho tài liệu này: `dashboard/routes/
settings.py` (nút "Khởi động lại Worker"/"Khởi động lại Watcher" trên
Settings, chạy MỖI KHI lưu cấu hình cụm/API AI) trước đây tìm tiến trình
bằng `pgrep -f "-m\s+worker\.main"` — pattern này khớp với **bất kỳ**
checkout nào trên máy, không riêng gì checkout đang chạy trang Settings đó.
Nếu 2 instance của app chạy chung 1 server, bấm nút này ở Dashboard của cụm
A có thể tìm nhầm và giết/khởi động lại Worker của cụm B.

Đã sửa (2026-08-07): pattern giờ có thêm đường dẫn tuyệt đối tới
`sys.executable` (interpreter `.venv` của chính checkout đó) ở đầu, nên chỉ
khớp tiến trình do đúng checkout này khởi chạy — tương tự cách
`restart_services.sh` được sửa ở trên. Đường dẫn log hiển thị trên Settings
(`WORKER_LOG_PATH`/`WATCHER_LOG_PATH`/`DASHBOARD_LOG_PATH`) cũng đổi theo
tên thư mục checkout, đồng bộ với `LOG_TAG` trong `restart_services.sh`.

## CI/CD tự động deploy nhiều cụm (chưa làm — cân nhắc khi cần)

`.github/workflows/ci-cd.yml` hiện chỉ deploy tới **1** đích (secrets
`DEPLOY_HOST`/`DEPLOY_PATH` ở cấp repo). Chưa đổi phần này trong đợt này vì
sửa CI/CD ảnh hưởng tới pipeline đang chạy thật — nếu cần tự động deploy
nhiều cụm, cách chuẩn khi sẵn sàng là dùng **GitHub Environments**: tạo 1
Environment/cụm (mỗi Environment có bộ secret `DEPLOY_HOST`/`DEPLOY_PATH`
riêng), rồi matrix hoá job `deploy` trong `ci-cd.yml` chạy trên danh sách
Environment đó — thêm 1 cụm mới sau này chỉ là thêm 1 Environment, không
cần sửa YAML. Nói với tôi khi muốn triển khai phần này.
