"""Phân biệt ceph_code THẬT của Ceph với ceph_code do monitor tự đặt.

`ceph health detail` chỉ biết những check của chính Ceph (OSD_DOWN,
MON_CLOCK_SKEW, PG_DEGRADED...). Bên cạnh đó, app này tự sinh ra một loạt
ceph_code riêng cho những vấn đề Ceph không có check sẵn — CRUSH skew, độ
trễ OSD, tải CPU/RAM node, dung lượng database, volume bão hoà, thiết bị
sắp hỏng, BlueStore omap kiểu cũ — và mỗi module monitor TỰ QUẢN vòng đời
tạo/đóng của họ ceph_code ấy, bằng dữ liệu riêng của nó (streak, tập
osd_id đang ảnh hưởng, cửa sổ trượt...).

Hệ quả: với những code tự đặt đó, "không thấy trong `ceph health detail`"
KHÔNG có nghĩa là đã hết — nó không bao giờ xuất hiện ở đó ngay từ đầu.
Bất cứ chỗ nào lấy `current_codes` từ health làm chuẩn để kết luận
"đã khỏi" đều phải bỏ qua chúng, nếu không sẽ đóng nhầm Incident ngay lượt
poll kế tiếp.

Điều kiện này trước 2026-08-20 tồn tại dưới dạng một chuỗi if dài trong
`watcher/main.py::_resolve_recovered_incidents`. Tách ra đây vì
`watcher/verify.py` cần đúng cùng một danh sách — hai bản chép tay sẽ lệch
nhau ngay lần thêm monitor tiếp theo, và lần lệch đó sẽ biểu hiện thành
Incident tự đóng một cách khó hiểu.
"""

from __future__ import annotations

from watcher.bluestore_omap_monitor import BLUESTORE_OMAP_PREFIX
from watcher.crush_skew_monitor import CRUSH_SKEW_PG_PREFIX, CRUSH_SKEW_USE_PREFIX
from watcher.database_capacity_monitor import DATABASE_SIZE_HIGH_PREFIX
from watcher.device_health_monitor import DEVICE_HEALTH_EVACUATE_PREFIX
from watcher.node_health_monitor import NODE_RESOURCE_HIGH_PREFIX
from watcher.osd_latency_monitor import OSD_LATENCY_HIGH_PREFIX
from watcher.volume_monitor import VOLUME_SATURATED_PREFIX

# Incident tổng hợp, không gắn với một check nào của Ceph: một hành động
# operator xác nhận từ khung Chat, và một đề xuất nâng cấp cụm.
CHAT_REQUEST_CEPH_CODE = "CHAT_REQUEST"
CLUSTER_UPGRADE_CEPH_CODE = "CLUSTER_UPGRADE"

_EXACT_CODES = (CHAT_REQUEST_CEPH_CODE, CLUSTER_UPGRADE_CEPH_CODE, DATABASE_SIZE_HIGH_PREFIX)

_PREFIXES = (
    VOLUME_SATURATED_PREFIX,
    DEVICE_HEALTH_EVACUATE_PREFIX,
    NODE_RESOURCE_HIGH_PREFIX,
    BLUESTORE_OMAP_PREFIX,
    OSD_LATENCY_HIGH_PREFIX,
    CRUSH_SKEW_USE_PREFIX,
    CRUSH_SKEW_PG_PREFIX,
)


def is_monitor_owned(ceph_code: str) -> bool:
    """True nếu ceph_code này do một monitor của app tự đặt và tự quản vòng
    đời, tức KHÔNG BAO GIỜ xuất hiện trong `ceph health detail`.

    `DATABASE_SIZE_HIGH` so khớp chính xác chứ không theo tiền tố: nó không
    có hậu tố động theo entity (chỉ có đúng một database), nên `==` diễn tả
    đúng ý hơn và không vô tình nuốt một code khác cùng đầu chuỗi.
    """
    if not ceph_code:
        return False
    if ceph_code in _EXACT_CODES:
        return True
    return any(ceph_code.startswith(prefix) for prefix in _PREFIXES)
