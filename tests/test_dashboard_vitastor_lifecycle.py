import json

from shared import db
from shared.models import VitastorCluster, VitastorOperation
import dashboard.routes.vitastor_lifecycle as route


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin", "product": "vitastor"})


def _deploy_payload():
    return {
        "cluster_name": "vita-prod", "ssh_user": "root", "ssh_key_path": "/root/.ssh/vita",
        "etcd_prefix": "/vitastor", "osd_network": "10.20.1.0/24",
        "nodes": [
            {"host": "10.20.1.10", "roles": ["mon"], "disks": []},
            {"host": "10.20.1.11", "roles": ["osd"], "disks": ["/dev/nvme0n1"]},
        ],
    }


def test_deploy_requires_preview_then_explicit_execute(dashboard_client, monkeypatch):
    calls = []
    def fake_deploy(params, progress):
        calls.append(params); progress("preflight", "done", "OK")
    monkeypatch.setattr(route, "deploy", fake_deploy)
    _login(dashboard_client)

    proposed = dashboard_client.post("/vitastor/deploy-cluster/propose", json=_deploy_payload())
    assert proposed.status_code == 200
    operation_id = proposed.json()["operation_id"]
    assert calls == []
    with db.SessionLocal() as session:
        assert session.get(VitastorOperation, operation_id).status == "PENDING_APPROVAL"

    executed = dashboard_client.post(f"/vitastor/operations/{operation_id}/execute")
    assert executed.status_code == 200
    assert calls and calls[0]["nodes"][1]["disks"] == ["/dev/nvme0n1"]
    with db.SessionLocal() as session:
        assert session.get(VitastorOperation, operation_id).status == "SUCCESS"
        cluster = session.query(VitastorCluster).filter_by(name="vita-prod").one()
        assert "deployment" in json.loads(cluster.last_status_json)


def test_operation_execute_is_one_shot(dashboard_client, monkeypatch):
    calls = []
    monkeypatch.setattr(route, "deploy", lambda params, progress: calls.append(params))
    _login(dashboard_client)

    operation_id = dashboard_client.post("/vitastor/deploy-cluster/propose", json=_deploy_payload()).json()["operation_id"]
    first = dashboard_client.post(f"/vitastor/operations/{operation_id}/execute")
    second = dashboard_client.post(f"/vitastor/operations/{operation_id}/execute")

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(calls) == 1


def test_delete_requires_exact_name_and_can_preserve_disks(dashboard_client, monkeypatch):
    deleted_params = []
    monkeypatch.setattr(route, "delete", lambda params, progress: deleted_params.append(params))
    _login(dashboard_client)
    deployment = _deploy_payload()
    with db.SessionLocal() as session:
        cluster = VitastorCluster(name="vita-prod", management_host="10.20.1.10", etcd_address="10.20.1.10:2379", etcd_prefix="/vitastor", config_path="/etc/vitastor/vitastor.conf", ssh_user="root", ssh_key_path="/root/.ssh/vita", exec_mode="none", container_name="", created_by="admin", last_status_json=json.dumps({"deployment": deployment}))
        session.add(cluster); session.commit(); cluster_id = cluster.id

    bad = dashboard_client.post("/vitastor/delete-cluster/propose", json={"cluster_id": cluster_id, "confirmation": "wrong", "wipe_disks": False})
    assert bad.status_code == 400
    proposed = dashboard_client.post("/vitastor/delete-cluster/propose", json={"cluster_id": cluster_id, "confirmation": "vita-prod", "wipe_disks": False})
    operation_id = proposed.json()["operation_id"]
    dashboard_client.post(f"/vitastor/operations/{operation_id}/execute")
    assert deleted_params[0]["wipe_disks"] is False
    with db.SessionLocal() as session:
        assert session.get(VitastorCluster, cluster_id) is None


def test_reject_does_not_execute(dashboard_client, monkeypatch):
    monkeypatch.setattr(route, "deploy", lambda *_: (_ for _ in ()).throw(AssertionError("must not run")))
    _login(dashboard_client)
    operation_id = dashboard_client.post("/vitastor/deploy-cluster/propose", json=_deploy_payload()).json()["operation_id"]
    response = dashboard_client.post(f"/vitastor/operations/{operation_id}/reject")
    assert response.json()["status"] == "REJECTED"


def test_upgrade_requires_preview_and_runs_independently(dashboard_client, monkeypatch):
    calls = []
    monkeypatch.setattr(route, "upgrade", lambda params, progress: calls.append(params))
    _login(dashboard_client)
    with db.SessionLocal() as session:
        cluster = VitastorCluster(
            name="vita-prod", management_host="10.20.1.10",
            etcd_address="10.20.1.10:2379", etcd_prefix="/vitastor",
            config_path="/etc/vitastor/vitastor.conf", ssh_user="root",
            ssh_key_path="/root/.ssh/vita", exec_mode="none", container_name="",
            last_status_json=json.dumps({
                "osds": [
                    {"type": "osd", "parent": "10.20.1.11"},
                    {"type": "osd", "parent": "10.20.1.12"},
                ],
            }),
            created_by="admin",
        )
        session.add(cluster); session.commit(); cluster_id = cluster.id

    page = dashboard_client.get("/vitastor/upgrade")
    assert page.status_code == 200
    assert "Upgrade Vitastor Cluster" in page.text
    proposed = dashboard_client.post("/vitastor/upgrade/propose", json={
        "cluster_id": cluster_id, "target_version": "3.0.16",
        "nodes": ["10.20.1.11", "10.20.1.12"],
    })
    assert proposed.status_code == 200
    operation_id = proposed.json()["operation_id"]
    assert calls == []
    dashboard_client.post(f"/vitastor/operations/{operation_id}/execute")
    assert calls[0]["target_version"] == "3.0.16"
    assert calls[0]["nodes"] == ["10.20.1.11", "10.20.1.12"]
    with db.SessionLocal() as session:
        assert session.get(VitastorOperation, operation_id).status == "SUCCESS"
        assert session.get(VitastorCluster, cluster_id) is not None


