from watcher import trash_capacity_monitor as monitor


def test_check_trash_capacity_aggregates_all_pools(monkeypatch):
    monkeypatch.setattr(monitor.ceph_client, "configured_rbd_pools", lambda: ["vms", "images"])
    monkeypatch.setattr(
        monitor.ceph_client,
        "query_rbd_trash",
        lambda pool: [{"size_bytes": 15}] if pool == "vms" else [{"size_bytes": 10}],
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


def test_alert_sent_only_when_crossing_above_twenty_percent(monkeypatch):
    states = iter(
        [
            {"trash_bytes": 21, "total_bytes": 100, "ratio": 0.21, "entry_count": 2, "over_threshold": True},
            {"trash_bytes": 22, "total_bytes": 100, "ratio": 0.22, "entry_count": 2, "over_threshold": True},
            {"trash_bytes": 10, "total_bytes": 100, "ratio": 0.10, "entry_count": 1, "over_threshold": False},
            {"trash_bytes": 30, "total_bytes": 100, "ratio": 0.30, "entry_count": 3, "over_threshold": True},
        ]
    )
    monkeypatch.setattr(monitor, "check_trash_capacity", lambda: next(states))
    alerts = []
    monkeypatch.setattr(monitor, "send_trash_capacity_alert", lambda *args: alerts.append(args))
    monkeypatch.setattr(monitor, "_was_over_threshold", False)

    for _ in range(4):
        monitor.check_and_alert()

    assert alerts == [(21, 100, 0.21, 2), (30, 100, 0.30, 3)]
