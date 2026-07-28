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

Hướng dẫn dưới đây viết cho một máy **chưa cài gì cả** (Ubuntu/Debian sạch)
— làm theo đúng thứ tự, mỗi bước đều có lệnh kiểm tra để biết đã đúng chưa
trước khi sang bước tiếp theo.

## 1. Yêu cầu hệ thống

Cần có trên **máy chạy ứng dụng này** (không phải node Ceph — node Ceph là
máy khác, không cần cài gì cả, chỉ cần cho phép SSH vào là đủ):

- Python **3.11+**
- Git
- RabbitMQ (broker cho hàng đợi Incident giữa Watcher và Worker)

Không bắt buộc phải có sẵn cụm Ceph để cài xong ứng dụng — ứng dụng khởi
động và chạy được ngay cả khi chưa cấu hình cụm nào; phần "cụm Ceph để
giám sát" cấu hình ở bước 6, có thể làm sau và sửa lại bất cứ lúc nào qua
trang Cài đặt.

### 1.1. Cài gói hệ thống (Ubuntu/Debian)

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv git
```

Kiểm tra lại:

```bash
python3.11 --version   # phải in ra Python 3.11.x trở lên
git --version
```

> Máy dùng bản Linux khác (RHEL/CentOS/Rocky)? Cài `python3.11`, `git` bằng
> trình quản lý gói tương ứng (`dnf install python3.11 git`) — phần còn lại
> của hướng dẫn giống hệt nhau.

### 1.2. Cài RabbitMQ

Cách nhanh nhất — chạy bằng Docker (không cần cài Docker sẵn thì xem
[hướng dẫn cài Docker chính thức](https://docs.docker.com/engine/install/)):

```bash
docker run -d --name rabbitmq --restart unless-stopped \
  -p 5672:5672 -p 15672:15672 rabbitmq:3.13-management
```

Không dùng Docker thì cài trực tiếp:

```bash
sudo apt install -y rabbitmq-server
sudo systemctl enable --now rabbitmq-server
```

Kiểm tra RabbitMQ đã chạy (dù cách nào ở trên):

```bash
curl -s -u guest:guest http://localhost:15672/api/overview | head -c 100
```

Có in ra JSON (không phải "Connection refused") là RabbitMQ đã sẵn sàng.

## 2. Clone code

```bash
git clone git@github.com:PHAMXUANKHIEM/ceph-ai.git
cd ceph-ai
```

(Nếu chưa có SSH key trên GitHub thì dùng link HTTPS thay thế:
`git clone https://github.com/PHAMXUANKHIEM/ceph-ai.git`)

## 3. Tạo virtualenv và cài dependency

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .[dev]
```

Kiểm tra:

```bash
python -c "import fastapi, sqlalchemy, paramiko; print('OK — cài đủ dependency')"
```

> **Lỗi `ensurepip is not available`** khi chạy `python3.11 -m venv`? Thiếu
> gói `python3.11-venv` — quay lại bước 1.1 cài lại.
>
> Từ giờ về sau, **mọi lệnh `python`/`pip`/`alembic`/`pytest` trong hướng
> dẫn này đều giả định venv đã activate** (`source .venv/bin/activate`,
> dấu nhắc dòng lệnh có tiền tố `(.venv)`). Mở terminal mới thì phải
> activate lại — quên bước này là nguyên nhân phổ biến nhất của lỗi
> "ModuleNotFoundError" khi chạy lệnh.

## 4. Tạo file `.env`

```bash
cp .env.example .env
```

`.env` đã nằm sẵn trong `.gitignore` — không bao giờ commit file này (chứa
mật khẩu/API key thật). `.env.example` đã liệt kê đủ mọi biến ứng dụng đọc
từ `config/settings.py`, để trống là được — hầu hết đều tuỳ chọn, cấu hình
sau qua trang Cài đặt cũng được, không bắt buộc điền hết ngay từ đầu.

Riêng phần **đăng nhập Dashboard** thì bắt buộc điền trước khi chạy thật
(mục dưới) — mọi mục còn lại (cụm Ceph, API AI) có thể để trống, làm ở
bước 6/7 hoặc sau khi đã đăng nhập vào Dashboard.

## 5. Tạo mật khẩu đăng nhập Dashboard

Mở `.env` vừa tạo, điền 2 dòng sau (bỏ trống thì ứng dụng vẫn chạy được
nhưng dùng mật khẩu mặc định `admin`/`admin` — **chỉ chấp nhận được khi
test trên localhost**, không bao giờ để vậy nếu Dashboard mở ra mạng
ngoài):

```bash
# Sinh hash bcrypt cho mật khẩu thật của bạn — thay MAT_KHAU_THAT
python -c "import bcrypt; print(bcrypt.hashpw(b'MAT_KHAU_THAT', bcrypt.gensalt()).decode())"

