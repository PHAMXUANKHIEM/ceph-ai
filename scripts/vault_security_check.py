"""CLI banner and one-shot Vault security check. It prints no token or secret."""
from __future__ import annotations
import sys
from watcher.vault_monitor import VaultMonitor

BANNER = """⚠️ CẢNH BÁO
Hệ thống này chỉ dành cho người dùng được ủy quyền. Mọi hoạt động truy cập,
đọc/ghi secret đều được ghi log và giám sát. Việc sử dụng trái phép hoặc
vượt quá quyền hạn được cấp có thể bị xử lý kỷ luật và/hoặc truy cứu trách
nhiệm pháp lý.

Bằng việc tiếp tục đăng nhập, bạn xác nhận đã hiểu và đồng ý với chính
sách bảo mật của tổ chức.
"""


def main() -> int:
    print(BANNER, file=sys.stderr)
    findings = VaultMonitor().check_once(notify=False)
    for finding in findings:
        print(f"⚠️  [CẢNH BÁO {finding['severity'].upper()}] {finding['title']}: {finding['detail']}", file=sys.stderr)
    return 2 if any(item["severity"] == "critical" for item in findings) else 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
