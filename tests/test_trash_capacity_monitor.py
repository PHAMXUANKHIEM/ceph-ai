from watcher import trash_capacity_monitor as monitor


def test_check_trash_capacity_aggregates_all_pools(monkeypatch):
    monkeypatch.setattr(monitor.ceph_client, "configured_rbd_pools", lambda: ["vms", "images"])
    monkeypatch.setattr(
        monitor.ceph_client,
        "query_rbd_trash",
        lambda pool: [{"size_bytes": 1000, "used_size_bytes": 15}] if pool == "vms" else [{"size_bytes": 2000, "used_size_bytes": 10}],
    )
    monkeypatch.setattr(
        monitor.ceph_client,
        "run_ceph_json_command",
        lambda command: ("mon-a", {"stats": {"total_bytes": 100}}),
    )

    result = monitor.check_trash_capacity()

    assert result["trash_bytes"] == 25
    assert result["ratio"] == 0.25
    assert result["entry_count"] == 2
    assert result["over_threshold"] is True
    assert result["usage_known"] is True


def test_check_trash_capacity_does_not_treat_unknown_usage_as_zero(monkeypatch):
    monkeypatch.setattr(monitor.ceph_client, "configured_rbd_pools", lambda: ["vms"])
    monkeypatch.setattr(
        monitor.ceph_client,
        "query_rbd_trash",
        lambda pool: [{"size_bytes": 10_000, "used_size_bytes": None}],
    )
    monkeypatch.setattr(
        monitor.ceph_client,
        "run_ceph_json_command",
        lambda command: ("mon-a", {"stats": {"total_bytes": 100}}),
    )

    result = monitor.check_trash_capacity()

    assert result["trash_bytes"] is None
    assert result["usage_known"] is False
    assert result["ratio"] == 0.0
    assert result["over_threshold"] is False


def test_alert_sent_only_when_crossing_above_twenty_percent(monkeypatch):
    states = iter(
        [
            {"trash_bytes": 21, "total_bytes": 100, "ratio": 0.21, "entry_count": 2, "over_threshold": True, "measurement_complete": True, "errors": []},
            {"trash_bytes": 22, "total_bytes": 100, "ratio": 0.22, "entry_count": 2, "over_threshold": True, "measurement_complete": True, "errors": []},
            {"trash_bytes": 10, "total_bytes": 100, "ratio": 0.10, "entry_count": 1, "over_threshold": False, "measurement_complete": True, "errors": []},
            {"trash_bytes": 30, "total_bytes": 100, "ratio": 0.30, "entry_count": 3, "over_threshold": True, "measurement_complete": True, "errors": []},
        ]
    )
    monkeypatch.setattr(monitor, "check_trash_capacity", lambda: next(states))
    alerts = []
    monkeypatch.setattr(
        monitor, "send_trash_capacity_alert", lambda *args: alerts.append(args) or True
    )
    monkeypatch.setattr(monitor, "_was_over_threshold", False)

    for _ in range(4):
        monitor.check_and_alert()

    assert alerts == [(21, 100, 0.21, 2), (30, 100, 0.30, 3)]


def test_incomplete_pool_scan_never_reports_under_threshold(monkeypatch):
    monkeypatch.setattr(monitor.ceph_client, "configured_rbd_pools", lambda: ["vms", "images"])

    def query(pool):
        if pool == "images":
            raise monitor.ceph_client.CephQueryError("MON busy")
        return [{"used_size_bytes": 90}]

    monkeypatch.setattr(monitor.ceph_client, "query_rbd_trash", query)
    monkeypatch.setattr(
        monitor.ceph_client,
        "run_ceph_json_command",
        lambda command: ("mon-a", {"stats": {"total_bytes": 100}}),
    )

    result = monitor.check_trash_capacity()

    assert result["measurement_complete"] is False
    assert result["over_threshold"] is False
    assert result["ratio"] == 0.9
    assert any("images" in error for error in result["errors"])