# Sinh session secret ngẫu nhiên
python -c "import secrets; print(secrets.token_hex(32))"
```

Dán 2 kết quả trên vào `.env`:

```dotenv
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD_HASH=<kết quả lệnh bcrypt ở trên>
SESSION_SECRET_KEY=<kết quả lệnh secrets ở trên>
```

## 6. (Tuỳ chọn) Cấu hình cụm Ceph cần giám sát

Bỏ qua bước này nếu chưa có cụm, hoặc muốn cấu hình sau qua trang Cài đặt
(mục 9 bên dưới) — ứng dụng chạy bình thường không cần cụm nào cấu hình
sẵn.

Nếu đã có cụm Ceph và muốn điền luôn vào `.env`:

- **SSH key riêng cho ứng dụng** (không dùng chung key cá nhân) — tạo một
  keypair không passphrase (Watcher/Worker chạy nền, không ai ngồi gõ
  passphrase), rồi deploy public key vào **từng node MON/OSD** cần SSH tới:
  ```bash
  ssh-keygen -t ed25519 -f ~/.ssh/ceph_aiops_watcher -N "" -C "ceph-aiops"
  ssh-copy-id -i ~/.ssh/ceph_aiops_watcher.pub root@<ip-node-mon>
  ```
  Điền `SSH_KEY_PATH=` trỏ tới file **private** key vừa tạo (không phải
  `.pub`), và `CEPH_MON_NODES=` là danh sách IP các MON node, cách nhau
  bởi dấu phẩy.
- **`CEPH_EXEC_MODE`** — cách chạy lệnh `ceph ...` trên node, tuỳ cụm được
  deploy kiểu gì:
  - `cephadm` — cụm deploy bằng cephadm (khuyến nghị cho bản Ceph mới),
    không cần điền `CEPH_CONTAINER_NAME`
  - `docker` / `podman` — cụm chạy container thủ công với tên container cố
    định, cần điền `CEPH_CONTAINER_NAME`
  - `none` — `ceph` binary cài thẳng trên host (ceph-deploy / cài package)

Mọi mục còn lại (`CEPH_OSD_NODES`, `CEPH_MGR_NODES`, `CEPH_RGW_NODES`,
`CEPH_RBD_POOLS`, ...) đều tuỳ chọn — xem chú thích ngay trong
`.env.example`, để trống nếu không dùng tính năng tương ứng.

## 7. Khởi tạo database

```bash
alembic upgrade head
```

File SQLite (`ceph_aiops.db`, theo `DATABASE_URL`) sẽ được tạo tự động ở
lần chạy đầu. Không thấy lỗi nào in ra là thành công.

## 8. Chạy 3 tiến trình

Mở 3 terminal (hoặc dùng `nohup ... & disown` để chạy nền), đều từ thư mục
gốc repo với venv đã activate:

> Chưa cấu hình `CEPH_MON_NODES` ở bước 6 (vd chưa có cụm)? Watcher log dòng
> `run: no MON nodes configured (...) — cấu hình CEPH_MON_NODES (.env hoặc
> trang Cài đặt) để bắt đầu giám sát` lặp lại mỗi
> `WATCHER_POLL_INTERVAL_SECONDS` giây — **đây là bình thường, không phải
> lỗi**, không cần Ctrl-C. Cứ để nó chạy, tiếp tục sang Worker/Dashboard,
> quay lại cấu hình cụm qua trang Cài đặt sau (mục 9) — hệ thống tự khởi
> động lại Watcher cho bạn ngay khi lưu, không cần tự tay restart.

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
`DASHBOARD_USERNAME`/mật khẩu đã tạo hash ở bước 5. Đăng nhập thành công là
coi như cài đặt xong — mọi cấu hình còn lại (cụm Ceph, API AI) làm được
ngay trong Dashboard, không cần SSH vào server nữa (xem bước 9).

> Không mở được trang / "connection refused"? Kiểm tra tiến trình dashboard
> thật sự đang chạy (`pgrep -fa "uvicorn dashboard.app"`), và firewall của
> máy có cho phép cổng 8000 không (`sudo ufw allow 8000` trên
> Ubuntu nếu có bật ufw).
>
> Repo không dùng systemd unit — cả 3 tiến trình đều là background process
> thuần. Muốn restart, `pkill -f "python -m watcher.main"` (tương tự cho
> `worker.main` / `uvicorn dashboard.app`) rồi chạy lại lệnh `nohup` ở
> trên. `scripts/deploy/restart_services.sh` đã đóng gói sẵn toàn bộ quy
> trình này (pull code mới nhất + migrate + restart) — dùng lại được cho
> máy mới nếu muốn.

## 9. Cấu hình cụm Ceph / AI qua Dashboard (thay vì `.env`)

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

Nói cách khác: sau lần chạy tay ban đầu (bước 8), **hầu hết các thay đổi
cấu hình sau này không cần SSH/`nohup` thủ công nữa** — chỉ cần vào Cài đặt
và lưu. Bước 8 vẫn cần thiết cho lần khởi động đầu tiên trên máy mới, và
cho các thay đổi CODE (không phải cấu hình) — khi đó dùng lại
`scripts/deploy/restart_services.sh` hoặc lệnh `nohup` thủ công.

## 10. Kiểm tra hoạt động

```bash
pytest
```

Mặc định loại trừ nhóm test `live` (gọi SSH/API thật ra cụm Ceph/API AI
thật — chỉ chạy tay khi có sẵn cụm lab thật để test):

```bash
pytest -m live   # chỉ chạy khi thật sự có cụm/API AI để test
```

## 11. CI/CD (tuỳ chọn)

Repo có sẵn `.github/workflows/ci-cd.yml`: tự động chạy test trên mọi
push/PR, và tự động SSH vào server để deploy lại khi push lên `main`. Xem
`scripts/deploy/README.md` để set up SSH deploy key + GitHub Secrets cho
máy chủ mới.

## Xử lý sự cố thường gặp

- **`ensurepip is not available`** khi chạy `python3.11 -m venv .venv` →
  thiếu gói `python3.11-venv` (`sudo apt install python3.11-venv`).
- **`ModuleNotFoundError` khi chạy `python -m watcher.main`/`pytest`/...**
  → quên activate venv (`source .venv/bin/activate`) ở terminal đó, hoặc
  cài dependency vào nhầm Python khác `.venv`.
- **`extra fields not permitted` / lỗi validate `Settings`** khi khởi động
  bất kỳ tiến trình nào → `.env` có biến thừa không có trong
  `config/settings.py` (thường do gõ nhầm tên biến) — đối chiếu lại với
  `.env.example`.
- **`Không kết nối được database — đã chạy alembic upgrade head chưa?`**
  trên Dashboard → chưa chạy bước 7.
- **RabbitMQ: `Connection refused` khi Watcher/Worker khởi động** → kiểm
  tra RabbitMQ đã chạy (bước 1.2) và `RABBITMQ_URL` trong `.env` đúng.
- **Watcher báo "mất kết nối cụm"** → kiểm tra `SSH_KEY_PATH` đã deploy
  đúng public key lên node MON, và `CEPH_MON_NODES`/`SSH_USER` đúng.
- **`xóa pool` báo lỗi `EPERM: pool deletion is disabled`** → cụm Ceph mặc
  định chặn xoá pool; tính năng `delete_pool` của Chat-with-AI tự bật/tắt
  `mon_allow_pool_delete` quanh lệnh xoá, không cần tự cấu hình tay.
- **Chat-with-AI báo "Chưa kết nối API AI"** → chưa cấu hình
  `ROUTER_API_KEY`/`ROUTER_BASE_URL`/`ROUTER_MODEL` (qua `.env` hoặc trang
  Cài đặt — chọn loại kết nối Claude/Codex/OpenRouter/9router rồi nhập key).
