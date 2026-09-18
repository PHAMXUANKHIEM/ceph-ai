from types import SimpleNamespace

import dashboard.routes.block_storage as block_storage_route
from watcher.ceph_client import CephQueryError


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def test_unauthenticated_block_storage_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/block-storage", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_block_storage_lists_name_pool_namespace_and_size(dashboard_client, monkeypatch):
    monkeypatch.setattr(block_storage_route, "_query_block_storage", lambda cluster: [
        {"name": "volume-a", "pool": "volumes", "namespace": "openstack", "size_bytes": 10737418240, "size": "10.0 GiB", "used_size_bytes": 3221225472, "used_size": "3.0 GiB", "used_percent": 30.0},
        {"name": "image-b", "pool": "images", "namespace": "", "size_bytes": 1073741824, "size": "1.0 GiB", "used_size_bytes": 0, "used_size": "0 B", "used_percent": 0.0},
    ])
    _login(dashboard_client)

    response = dashboard_client.get("/block-storage")

    assert response.status_code == 200
    assert "Block Storage Control Plane" not in response.text
    assert 'id="block-storage-search"' in response.text
    assert 'data-name="volume-a"' in response.text
    assert 'data-pool="volumes"' in response.text
    assert 'data-namespace="openstack"' in response.text
    assert "/static/block_storage.js" in response.text
    assert 'id="block-storage-create-form"' in response.text
    assert 'name="pool"' in response.text
    for heading in ("Name", "Pool", "Namespace", "Cấp phát", "Đã dùng", "Tỷ lệ dùng"):
        assert f">{heading}<" in response.text
    assert "volume-a" in response.text
    assert "openstack" in response.text
    assert "10.0 GiB" in response.text
    assert "3.0 GiB" in response.text
    assert "30.0%" in response.text
    assert "Default" in response.text
    assert response.text.count("block-storage-trash-btn") == 1
    assert "Namespace riêng — chưa hỗ trợ" in response.text


def test_block_storage_overview_paginates_ten_volumes_per_page(dashboard_client, monkeypatch):
    monkeypatch.setattr(block_storage_route, "_query_block_storage", lambda cluster: [
        {"name": f"volume-{index:02d}", "pool": "volumes", "namespace": "",
         "size_bytes": 1024, "size": "1.0 KiB", "used_size_bytes": 512,
         "used_size": "512 B", "used_percent": 50.0}
        for index in range(12)
    ])
    _login(dashboard_client)

    first = dashboard_client.get("/block-storage")
    second = dashboard_client.get("/block-storage?page=2")

    assert first.status_code == 200
    assert first.text.count('class="block-storage-image-row"') == 10
    assert 'data-name="volume-09"' in first.text
    assert '/volumes/volumes/volume-09' in first.text
    assert 'data-name="volume-10"' not in first.text
    assert "Trang 1 / 2" in first.text
    assert "page=2" in first.text
    assert ">12 images<" in first.text
    assert second.status_code == 200
    assert second.text.count('class="block-storage-image-row"') == 2
    assert 'data-name="volume-10"' in second.text
    assert 'data-name="volume-11"' in second.text
    assert 'data-name="volume-00"' not in second.text
    assert "Trang 2 / 2" in second.text
    assert "page=1" in second.text


def test_block_storage_reuses_cluster_inventory_cache(dashboard_client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        block_storage_route,
        "_query_block_storage",
        lambda cluster: calls.append(cluster.id) or [{
            "name": "cached-volume", "pool": "volumes", "namespace": "",
            "size_bytes": 1024, "size": "1.0 KiB", "used_size_bytes": 512,
            "used_size": "512 B", "used_percent": 50.0,
        }],
    )
    _login(dashboard_client)

    first = dashboard_client.get("/block-storage")
    second = dashboard_client.get("/block-storage")

    assert first.status_code == second.status_code == 200
    assert "cached-volume" in first.text + second.text
    assert len(calls) == 1


