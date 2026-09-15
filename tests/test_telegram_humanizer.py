"""Regression: humanizer phải chấp nhận câu tóm tắt đúng trên LOG CEPH THẬT.

Bộ test cũ chỉ dùng chuỗi đồ chơi ("OSD 2 DOWN trên node 10.20.1.195") nên
không phát hiện được lỗi chuẩn hoá `osd.7` vs `osd 7` — trên production mọi
alert đều rơi về fallback log máy.
"""

from __future__ import annotations

import pytest

from shared.telegram_humanizer import _identity_facts, rejection_reason

REAL_LOG = """HEALTH_WARN: 1 osds down; Degraded data redundancy: 1234/56789 objects degraded (2.17%), 12 pgs degraded
--- ceph-node03 (osd.7)
osd.7 down since 2026-09-15 08:12:03, last heartbeat no reply from 10.10.20.13:6802 osd.9
BlueStore slow op: 512 ms, pool=volumes, host=ceph-node03, used 87.5%"""


@pytest.mark.parametrize(
    "response",
    [
        # dạng "osd.7" đúng như prompt yêu cầu giữ nguyên
        "Cụm Ceph đang ở trạng thái HEALTH_WARN vì osd.7 trên node ceph-node03 "
        "đã dừng hoạt động. Khoảng 2,17% dữ liệu đang thiếu bản sao.",
        # trích lại nguyên văn tiếng Anh mà Ceph in ra
        "Ceph báo osd.7 chậm bất thường, health check failed trên pool volumes. "
        "Đây là thông tin quan sát được, chưa khẳng định nguyên nhân gốc.",
        # không nhắc lại mọi con số — đúng thiết kế mới
        "Một OSD trên ceph-node03 đã ngừng hoạt động khiến cụm chuyển sang HEALTH_WARN.",
    ],
)
def test_accepts_natural_vietnamese_about_real_log(response):
    assert rejection_reason(response, identity_facts=_identity_facts(REAL_LOG)) is None


@pytest.mark.parametrize(
    ("response", "expected_reason_fragment"),
    [
        ("Sự cố nằm ở osd.12 nên cần thay ổ đĩa ngay.", "không có trong nguồn"),
        ("Node 192.168.9.9 đang mất kết nối tới cụm Ceph.", "không có trong nguồn"),
        ("Cụm đang có sự cố cần kiểm tra ngay lập tức.", "mốc định danh"),
        ("osd: down\nnode: ceph-node03", "định dạng máy"),
        # hết max_tokens giữa câu -> Telegram nhận một câu cụt
        ("Cụm Ceph đang ở trạng thái HEALTH_WARN vì osd.7 trên node", "cắt cụt"),
    ],
)
def test_rejects_wrong_or_machine_response(response, expected_reason_fragment):
    reason = rejection_reason(response, identity_facts=_identity_facts(REAL_LOG))
    assert reason is not None and expected_reason_fragment in reason


def test_identity_facts_normalize_daemon_spelling():
    facts = _identity_facts("osd.7 down, OSD_9 flapping, osd 11 nearfull")
    assert {"osd7", "osd9", "osd11"} <= set(facts)
