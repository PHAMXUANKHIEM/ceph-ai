# Vault Security Runbook

## Banner trước khi login

Trên máy chạy Vault, đặt banner trong /etc/motd.d/99-vault-security-warning.
Có thể chạy python scripts/vault_security_check.py trước các thao tác CLI.
Script không in token hoặc secret.

⚠️ **CẢNH BÁO**

Hệ thống này chỉ dành cho người dùng được ủy quyền. Mọi hoạt động truy cập,
đọc/ghi secret đều được ghi log và giám sát. Việc sử dụng trái phép hoặc vượt
quá quyền hạn được cấp có thể bị xử lý kỷ luật và/hoặc truy cứu trách nhiệm
pháp lý.

Bằng việc tiếp tục đăng nhập, bạn xác nhận đã hiểu và đồng ý với chính sách
bảo mật của tổ chức.

## Cảnh báo tự động

watcher.vault_monitor chỉ đọc health, audit và token lookup:

- Vault SEALED hoặc chưa initialized: cảnh báo nghiêm trọng.
- Token sắp hết TTL: cảnh báo và nhắc renew/lấy token mới.
- Token có policy root: cảnh báo nghiêm trọng; revoke sau bootstrap.
- Không có audit device: cảnh báo nghiêm trọng.
- Không kết nối được Vault/TLS lỗi: cảnh báo nghiêm trọng.

Monitor không tự unseal, renew, revoke, bật audit hoặc sửa cấu hình Vault.

## Thiết lập

VAULT_ADDR=https://vault.example.internal:8200
VAULT_TOKEN_FILE=/etc/ceph-ai-secrets/vault.token
VAULT_MONITOR_ENABLED=true
VAULT_POLL_INTERVAL_SECONDS=60
VAULT_TOKEN_EXPIRY_WARNING_SECONDS=86400
VAULT_TLS_VERIFY=true

Token nên là token read-only có lookup-self và sys/audit; không lưu root token
trong .env, chat, email hoặc source code.

## Unseal keys

Unseal keys phải được chia cho nhiều người giữ bằng Shamir secret sharing và
không bao giờ được lưu chung một chỗ, chụp ảnh, hoặc gửi qua email/chat không
mã hóa. Mất đủ số lượng key threshold đồng nghĩa không thể mở khóa Vault.

## Root token, backup và TLS

Root token chỉ nên tồn tại tạm thời để bootstrap. Sau setup, phải revoke root
token và tạo policy/token least privilege cho vận hành hằng ngày.

Luôn backup storage backend (Consul, Raft, v.v.) và lưu unseal/recovery keys ở
nơi an toàn, tách biệt. Production không được chạy tls_disable=true; toàn bộ
traffic đến Vault phải được mã hóa.

Audit file mẫu:

vault audit enable file file_path=/var/log/vault_audit.log
