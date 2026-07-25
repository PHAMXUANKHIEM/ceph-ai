# Ceph AIOps

Giám sát cụm Ceph tự động, chẩn đoán nguyên nhân bằng AI (qua một kết nối
API AI — Claude, Codex/OpenAI, OpenRouter, hoặc 9router tự triển khai), tự
động khắc phục sự cố an toàn (Safe Action) hoặc đề xuất chờ duyệt (Risky
Action), kèm Dashboard quản trị có Chat-with-AI để tra cứu và quản lý
cluster (tạo/xoá pool, bật/tắt OSD, ...).

Tài liệu này hướng dẫn chạy toàn bộ hệ thống trên **một máy mới** (không
phải máy đang chạy sẵn) — ví dụ khi chuyển sang server khác hoặc set up
môi trường dev.

## Kiến trúc tổng quan

Ba tiến trình độc lập, cùng đọc/ghi một database (SQLite mặc định):

| Tiến trình | Vai trò |
|---|---|
| `watcher` | Poll cụm Ceph qua SSH mỗi `WATCHER_POLL_INTERVAL_SECONDS` giây, phát hiện `HEALTH_WARN`/`HEALTH_ERR`, ghi Incident + publish lên RabbitMQ |
| `worker` | Tiêu thụ Incident từ RabbitMQ, gọi AI chẩn đoán, phân loại Safe/Risky, tự thực thi (Safe) hoặc chờ duyệt (Risky) — **đây là tiến trình DUY NHẤT giữ SSH credential và thực thi lệnh trên cụm** |
| `dashboard` | Web UI (FastAPI) — xem Incident, duyệt/từ chối Risky Action, chat với AI, xem log node, cấu hình |

Dashboard **không bao giờ** thực thi lệnh trực tiếp lên cụm — chỉ đổi
trạng thái trong DB; Worker mới là nơi thực sự SSH vào cụm.

## 1. Yêu cầu hệ thống

- Python **3.11+**
- RabbitMQ (broker cho hàng đợi Incident giữa Watcher và Worker)
- Một cặp SSH keypair (không passphrase) có quyền SSH vào các node MON/OSD
  của cụm Ceph cần giám sát
- Cụm Ceph đã deploy sẵn (hỗ trợ cephadm, docker/podman exec, hoặc cài đặt
  package thuần — xem `CEPH_EXEC_MODE` bên dưới)
- (Tuỳ chọn) Một API key cho tính năng chẩn đoán AI / Chat-with-AI — Claude
  (Anthropic), Codex (OpenAI), OpenRouter, hoặc một endpoint 9router tự
  triển khai (proxy OpenAI-compatible); chọn loại kết nối ở trang Cài đặt

### Cài RabbitMQ nhanh bằng Docker

```bash
docker run -d --name rabbitmq -p 5672:5672 -p 15672:15672 rabbitmq:3.13-management
```

## 2. Clone code

```bash
git clone git@github.com:PHAMXUANKHIEM/ceph-ai.git
cd ceph-ai
```

## 3. Tạo virtualenv và cài dependency

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## 4. Cấu hình `.env`

Tạo file `.env` ở thư mục gốc (đã có sẵn trong `.gitignore`, không bao giờ
commit file này). Toàn bộ biến đọc từ `config/settings.py`:

```dotenv
# --- Database & message queue ---
DATABASE_URL=sqlite:///./ceph_aiops.db
RABBITMQ_URL=amqp://guest:guest@localhost/

# --- Đăng nhập Dashboard (BẮT BUỘC đổi trước khi public ra ngoài) ---
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD_HASH=<xem cách tạo bên dưới>
SESSION_SECRET_KEY=<chuỗi random dài, xem cách tạo bên dưới>

# --- SSH tới cụm Ceph (dùng chung cho Watcher lẫn Worker) ---
SSH_KEY_PATH=/root/.ssh/ceph_lab_watcher
SSH_USER=root
CEPH_MON_NODES=10.20.1.150,10.20.1.249,10.20.1.253
CEPH_MON_HOSTNAMES=mon1,mon2,mon3
CEPH_EXEC_MODE=cephadm
CEPH_CONTAINER_NAME=
WATCHER_POLL_INTERVAL_SECONDS=15

# --- Tuỳ chọn: node OSD/MGR/RGW (để lấy log/CLI riêng, có thể để trống) ---
CEPH_OSD_NODES=
CEPH_OSD_CONTAINER_NAME=
CEPH_MGR_NODES=
CEPH_RGW_NODES=
CEPH_RGW_CONTAINER_NAME=

# --- Worker ---
WORKER_MAX_RETRIES=3
WORKER_APPROVAL_POLL_INTERVAL_SECONDS=5

# --- API AI (Claude/Codex/OpenRouter/9router) — để trống nếu chưa dùng tính năng AI ---
ROUTER_PROVIDER=9router
ROUTER_API_KEY=
ROUTER_BASE_URL=
ROUTER_MODEL=
ROUTER_ENABLED=false
```

Giải thích các mục quan trọng:

- **`CEPH_EXEC_MODE`** — cách chạy lệnh `ceph ...` trên node:
  - `cephadm` — cụm deploy bằng cephadm (khuyến nghị nếu dùng cephadm/reef trở lên), không cần `CEPH_CONTAINER_NAME`
  - `docker` / `podman` — cụm chạy container thủ công với tên container cố định, cần set `CEPH_CONTAINER_NAME`
  - `none` — `ceph` binary cài thẳng trên host (package install)
- **SSH key** — tạo riêng một keypair không passphrase cho service này, và
  deploy public key vào `authorized_keys` của **tất cả** node MON/OSD cần
  SSH tới:
  ```bash
  ssh-keygen -t ed25519 -f ~/.ssh/ceph_lab_watcher -N "" -C "ceph-aiops"
  ssh-copy-id -i ~/.ssh/ceph_lab_watcher.pub root@<mon-node-ip>
  ```
