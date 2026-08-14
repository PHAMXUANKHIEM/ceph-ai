from pathlib import Path

import dashboard.routes.pgs as pgs_route
from shared import db as db_module
from shared.models import Action, ActionStatus, AuditEntry, Cluster, Incident
from watcher.ceph_client import CephQueryError


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def test_unauthenticated_pgs_page_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/pgs", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_pool_name_mapping_supports_ceph_json_key_variants():
    assert pgs_route._pool_names_by_id([
        {"pool_id": 1, "pool_name": "rbd"},
        {"pool": 2, "poolname": "cephfs"},
        {"poolnum": 3, "name": "rgw"},
    ]) == {"1": "rbd", "2": "cephfs", "3": "rgw"}


def test_pg_filter_script_builds_pool_options_from_rendered_rows():
    script = (Path(pgs_route.__file__).resolve().parents[1] / "static" / "pgs.js").read_text()
    assert "row.dataset.pool" in script
    assert 'poolSelect.replaceChildren(new Option("Tất cả pool", ""))' in script
    assert "poolSelect.add(new Option(poolName, poolName))" in script


def test_pgs_page_returns_all_pgs_with_pool_and_scrub_details(dashboard_client, monkeypatch):
    calls = []

    def fake_query(command):
        calls.append(command)
        if command == "ceph osd pool ls detail":
            return "mon1", [
                {"pool_id": 1, "pool_name": "vms"},
                {"pool_id": 2, "pool_name": "backups"},
            ]
        if command == "ceph pg dump pgs":
            return "mon1", {
                "pg_stats": [
                    {
                        "pgid": "1.a",
                        "state": "active+clean",
                        "up": [0, 1, 2],
                        "acting": [0, 1, 2],
                        "acting_primary": 0,
                        "last_scrub_stamp": "2026-08-11T01:02:03Z",
                        "last_deep_scrub_stamp": "2026-08-10T01:02:03Z",
                    },
                    {
                        "pgid": "2.b",
                        "state": "active+degraded",
                        "up": [3, 4],
                        "acting": [3, 4],
                        "up_primary": 3,
                    },
                ]
            }
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(pgs_route.ceph_client, "run_ceph_json_command", fake_query)
    _login(dashboard_client)
    response = dashboard_client.get("/pgs")

    assert response.status_code == 200
    assert calls == ["ceph osd pool ls detail", "ceph pg dump pgs"]
    assert "1.a" in response.text
    assert "2.b" in response.text
    assert "vms" in response.text
    assert "backups" in response.text
    assert "active+clean" in response.text
    assert "active+degraded" in response.text
    assert "[0, 1, 2]" in response.text
    assert "2026-08-11T01:02:03Z" in response.text
    assert "2026-08-10T01:02:03Z" in response.text
    assert 'id="pg-search"' in response.text
    assert 'id="pg-id-filter"' in response.text
    assert 'id="pg-pool-filter"' in response.text
    assert 'aria-label="Lọc theo Pool name"' in response.text
    assert '<option value="vms">vms</option>' in response.text
    assert '<option value="backups">backups</option>' in response.text
    assert 'data-pgid="1.a" data-pool="vms"' in response.text
    assert 'src="/static/pgs.js' in response.text
    assert 'id="pg-pagination"' in response.text
    assert 'id="pg-page-prev"' in response.text
    assert 'id="pg-page-next"' in response.text
    assert "10 PGs mỗi trang" in response.text
    assert "Chọn một pool" not in response.text
    assert "Tạo pool" not in response.text


def test_pgs_page_shows_ceph_query_error(dashboard_client, monkeypatch):
    def fail(_command):
        raise CephQueryError("all MON nodes unavailable")

    monkeypatch.setattr(pgs_route.ceph_client, "run_ceph_json_command", fail)
    _login(dashboard_client)
    response = dashboard_client.get("/pgs")
    assert response.status_code == 200
    assert "all MON nodes unavailable" in response.text


def test_pgs_page_uses_selected_additional_cluster(dashboard_client, monkeypatch):
    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="cluster-b", ceph_mon_nodes="10.0.0.21", ceph_container_name="mon-b",
            ssh_user="ceph-b", ssh_key_path="/keys/b", ceph_exec_mode="cephadm",
            is_default=False, is_active=True,
        )
        session.add(cluster)
        session.commit()
        cluster_id = cluster.id

    calls = []

    def fake_query(*args):
        calls.append(args)
        if args[-1] == "ceph osd pool ls detail":
            return "10.0.0.21", [{"pool_id": 9, "pool_name": "rbd-b"}]
        return "10.0.0.21", {
            "pg_stats": [{"pgid": "9.a", "state": "active+clean", "acting": [2], "up": [2]}]
        }

    monkeypatch.setattr(pgs_route, "run_ceph_json_command_with", fake_query)
    _login(dashboard_client)
    dashboard_client.get(f"/?cluster={cluster_id}")
    response = dashboard_client.get("/pgs")

    assert response.status_code == 200
    assert "9.a" in response.text
    assert 'aria-label="Chọn cluster"' in response.text
    assert "cluster-b" in response.text
    assert calls[0] == (
        ["10.0.0.21"], "mon-b", "ceph-b", "/keys/b", "cephadm",
        "ceph osd pool ls detail",
    )
    assert calls[1] == (
        ["10.0.0.21"], "mon-b", "ceph-b", "/keys/b", "cephadm",
        "ceph pg dump pgs",
    )


def test_create_pool_targets_selected_additional_cluster(dashboard_client):
    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="cluster-create", ceph_mon_nodes="10.0.0.31,10.0.0.32",
            ceph_container_name="", ssh_user="ceph", ssh_key_path="/keys/create",
            ceph_exec_mode="cephadm", is_default=False, is_active=True,
        )
        session.add(cluster)
        session.commit()
        cluster_id = cluster.id

    _login(dashboard_client)
    dashboard_client.get(f"/?cluster={cluster_id}")
    response = dashboard_client.post(
        "/pgs/pools/create",
        data={"cluster_id": cluster_id, "pool_name": "rbd-new", "pg_num": "64"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert f"cluster={cluster_id}" in response.headers["location"]
    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(cluster_id=cluster_id).one()
        action = session.query(Action).filter_by(incident_id=incident.id).one()
        audit_row = session.query(AuditEntry).filter_by(action_id=action.id).one()
        assert action.action_id == "create_pool"
        assert action.status == ActionStatus.APPROVED.value
        assert action.proposed_command == (
            "ceph osd pool create rbd-new 64 && "
            "ceph osd pool application enable rbd-new rbd"
        )
        assert action.target_nodes == '["10.0.0.31"]'
        assert audit_row.event_type == "pool_create_requested"


def test_create_pool_rejects_cluster_different_from_session_selection(dashboard_client):
    with db_module.SessionLocal() as session:
        clusters = session.query(Cluster).all()
        selected = clusters[0]
        other = Cluster(
            name="other", ceph_mon_nodes="10.0.0.41", ceph_container_name="",
            ssh_user="ceph", ssh_key_path="/keys/other", ceph_exec_mode="cephadm",
            is_default=False, is_active=True,
        )
        session.add(other)
        session.commit()
        selected_id, other_id = selected.id, other.id

    _login(dashboard_client)
    dashboard_client.get(f"/?cluster={selected_id}")
    response = dashboard_client.post(
        "/pgs/pools/create",
        data={"cluster_id": other_id, "pool_name": "wrong-cluster", "pg_num": "32"},
    )

    assert response.status_code == 400
    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(action_id="create_pool").count() == 0
