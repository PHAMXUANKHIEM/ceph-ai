from types import SimpleNamespace

import dashboard.routes.volumes as volumes_route


VOLUME_IMAGE = "volume-12345678-1234-4123-8123-1234567890ab"


def test_boot_dependencies_api_is_read_only_and_cluster_scoped(dashboard_client, monkeypatch):
    cluster = SimpleNamespace(id="cluster-boot", is_default=False)
    monkeypatch.setattr(volumes_route, "_allowed_pools_for_request", lambda _request: (cluster, {"images"}))
    monkeypatch.setattr(
        volumes_route,
        "discover_cinder_volume",
        lambda _cluster, _image: {
            "status": "managed", "verified": True, "volume_id": "12345678-1234-4123-8123-1234567890ab",
            "bootable": True, "project_id": "project-1",
            "image_metadata": {"image_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
            "attachments": [{"instance_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}],
        },
    )
    monkeypatch.setattr(
        volumes_route,
        "discover_cinder_snapshots",
        lambda *_args: {"status": "ok", "items": [{"snapshot_id": "snap-1", "name": "daily", "status": "available"}]},
    )
    monkeypatch.setattr(
        volumes_route,
        "discover_cinder_server",
        lambda *_args: {
            "status": "ok", "server_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "image": {"image_id": None}, "volumes_attached": [{"volume_id": "12345678-1234-4123-8123-1234567890ab"}],
        },
    )
    monkeypatch.setattr(
        volumes_route,
        "discover_glance_image",
        lambda *_args: {"status": "ok", "image_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "name": "ubuntu"},
    )
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    response = dashboard_client.get(f"/api/volumes/images/inventory/{VOLUME_IMAGE}/boot-dependencies")

    assert response.status_code == 200
    body = response.json()
    assert body["cluster_id"] == "cluster-boot"
    assert body["boot_volume"]["source"] == "boot_from_volume"
    assert body["image_service"]["name"] == "ubuntu"
    assert body["guards"]["protect_boot_volume"] is True
    assert body["mutation_supported"] is False


def test_boot_dependencies_api_rejects_unconfigured_pool(dashboard_client, monkeypatch):
    cluster = SimpleNamespace(id="cluster-boot", is_default=False)
    monkeypatch.setattr(volumes_route, "_allowed_pools_for_request", lambda _request: (cluster, {"images"}))
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    response = dashboard_client.get(f"/api/volumes/unknown/inventory/{VOLUME_IMAGE}/boot-dependencies")

    assert response.status_code == 404
