from shared import capability_matrix as cm


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def test_unauthenticated_get_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/capability-matrix", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_page_empty_state(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/capability-matrix")
    assert response.status_code == 200
    assert "UNKNOWN" in response.text or "Chưa có capability matrix entry" in response.text


def test_create_entry_via_form(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/capability-matrix/create",
        data={
            "command_id": "ceph_versions",
            "inner_command": "ceph versions",
            "doc_url": "https://docs.ceph.com/en/latest/man/8/ceph/",
            "min_major": "14",
            "max_major": "",
        },
    )
    assert response.status_code == 200
    assert "Đã thêm entry" in response.text

    entries = cm.list_entries()
    assert len(entries) == 1
    assert entries[0].command_id == "ceph_versions"
    assert entries[0].verified_by == "admin"


def test_create_entry_rejects_non_url_doc_source(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/capability-matrix/create",
        data={
            "command_id": "ceph_versions",
            "inner_command": "ceph versions",
            "doc_url": "some blog post I read",
            "min_major": "14",
        },
    )
    assert response.status_code == 200
    assert "tài liệu Ceph chính thức" in response.text
    assert cm.list_entries() == []


def test_deprecate_entry_via_form(dashboard_client):
    _login(dashboard_client)
    entry = cm.create_entry(
        command_id="ceph_df", inner_command="ceph df",
        doc_url="https://docs.ceph.com/en/latest/rados/operations/monitoring/",
        verified_by="admin", min_major=14,
    )
    response = dashboard_client.post(f"/capability-matrix/{entry.id}/deprecate")
    assert response.status_code == 200
    assert "Đã deprecate" in response.text
    assert cm.list_entries() == []
    assert cm.list_entries(include_deprecated=True)[0].status == "DEPRECATED"


def test_page_shows_preflight_readiness_and_the_finite_gap(dashboard_client):
    """Điểm cốt lõi của L-0.2: biến "hãy seed capability matrix" từ việc
    nghe như vô hạn thành một checklist đếm được, và cho operator thấy TRƯỚC
    hậu quả của việc bật enforcement."""
    _login(dashboard_client)

    response = dashboard_client.get("/capability-matrix")

    assert response.status_code == 200
    assert "Mức sẵn sàng bật cổng an toàn" in response.text
    assert "ĐANG BẬT" in response.text          # Pha 0: fail-closed mặc định
    assert "sẽ bị chặn" in response.text        # hậu quả nếu bật ngay
    assert "restart_osd_daemon" in response.text
    # Và KHÔNG liệt kê họ management — chúng không đi qua cổng này.
    assert "delete_pool" not in response.text


def test_readiness_reflects_a_seeded_entry(dashboard_client):
    """Cần ĐỦ hai tiền đề mới đi qua được: Pha 0.1 đã quét ra phiên bản cụm,
    VÀ matrix có entry phủ đúng phiên bản đó. Thiếu bản quét thì dù đã seed
    vẫn UNKNOWN — chính là fail-closed hoạt động đúng."""
    from datetime import datetime

    from shared import db as db_module
    from shared.clusters import ensure_default_cluster
    from shared.models import CapabilityStatus, ClusterCapabilityInventory

    with db_module.SessionLocal() as session:
        cluster = ensure_default_cluster(session)
        session.add(ClusterCapabilityInventory(
            cluster_id=cluster.id,
            status=CapabilityStatus.SUPPORTED.value,
            deployment_mode="cephadm",
            current_version="18.2.2",
            current_major=18,
            collected_at=datetime.utcnow(),
        ))
        session.commit()

    _login(dashboard_client)
    dashboard_client.post(
        "/capability-matrix/create",
        data={
            "command_id": "restart_osd_daemon",
            "inner_command": "systemctl restart ceph-osd@N",
            "doc_url": "https://docs.ceph.com/en/latest/rados/operations/operating/",
            "min_major": "14",
            "max_major": "",
        },
    )

    response = dashboard_client.get("/capability-matrix")

    assert "SUPPORTED" in response.text