- **`DASHBOARD_PASSWORD_HASH`** — hash bcrypt của mật khẩu đăng nhập, tạo bằng:
  ```bash
  python -c "import bcrypt; print(bcrypt.hashpw(b'MAT_KHAU_THAT', bcrypt.gensalt()).decode())"
  ```
- **`SESSION_SECRET_KEY`** — chuỗi random bất kỳ, tạo bằng:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
  Nếu không set, hệ thống vẫn chạy được nhưng sẽ log cảnh báo dùng giá trị
  dev mặc định — **không an toàn nếu Dashboard public ra internet**.

## 5. Khởi tạo database

```bash
alembic upgrade head
```

File SQLite (`ceph_aiops.db`, theo `DATABASE_URL`) sẽ được tạo tự động ở
lần chạy đầu.

## 6. Chạy 3 tiến trình

Mở 3 terminal (hoặc dùng `nohup ... & disown` để chạy nền), đều từ thư mục
gốc repo với venv đã activate:

```bash
# Terminal 1 — Watcher (polling + phát hiện incident)
python -m watcher.main

# Terminal 2 — Worker (chẩn đoán AI + thực thi remediation)
python -m worker.main

# Terminal 3 — Dashboard (web UI)
python -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8000
```

Chạy nền có log ra file (giống cách server hiện tại đang chạy):

```bash
nohup python -m watcher.main >> /var/log/ceph-aiops-watcher.log 2>&1 & disown
nohup python -m worker.main  >> /var/log/ceph-aiops-worker.log  2>&1 & disown
nohup python -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8000 \
  >> /var/log/ceph-aiops-dashboard.log 2>&1 & disown
```

Truy cập Dashboard tại `http://<ip-máy>:8000`, đăng nhập bằng
`DASHBOARD_USERNAME`/mật khẩu đã tạo hash ở bước 4.

> Repo không dùng systemd unit — cả 3 tiến trình đều là background process
> thuần. Muốn restart, `pkill -f "python -m watcher.main"` (tương tự cho
> `worker.main` / `uvicorn dashboard.app`) rồi chạy lại lệnh `nohup` ở
> trên. `scripts/deploy/restart_services.sh` đã đóng gói sẵn toàn bộ quy
> trình này (pull code mới nhất + migrate + restart) — dùng lại được cho
> máy mới nếu muốn.

## 7. Cấu hình cụm Ceph / AI qua Dashboard (thay vì `.env`)

Sau khi đăng nhập, vào trang **Cài đặt** để cấu hình/chỉnh lại kết nối cụm
Ceph và API AI mà không cần SSH vào server:

- Lưu cấu hình **cụm Ceph** (form "cluster") → ghi thẳng vào `.env` **và tự
  động khởi động lại tiến trình Watcher** (Worker/Dashboard không bị ảnh
  hưởng).
- Lưu cấu hình **API AI** (chọn loại kết nối — Claude/Codex/OpenRouter/
  9router — rồi nhập API key/model) → ghi thẳng vào `.env` **và tự động
  khởi động lại tiến trình Worker**.
- Nút **"Khởi động lại Dashboard"** riêng ở cuối trang Cài đặt → tự restart
  chính tiến trình Dashboard (cần thiết vì nó không thể tự restart giữa
  chừng một request như Worker/Watcher).

Nói cách khác: sau lần chạy tay ban đầu (bước 6), **hầu hết các thay đổi
cấu hình sau này không cần SSH/`nohup` thủ công nữa** — chỉ cần vào Cài đặt
và lưu. Bước 6 vẫn cần thiết cho lần khởi động đầu tiên trên máy mới, và
cho các thay đổi CODE (không phải cấu hình) — khi đó dùng lại
`scripts/deploy/restart_services.sh` hoặc lệnh `nohup` thủ công.

## 8. Kiểm tra hoạt động

```bash
pytest
```

Mặc định loại trừ nhóm test `live` (gọi SSH/API thật ra cụm Ceph/API AI
thật — chỉ chạy tay khi có sẵn cụm lab thật để test):

```bash
pytest -m live   # chỉ chạy khi thật sự có cụm/API AI để test
```

## 9. CI/CD (tuỳ chọn)

Repo có sẵn `.github/workflows/ci-cd.yml`: tự động chạy test trên mọi
push/PR, và tự động SSH vào server để deploy lại khi push lên `main`. Xem
`scripts/deploy/README.md` để set up SSH deploy key + GitHub Secrets cho
máy chủ mới.

## Xử lý sự cố thường gặp

- **`Không kết nối được database — đã chạy alembic upgrade head chưa?`**
  trên Dashboard → chưa chạy bước 5.
- **Watcher báo "mất kết nối cụm"** → kiểm tra `SSH_KEY_PATH` đã deploy
  đúng public key lên node MON, và `CEPH_MON_NODES`/`SSH_USER` đúng.
- **`xóa pool` báo lỗi `EPERM: pool deletion is disabled`** → cụm Ceph mặc
  định chặn xoá pool; tính năng `delete_pool` của Chat-with-AI tự bật/tắt
  `mon_allow_pool_delete` quanh lệnh xoá, không cần tự cấu hình tay.
- **Chat-with-AI báo "Chưa kết nối API AI"** → chưa cấu hình
  `ROUTER_API_KEY`/`ROUTER_BASE_URL`/`ROUTER_MODEL` (qua `.env` hoặc trang
  Cài đặt — chọn loại kết nối Claude/Codex/OpenRouter/9router rồi nhập key).
