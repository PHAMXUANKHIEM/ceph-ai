from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config.settings import settings
from shared import capability_matrix as cm
from shared import db as db_module
from shared.db import Base
from shared.models import CapabilityMatrixChange, CapabilityMatrixEntry, CapabilityStatus


@pytest.fixture()
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    yield engine


# --- check_capability() fail-closed semantics -------------------------------


def test_check_capability_unknown_when_version_unknown(isolated_db):
    result = cm.check_capability("ceph_versions", None)
    assert result.status == CapabilityStatus.UNKNOWN
    assert result.entry is None


def test_check_capability_unknown_when_no_matrix_entry(isolated_db):
    result = cm.check_capability("ceph_versions", 18)
    assert result.status == CapabilityStatus.UNKNOWN


def test_check_capability_supported_when_entry_covers_version(isolated_db):
    cm.create_entry(
        command_id="ceph_versions", inner_command="ceph versions",
        doc_url="https://docs.ceph.com/en/latest/man/8/ceph/",
        verified_by="admin", min_major=14, max_major=None,
    )
    result = cm.check_capability("ceph_versions", 18)
    assert result.status == CapabilityStatus.SUPPORTED
    assert result.entry is not None
    assert result.is_stale is False


def test_check_capability_unsupported_version_when_entry_exists_but_doesnt_cover(isolated_db):
    cm.create_entry(
        command_id="ceph_orch_ps", inner_command="ceph orch ps",
        doc_url="https://docs.ceph.com/en/latest/cephadm/operations/",
        verified_by="admin", min_major=15, max_major=17,
    )
    result = cm.check_capability("ceph_orch_ps", 12)
    assert result.status == CapabilityStatus.UNSUPPORTED_VERSION


def test_check_capability_deprecated_entry_not_matched(isolated_db):
    entry = cm.create_entry(
        command_id="ceph_versions", inner_command="ceph versions",
        doc_url="https://docs.ceph.com/en/latest/man/8/ceph/",
        verified_by="admin", min_major=14,
    )
    cm.deprecate_entry(entry.id, actor="admin2")
    result = cm.check_capability("ceph_versions", 18)
    assert result.status == CapabilityStatus.UNKNOWN


def test_check_capability_stale_flag(isolated_db):
    old_date = datetime.utcnow() - timedelta(days=settings.capability_matrix_max_age_days + 10)
    cm.create_entry(
        command_id="ceph_versions", inner_command="ceph versions",
        doc_url="https://docs.ceph.com/en/latest/man/8/ceph/",
        verified_by="admin", min_major=14, verified_at=old_date,
    )
    result = cm.check_capability("ceph_versions", 18)
    assert result.status == CapabilityStatus.SUPPORTED
    assert result.is_stale is True


# --- create_entry() / deprecate_entry() audit trail -------------------------


def test_create_entry_writes_audit_row(isolated_db):
    entry = cm.create_entry(
        command_id="ceph_df", inner_command="ceph df",
        doc_url="https://docs.ceph.com/en/latest/rados/operations/monitoring/",
        verified_by="alice", min_major=14,
    )
    changes = cm.list_changes(entry.id)
    assert len(changes) == 1
    assert changes[0].change_type == "CREATED"
    assert changes[0].actor == "alice"


def test_deprecate_entry_writes_audit_row_and_changes_status(isolated_db):
    entry = cm.create_entry(
        command_id="ceph_df", inner_command="ceph df",
        doc_url="https://docs.ceph.com/en/latest/rados/operations/monitoring/",
        verified_by="alice", min_major=14,
    )
    deprecated = cm.deprecate_entry(entry.id, actor="bob")
    assert deprecated.status == "DEPRECATED"

    changes = cm.list_changes(entry.id)
    assert len(changes) == 2
    assert changes[0].change_type == "DEPRECATED"
    assert changes[0].actor == "bob"


def test_deprecate_entry_returns_none_for_unknown_id(isolated_db):
    assert cm.deprecate_entry("does-not-exist", actor="bob") is None


def test_list_entries_excludes_deprecated_by_default(isolated_db):
    entry = cm.create_entry(
        command_id="ceph_df", inner_command="ceph df",
        doc_url="https://docs.ceph.com/en/latest/rados/operations/monitoring/",
        verified_by="alice", min_major=14,
    )
    cm.deprecate_entry(entry.id, actor="bob")
    assert cm.list_entries(include_deprecated=False) == []
    assert len(cm.list_entries(include_deprecated=True)) == 1


# --- Báo cáo độ phủ (2026-08-19) -----------------------------------------


def test_gated_command_ids_is_the_diagnosis_enum_not_everything():
    """Preflight chỉ chạy ở nhánh tạo Action mới trong `diagnose_incident`,
    nên chỉ enum chẩn đoán sự cố mới đi qua cổng này. Gộp cả họ
    management/Chat vào đây sẽ thổi phồng việc-cần-làm một cách sai lệch và
    khiến operator tưởng phải seed hàng chục dòng."""
    from worker.policy import gate

    ids = set(cm.gated_command_ids())

    assert "restart_osd_daemon" in ids
    assert ids.isdisjoint(gate.VALID_MANAGEMENT_ACTION_IDS)
    assert "delete_pool" not in ids
    assert len(ids) < 15   # hữu hạn và nhỏ, không phải "vô hạn"


def test_empty_matrix_reports_everything_blocked(isolated_db):
    """Đây chính là hiện trạng deployment thật: bảng rỗng nên bật
    enforcement bây giờ sẽ chặn sạch. Báo cáo phải nói thẳng điều đó thay vì
    để operator bật lên rồi mới phát hiện."""
    report = cm.coverage_report(18)

    assert report["covered"] == 0
    assert report["blocked"] == report["total"] > 0
    assert report["ready"] is False
    assert all(r["status"] == "UNKNOWN" for r in report["rows"])


def test_seeding_one_entry_moves_exactly_one_row_out_of_blocked(isolated_db):
    cm.create_entry(
        command_id="restart_osd_daemon", inner_command="systemctl restart ceph-osd@N",
        doc_url="https://docs.ceph.com/en/latest/rados/operations/operating/",
        verified_by="admin", verified_at=datetime.utcnow(), min_major=14,
    )

    report = cm.coverage_report(18)

    assert report["covered"] == 1
    assert report["ready"] is False
    row = next(r for r in report["rows"] if r["command_id"] == "restart_osd_daemon")
    assert row["status"] == "SUPPORTED" and row["blocked"] is False


def test_entry_outside_version_range_still_counts_as_blocked(isolated_db):
    """Có entry KHÔNG đồng nghĩa với được đi qua — phải phủ đúng phiên bản
    của cụm. Một chỉ số phủ gộp chung mọi cụm sẽ giấu mất đúng ca này."""
    cm.create_entry(
        command_id="restart_osd_daemon", inner_command="x",
        doc_url="https://docs.ceph.com/en/latest/", verified_by="admin",
        verified_at=datetime.utcnow(), min_major=14, max_major=16,
    )

    assert cm.coverage_report(16)["covered"] == 1
    assert cm.coverage_report(18)["covered"] == 0   # cụm Reef vẫn hổng


def test_unknown_cluster_version_blocks_everything(isolated_db):
    """Pha 0.1 chưa quét được version thì không có gì để đối chiếu."""
    cm.create_entry(
        command_id="restart_osd_daemon", inner_command="x",
        doc_url="https://docs.ceph.com/en/latest/", verified_by="admin",
        verified_at=datetime.utcnow(), min_major=14,
    )

    report = cm.coverage_report(None)

    assert report["covered"] == 0 and report["ready"] is False
