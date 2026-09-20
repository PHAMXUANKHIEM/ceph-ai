from types import SimpleNamespace

import dashboard.routes.volumes as volumes_route


def test_cinder_mapping_api_is_bounded_and_cluster_scoped(dashboard_client, monkeypatch):
    cluster = SimpleNamespace(id="cluster-cinder", is_default=False)
    monkeypatch.setattr(
        volumes_route, "_allowed_pools_for_request",
        lambda _request: (cluster, {"images"}),
    )
    monkeypatch.setattr(
        volumes_route, "_cached_rbd_inventory_with_state",
        lambda _cluster, _pool: ([
            {"name": "volume-12345678-1234-4123-8123-1234567890ab", "image_id": "v1",
             "provisioned_size": 20, "used_size": 10},
            {"name": "volume-abcdefab-1234-4123-8123-1234567890ab", "image_id": "v2",
             "provisioned_size": 20, "used_size": 0},
        ], {"stale": False, "source": "test", "age_seconds": 0, "collected_at": "2026-09-20T10:00:00Z"}),
    )
    monkeypatch.setattr(
        volumes_route, "discover_cinder_volume",
        lambda _cluster, image: (
            {"status": "managed", "verified": True, "volume_id": "v1", "project_id": "project-1",
             "attachments": []}
            if image.startswith("volume-1234")
            else {"status": "not_found", "verified": True, "volume_id": "v2"}
        ),
    )
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    response = dashboard_client.get("/api/volumes/images/cinder-mapping?page_size=20")

    assert response.status_code == 200
    body = response.json()
    assert body["cluster_id"] == "cluster-cinder"
    assert body["summary"]["managed"] == 1
    assert body["summary"]["orphan"] == 1
    assert all(item["read_only"] for item in body["items"])
    assert body["items"][1]["management_source"] == "none"
    assert body["items"][1]["mutation_supported"] is False
