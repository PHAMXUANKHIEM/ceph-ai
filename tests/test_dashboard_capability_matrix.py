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