def test_query_block_storage_discovers_rbd_pools_and_namespaces(monkeypatch):
    commands = []

    def fake_query(*args):
        command = args[-1]
        commands.append(command)
        if command == "ceph osd pool ls detail":
            return "mon1", [
                {"pool_name": "volumes", "application_metadata": {"rbd": {}}},
                {"pool_name": "logs", "application_metadata": {}},
            ]
        if command == "rbd namespace list --pool volumes":
            return "mon1", ["openstack"]
        if command == "rbd ls --long --pool volumes":
            return "mon1", [{"image": "base", "id": "abc123", "size": 1024, "format": 2},
                             {"image": "base", "id": "abc123", "snapshot": "snap", "size": 1024, "format": 2}]
        if command == "rbd ls --long --pool volumes --namespace openstack":
            return "mon1", [{"name": "vm-1", "size": 2147483648}]
        if command == "rbd du --pool volumes":
            return "mon1", {"images": [{"name": "base", "provisioned_size": 1024, "used_size": 512}]}
        if command == "rbd du --pool volumes --namespace openstack":
            return "mon1", {"images": [{"name": "vm-1", "provisioned_size": 2147483648, "used_size": 1073741824}]}
        raise AssertionError(command)

    monkeypatch.setattr(block_storage_route, "cluster_connection", lambda cluster: (["mon1"], "", "root", "/key", "none"))
    monkeypatch.setattr(block_storage_route, "run_ceph_json_command_with", fake_query)

    rows = block_storage_route._query_block_storage(SimpleNamespace())

    assert [(row["name"], row["pool"], row["namespace"], row["size"], row["used_size"], row["used_percent"]) for row in rows] == [
        ("base", "volumes", "", "1.0 KiB", "512 B", 50.0),
        ("vm-1", "volumes", "openstack", "2.0 GiB", "1.0 GiB", 50.0),
    ]
    assert not any("logs" in command for command in commands[1:])


def test_block_storage_shows_cluster_error(dashboard_client, monkeypatch):
    def fail(cluster):
        raise CephQueryError("MON unavailable")

    monkeypatch.setattr(block_storage_route, "_query_block_storage", fail)
    _login(dashboard_client)

    response = dashboard_client.get("/block-storage")

    assert response.status_code == 200
    assert "Không tải được Block Storage" in response.text
    assert "MON unavailable" in response.text


def test_persistent_block_storage_fallback_preserves_empty_pools(monkeypatch):
    cluster = SimpleNamespace(id="cluster-a")
    monkeypatch.setattr(block_storage_route, "_uses_mocked_ceph_client", lambda: False)
    monkeypatch.setattr(
        block_storage_route,
        "get_persisted_cache",
        lambda *args, **kwargs: ({
            "rows": [{"name": "disk", "pool": "volumes"}],
            "pools": ["empty-pool", "volumes"],
        }, 1.0),
    )

    inventory = block_storage_route._persistent_block_storage_fallback(cluster)

    assert inventory.pools == ["empty-pool", "volumes"]
    assert inventory[0]["name"] == "disk"


def test_load_block_storage_persists_pool_metadata(monkeypatch):
    cluster = SimpleNamespace(id="cluster-a")
    inventory = block_storage_route.BlockStorageInventory(
        [{"name": "disk", "pool": "volumes"}], pools=["empty-pool", "volumes"]
    )
    stored = {}
    monkeypatch.setattr(block_storage_route, "_query_block_storage", lambda _cluster: inventory)
    monkeypatch.setattr(block_storage_route, "_uses_mocked_ceph_client", lambda: False)
    monkeypatch.setattr(
        block_storage_route,
        "store_persisted_cache",
        lambda namespace, key, value: stored.update({"namespace": namespace, "key": key, "value": value}),
    )

    result = block_storage_route._load_block_storage(cluster)

    assert result is inventory
    assert stored["value"]["pools"] == ["empty-pool", "volumes"]
