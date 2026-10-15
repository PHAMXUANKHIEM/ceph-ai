# Cài Ceph AI trên máy khách (R8.1)

Bộ này dùng cho máy **không phải** máy Ceph AI chính. `compose.yaml` ở gốc repo và `scripts/deploy/*`
chỉ dành cho máy chính. Ở đây:

- stack nằm trong `deploy/customer/compose.yaml`:
  - PostgreSQL 18 và RabbitMQ 3.13 chạy **bên trong** stack, không mở cổng ra ngoài;
  - ứng dụng gọi chúng bằng tên `postgres` và `rabbitmq`;
  - RabbitMQ hay PostgreSQL có sẵn trên máy (ví dụ của OpenStack) **không bị đụng tới**;
- chạy 4 dịch vụ ứng dụng: Dashboard, Watcher, Remediation Watcher, Worker. Không có Telegram AI gateway,
  Single Full executor hay Vault monitor;
- mọi thao tác đi qua `scripts/customer/ceph-ai-customer.sh`, chạy với quyền root.

## Chuẩn bị (Ubuntu 22.04)

```sh
apt-get install -y podman golang-github-containernetworking-plugin-dnsname catatonit
python3 -m venv /opt/ceph-ai-customer/venv
/opt/ceph-ai-customer/venv/bin/pip install podman-compose==1.0.6
podman login ghcr.io            # token read:packages; cần TTY thật
git clone https://github.com/PHAMXUANKHIEM/ceph-ai.git /root/ceph-ai
git -C /root/ceph-ai checkout <sha đã duyệt>
```

Podman 3.4 của Ubuntu 22.04 ghi file mạng CNI với `cniVersion` 1.0.0, mà plugin CNI 0.9.1 đi kèm không hiểu.
Script tự tạo mạng `ceph-ai-customer_default` và hạ bản về mức các plugin hỗ trợ (`scripts/customer/cni_compat.py`).

## Cài lần đầu

```sh
/root/ceph-ai/scripts/customer/ceph-ai-customer.sh preflight --image ghcr.io/phamxuankhiem/ceph-ai@sha256:<digest> \
    --dashboard-bind 0.0.0.0:8001
/root/ceph-ai/scripts/customer/ceph-ai-customer.sh install --image ghcr.io/phamxuankhiem/ceph-ai@sha256:<digest> \
    --ssh-key <khoá SSH tới các node Ceph> --old-env <.env của bản cũ, nếu có> --dashboard-bind 0.0.0.0:8001
```

`install` làm lần lượt:
1. Kiểm tra máy (preflight).
2. Sinh mật khẩu DB, RabbitMQ và admin Dashboard. Mật khẩu không bao giờ được in; mật khẩu admin nằm ở
   `/root/.config/ceph-ai/dashboard-admin.pass`.
3. Chép khoá SSH thành `/etc/ceph-ai-customer/ssh/id_ed25519`. Khoá gốc giữ nguyên.
4. Dựng `/var/lib/ceph-ai/config/.env`:
   - chỉ chép từ `--old-env` những khoá mà image nhận;
   - in ra tên khoá đã chép, đã thay và đã bỏ;
   - đặt `AUTOPILOT_ENABLED=false`.
5. Nếu lần `install` trước dừng giữa chừng (chưa khởi động xong stack), chạy lại `install` sẽ **dùng lại** mật khẩu
   và `.env` đã sinh thay vì sinh mới.
6. Chạy migration, khởi động stack, cài `ceph-ai-customer.service` (tự chạy lại sau reboot), rồi kiểm tra
   Dashboard và số lần restart của từng container.

## Nâng cấp, sao lưu, quay lại

```sh
ceph-ai-customer.sh upgrade --image ghcr.io/phamxuankhiem/ceph-ai@sha256:<digest mới>   # tự sao lưu DB trước
ceph-ai-customer.sh backup                      # /var/backups/ceph-ai, giữ 7 bản, có .sha256
CEPH_AI_ROLLBACK_ACK_COMPATIBLE_SCHEMA=yes ceph-ai-customer.sh rollback   # image trước; KHÔNG đảo DB
ceph-ai-customer.sh status | start | stop
```

Trước khi nâng cấp hãy `git -C /root/ceph-ai checkout <sha mới>`, để compose và script khớp với image.

## Dữ liệu và bí mật

| Đường dẫn | Nội dung |
|---|---|
| `/var/lib/ceph-ai/` | dữ liệu ứng dụng; `config/.env` (0640 root:10001) |
| `/var/lib/ceph-ai-postgres/`, `/var/lib/ceph-ai-rabbitmq/` | dữ liệu PostgreSQL / RabbitMQ |
| `/etc/ceph-ai-customer/` (0700) | `postgres.env`, `rabbitmq.env`, `compose.env`, `ssh/id_ed25519` |
| `/var/backups/ceph-ai/` | bản dump DB |

Federation (`/federation` trên Dashboard) còn cần file `/var/lib/ceph-ai/config/federation-enroll.pass`,
do nhà cung cấp gửi riêng.
