"""osd_id -> host SSH-được, tra thật chứ không đoán.

2026-08-20 -- tách ra từ `watcher/bluestore_omap_monitor.py`, nơi cách tra
đúng đã tồn tại từ trước nhưng chỉ một mình module đó dùng. Trong khi ấy
`watcher/collector.py::identify_relevant_nodes` vẫn mang comment "No cheap
osd-id -> host mapping available in v1" và trả về TOÀN BỘ danh sách node OSD
cho mọi ceph_code `OSD_`/`PG_`. Danh sách phẳng đó đi thẳng vào envelope,
rồi vào prompt LLM dưới dạng `Affected nodes: ip1, ip2, ip3` -- model không
có cách nào biết osd nào ở máy nào nên nó ĐOÁN, và sinh ra những câu chẩn
đoán kiểu "osd.2, osd.4 và osd.5 trên node <ip>" với ip sai. Tệ hơn: cùng
danh sách ấy được ghi vào `Action.target_nodes`, nên lệnh khắc phục cũng
nhắm sai máy.

Cách tra ở đây cố ý KHÔNG dùng `crush_host` từ `ceph osd tree`: đó là
hostname trong CRUSH map, mà app này không có bảng hostname->IP cho node OSD
(chỉ MON mới có cặp ceph_mon_hostnames/ceph_mon_nodes). Thay vào đó hỏi
thẳng từng node OSD đã cấu hình xem systemd của nó đang nạp những unit osd
nào -- kết quả luôn là một địa chỉ mà app này thật sự SSH được.

Nguyên tắc quan trọng nhất, giữ nguyên từ bản gốc: osd_id không khớp host
nào đã cấu hình thì VẮNG MẶT khỏi dict trả về. Người gọi phải xử lý "không
biết" như "không biết", tuyệt đối không thay bằng một host đoán bừa.
"""

from __future__ import annotations

import re

from shared.cluster_nodes import configured_nodes
from watcher import ceph_client

# Khớp CẢ hai kiểu đặt tên unit mà app này đã xử lý ở nơi khác
# (worker/executor/commands.py::_bluestore_omap_quick_fix_command):
# "ceph-osd@5.service" (ceph_exec_mode=none, cài kiểu package) và
# "...@osd.5.service" (cephadm). `\b` sau chữ số chặn "osd@1" nuốt nhầm
# vào "osd@15" nằm sau trên cùng dòng.
_OSD_UNIT_ID_RE = re.compile(r"osd[@.](\d+)\b")

# Ceph gọi tên OSD bị ảnh hưởng ngay trong phần detail của check, ví dụ
# "osd.5 legacy (not per-pool) BlueStore omap detected..." hay
# "osd.2 had slow ops". Regex này quét mọi "osd.N" trên tất cả dòng detail.
_OSD_DETAIL_ID_RE = re.compile(r"osd\.(\d+)")


def osd_ids_in_detail(check_detail: dict) -> set[int]:
    """Bóc mọi osd_id mà phần `detail` của một check `ceph health detail`
    nhắc tới. Rỗng nếu check không nêu đích danh OSD nào -- người gọi phải
    coi đó là "không xác định được", đừng suy diễn thêm."""
    if not isinstance(check_detail, dict):
        return set()
    entries = check_detail.get("detail") or []
    messages = [d.get("message", "") for d in entries if isinstance(d, dict)]
    return {int(m) for msg in messages for m in _OSD_DETAIL_ID_RE.findall(msg)}


def _discover_local_osd_ids(host: str, cluster=None) -> set[int]:
    """Best-effort, theo từng host -- một node OSD không SSH được không được
    phép chặn việc tra mọi osd_id CÒN LẠI (cùng thái độ "một trục trặc SSH
    không liên quan không bao giờ được là lý do bỏ sót một đề xuất thật" mà
    dashboard/routes/upgrade.py::_check_os_upgrade_needed đã ghi rõ)."""
    command = "systemctl list-units --all 2>/dev/null | grep -i osd || true"
    try:
        if cluster is not None:
            output = ceph_client.run_command_on_node_with(
                host, command, cluster.ssh_user, cluster.ssh_key_path
            )
        else:
            output = ceph_client.run_command_on_node(host, command)
    except Exception:
        return set()
    return {int(m) for m in _OSD_UNIT_ID_RE.findall(output)}


def resolve_osd_hosts(osd_ids: set[int], cluster=None) -> dict[int, str]:
    """Ánh xạ từng osd_id sang host OSD đã cấu hình đang thật sự nạp unit
    của osd đó. osd_id không khớp host nào (chạy ngoài danh sách node app
    này biết) sẽ vắng mặt khỏi kết quả -- không đoán.

    `cluster`: khi có, đọc danh sách node và SSH creds từ chính Cluster đó
    thay vì `settings` toàn cục, cùng nếp opt-in như mọi hàm khác trong
    watcher/.
    """
    remaining = set(osd_ids)
    result: dict[int, str] = {}
    for node in configured_nodes(cluster):
        if not remaining:
            break
        if "OSD" not in node["roles"]:
            continue
        local_ids = _discover_local_osd_ids(node["host"], cluster)
        for osd_id in local_ids & remaining:
            result[osd_id] = node["host"]
        remaining -= local_ids
    return result
