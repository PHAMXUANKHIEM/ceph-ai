"""CephFS · NFS · RGW services (dashboard/routes/file_services.py, worker/executor/commands.py)."""

import json
from types import SimpleNamespace

import pytest

from config.settings import settings
from dashboard.routes import file_services
from shared import db as db_module
from shared.models import Action
from worker.executor import commands
from worker.executor.ssh_executor import ExecutorError


@pytest.mark.parametrize(("action_id", "params", "expected"), [
    ("cephfs_create_volume", {"fs_name": "cephfs"}, "ceph fs volume create cephfs"),
    ("cephfs_create_subvolume", {"fs_name": "cephfs", "subvolume": "app1", "size_gib": 10},
     "ceph fs subvolume create cephfs app1 --size 10737418240"),
    ("nfs_create_cluster", {"nfs_id": "nfs1", "hosts": ["ceph1", "ceph2"]}, "ceph nfs cluster create nfs1 '2 ceph1 ceph2'"),
    ("nfs_create_export", {"nfs_id": "nfs1", "fs_name": "cephfs", "pseudo_path": "/app1", "path": "/", "readonly": True},
     "ceph nfs export create cephfs --cluster-id nfs1 --pseudo-path /app1 --fsname cephfs --path / --readonly"),
    ("rgw_apply_service", {"service_id": "s3", "hosts": ["ceph1"], "port": 8080},
     "ceph orch apply rgw s3 --placement='1 ceph1' --port=8080"),
])
def test_service_commands(action_id, params, expected):
    assert commands.get_command(action_id, None, params) == expected


@pytest.mark.parametrize(("action_id", "params"), [
    ("cephfs_create_volume", {"fs_name": "x; rm -rf /"}),
    ("cephfs_create_subvolume", {"fs_name": "a", "subvolume": "b", "size_gib": 0}),
    ("nfs_create_cluster", {"nfs_id": "n", "hosts": []}),
    ("nfs_create_cluster", {"nfs_id": "n", "hosts": ["a", "a"]}),
    ("nfs_create_export", {"nfs_id": "n", "fs_name": "f", "pseudo_path": "/x", "path": "/a/../../etc"}),
    ("nfs_create_export", {"nfs_id": "n", "fs_name": "f", "pseudo_path": "x", "path": "/"}),
    ("rgw_apply_service", {"service_id": "s3", "hosts": ["h1"], "port": 70000}),
])
def test_bad_service_parameters_are_refused(action_id, params):
    with pytest.raises(ExecutorError):
        commands.get_command(action_id, None, params)


def test_state_is_read_in_two_shells(monkeypatch):
    calls = []

    def batch(cluster, cmds):
        calls.append(cmds)
        if len(calls) == 1:
            return [[{"name": "cephfs", "metadata_pool": "m", "data_pools": ["d"]}], ["nfs1"],
                    [{"service_name": "rgw.s3", "placement": {"count": 1}, "status": {"running": 1, "size": 1}}],
                    [{"hostname": "ceph1"}]]
        return [{"mdsmap": [{"name": "cephfs.a", "state": "active", "rank": 0}]}, [{"name": "app1"}],
                [{"pseudo": "/app1", "path": "/", "fsal": {"fs_name": "cephfs"}, "access_type": "RW"}]]

    monkeypatch.setattr(file_services, "_batch", batch)
    monkeypatch.setattr(file_services, "_exec_mode", lambda cluster: "cephadm")

    state = file_services.load_state(SimpleNamespace(id="c1"))

    assert len(calls) == 2
    assert state["filesystems"][0]["mds"] == [{"name": "cephfs.a", "state": "active", "rank": 0}]
    assert state["filesystems"][0]["subvolumes"] == ["app1"]
    assert state["nfs"] == [{"id": "nfs1", "exports": [{"pseudo": "/app1", "path": "/", "fs": "cephfs", "access": "RW"}]}]
    assert state["rgw"][0]["name"] == "rgw.s3" and state["hosts"] == ["ceph1"]


@pytest.fixture
def admin(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")
    monkeypatch.setattr(file_services, "load_state", lambda cluster: {
        "filesystems": [{"name": "cephfs", "metadata_pool": "m", "data_pools": ["d"], "mds": [], "subvolumes": []}],
        "nfs": [], "rgw": [], "hosts": ["ceph1", "ceph2"], "cephadm": True})
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    return dashboard_client


def test_the_page_shows_services_and_a_proposal_waits_for_approval(admin):
    page = admin.get("/file-services").text
    assert "CephFS · NFS · RGW" in page and "cephfs" in page and "Đề xuất tạo cụm NFS" in page

    response = admin.post("/file-services/propose", json={"action_id": "nfs_create_cluster", "nfs_id": "nfs1",
                                                         "hosts": ["ceph1", "ceph2"]})

    assert response.status_code == 201, response.text
    with db_module.SessionLocal() as session:
        action = session.get(Action, response.json()["action_id"])
        assert (action.action_id, action.classification, action.status) == (
            "nfs_create_cluster", "RISKY", "PENDING_APPROVAL")
        assert json.loads(action.action_params) == {"nfs_id": "nfs1", "hosts": ["ceph1", "ceph2"]}
        assert action.proposed_command == "ceph nfs cluster create nfs1 '2 ceph1 ceph2'"
    assert "Duyệt" in admin.get("/file-services").text


@pytest.mark.parametrize("body", [
    {"action_id": "delete_pool", "pool_name": "x"},
    {"action_id": "cephfs_create_volume", "fs_name": "bad name"},
    {"action_id": "rgw_apply_service", "service_id": "s3", "hosts": ["ceph1"], "port": "abc"},
])
def test_bad_proposals_are_refused(admin, body):
    assert admin.post("/file-services/propose", json=body).status_code == 400
    with db_module.SessionLocal() as session:
        assert session.query(Action).count() == 0


def test_an_approved_service_proposal_goes_to_the_worker(admin):
    created = admin.post("/file-services/propose", json={"action_id": "cephfs_create_volume", "fs_name": "fs2"}).json()

    response = admin.post(f"/actions/{created['action_id']}/approve", follow_redirects=False)

    assert response.status_code in (200, 303), response.text
    with db_module.SessionLocal() as session:
        assert session.get(Action, created["action_id"]).status == "APPROVED"