def test_failed_telegram_delivery_is_retried(monkeypatch):
    states = iter(
        [
            {"trash_bytes": 21, "total_bytes": 100, "ratio": 0.21, "entry_count": 1,
             "measurement_complete": True, "over_threshold": True, "errors": []},
            {"trash_bytes": 22, "total_bytes": 100, "ratio": 0.22, "entry_count": 1,
             "measurement_complete": True, "over_threshold": True, "errors": []},
        ]
    )
    monkeypatch.setattr(monitor, "check_trash_capacity", lambda: next(states))
    deliveries = iter([False, True])
    attempts = []
    monkeypatch.setattr(
        monitor,
        "send_trash_capacity_alert",
        lambda *args: attempts.append(args) or next(deliveries),
    )
    monkeypatch.setattr(monitor, "_was_over_threshold", False)

    monitor.check_and_alert()
    monitor.check_and_alert()

    assert len(attempts) == 2
    assert monitor._was_over_threshold is True


def test_saved_snapshot_supplies_the_allocated_bytes_ceph_never_reports(monkeypatch):
    """`rbd trash ls`/`rbd info` không có số byte đã cấp phát của image trong
    Trash, nên ceph_client luôn trả `used_size_bytes=None`. Trước đây điều đó
    làm mọi lần quét thành incomplete và ngưỡng 20% không bao giờ kích hoạt."""
    monkeypatch.setattr(monitor.ceph_client, "configured_rbd_pools", lambda: ["vms"])
    monkeypatch.setattr(
        monitor.ceph_client, "query_rbd_trash",
        lambda pool, *, include_capacity=True: [{"id": "aaa", "used_size_bytes": None}],
    )
    monkeypatch.setattr(monitor, "_saved_usage_by_trash_id", lambda cluster, pool: {"aaa": 30})
    monkeypatch.setattr(
        monitor.ceph_client, "run_ceph_json_command",
        lambda command: ("mon-a", {"stats": {"total_bytes": 100}}),
    )

    result = monitor.check_trash_capacity()

    assert result["trash_bytes"] == 30
    assert result["ratio"] == 0.30
    assert result["usage_known"] is True
    assert result["measurement_complete"] is True
    assert result["over_threshold"] is True


def test_entry_with_no_saved_snapshot_still_fails_closed(monkeypatch):
    monkeypatch.setattr(monitor.ceph_client, "configured_rbd_pools", lambda: ["vms"])
    monkeypatch.setattr(
        monitor.ceph_client, "query_rbd_trash",
        lambda pool, *, include_capacity=True: [{"id": "aaa", "used_size_bytes": None}],
    )
    monkeypatch.setattr(monitor, "_saved_usage_by_trash_id", lambda cluster, pool: {})
    monkeypatch.setattr(
        monitor.ceph_client, "run_ceph_json_command",
        lambda command: ("mon-a", {"stats": {"total_bytes": 100}}),
    )

    result = monitor.check_trash_capacity()

    assert result["trash_bytes"] is None
    assert result["usage_known"] is False
    assert result["over_threshold"] is False


def test_monitor_asks_for_the_measured_listing(monkeypatch):
    """Ratio là toàn bộ lý do module này tồn tại, nên nó phải lấy số byte đã
    cấp phát đo thật, không phải bản liệt kê nhanh bỏ trống capacity."""
    monkeypatch.setattr(monitor.ceph_client, "configured_rbd_pools", lambda: ["vms"])
    seen = {}

    def query(pool, *, include_capacity=True):
        seen["include_capacity"] = include_capacity
        return [{"id": "aaa", "used_size_bytes": 10}]

    monkeypatch.setattr(monitor.ceph_client, "query_rbd_trash", query)
    monkeypatch.setattr(
        monitor.ceph_client,
        "run_ceph_json_command",
        lambda command: ("mon-a", {"stats": {"total_bytes": 100}}),
    )

    result = monitor.check_trash_capacity()

    assert seen["include_capacity"] is True
    assert result["trash_bytes"] == 10
    assert result["usage_known"] is True