def test_upgrade_rejects_unsafe_version_and_duplicate_nodes(dashboard_client):
    _login(dashboard_client)
    with db.SessionLocal() as session:
        cluster = VitastorCluster(
            name="vita-prod", management_host="10.20.1.10", etcd_address="",
            etcd_prefix="/vitastor", config_path="/etc/vitastor/vitastor.conf",
            ssh_user="root", ssh_key_path="/root/.ssh/vita", exec_mode="none",
            container_name="", created_by="admin",
        )
        session.add(cluster); session.commit(); cluster_id = cluster.id
    unsafe = dashboard_client.post("/vitastor/upgrade/propose", json={
        "cluster_id": cluster_id, "target_version": "3.0; reboot",
        "nodes": ["10.20.1.11"],
    })
    assert unsafe.status_code == 400
    duplicate = dashboard_client.post("/vitastor/upgrade/propose", json={
        "cluster_id": cluster_id, "target_version": "3.0.16",
        "nodes": ["10.20.1.11", "10.20.1.11"],
    })
    assert duplicate.status_code == 400


def test_upgrade_rejects_hosts_outside_cluster_topology(dashboard_client):
    _login(dashboard_client)
    with db.SessionLocal() as session:
        cluster = VitastorCluster(
            name="vita-prod", management_host="10.20.1.10",
            etcd_address="10.20.1.10:2379", etcd_prefix="/vitastor",
            config_path="/etc/vitastor/vitastor.conf", ssh_user="root",
            ssh_key_path="/root/.ssh/vita", exec_mode="none", container_name="",
            last_status_json=json.dumps({"osds": [{"type": "osd", "parent": "10.20.1.11"}]}),
            created_by="admin",
        )
        session.add(cluster); session.commit(); cluster_id = cluster.id

    response = dashboard_client.post("/vitastor/upgrade/propose", json={
        "cluster_id": cluster_id, "target_version": "3.0.16",
        "nodes": ["10.20.1.99"],
    })
    assert response.status_code == 400


def test_lifecycle_proposals_reject_inactive_cluster(dashboard_client):
    _login(dashboard_client)
    with db.SessionLocal() as session:
        cluster = VitastorCluster(
            name="inactive-vita", management_host="10.20.1.10",
            etcd_address="10.20.1.10:2379", etcd_prefix="/vitastor",
            config_path="/etc/vitastor/vitastor.conf", ssh_user="root",
            ssh_key_path="/root/.ssh/vita", exec_mode="none", container_name="",
            is_active=False, created_by="admin",
        )
        session.add(cluster); session.commit(); cluster_id = cluster.id

    upgrade = dashboard_client.post("/vitastor/upgrade/propose", json={
        "cluster_id": cluster_id, "target_version": "3.0.16", "nodes": ["10.20.1.10"],
    })
    backup = dashboard_client.post("/vitastor/backup/propose", json={
        "cluster_id": cluster_id, "method": "snapshot", "image": "vm-100", "snapshot": "daily",
    })
    assert upgrade.status_code == 404
    assert backup.status_code == 404


def test_backup_supports_all_documented_methods_with_approval(dashboard_client, monkeypatch):
    calls = []
    monkeypatch.setattr(route, "backup", lambda params, progress: calls.append(params))
    _login(dashboard_client)
    with db.SessionLocal() as session:
        cluster = VitastorCluster(
            name="vita-prod", management_host="10.20.1.10", etcd_address="10.20.1.10:2379",
            etcd_prefix="/vitastor", config_path="/etc/vitastor/vitastor.conf",
            ssh_user="root", ssh_key_path="/root/.ssh/vita", exec_mode="none",
            container_name="", created_by="admin",
        )
        session.add(cluster); session.commit(); cluster_id = cluster.id
    assert dashboard_client.get("/vitastor/backup").status_code == 200
    proposed = dashboard_client.post("/vitastor/backup/propose", json={
        "cluster_id": cluster_id, "method": "full_qcow2", "image": "vm/100-disk-0",
        "snapshot": "daily-20260813", "destination": "/backup/vm-100.qcow2",
    })
    assert proposed.status_code == 200
    assert calls == []
    dashboard_client.post(f"/vitastor/operations/{proposed.json()['operation_id']}/execute")
    assert calls[0]["method"] == "full_qcow2"


def test_backup_validates_destination_and_incremental_backing(dashboard_client):
    _login(dashboard_client)
    with db.SessionLocal() as session:
        cluster = VitastorCluster(
            name="vita-prod", management_host="10.20.1.10", etcd_address="10.20.1.10:2379",
            etcd_prefix="/vitastor", config_path="", ssh_user="root",
            ssh_key_path="/root/.ssh/vita", exec_mode="none", container_name="", created_by="admin",
        )
        session.add(cluster); session.commit(); cluster_id = cluster.id
    relative = dashboard_client.post("/vitastor/backup/propose", json={
        "cluster_id": cluster_id, "method": "raw", "image": "vm-100",
        "snapshot": "daily", "destination": "backup.raw",
    })
    assert relative.status_code == 400
    missing_backing = dashboard_client.post("/vitastor/backup/propose", json={
        "cluster_id": cluster_id, "method": "incremental_qcow2", "image": "vm-100",
        "snapshot": "daily", "destination": "/backup/daily.qcow2", "backing_file": "",
    })
    assert missing_backing.status_code == 400
