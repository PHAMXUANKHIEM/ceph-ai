"""Vault monitor heartbeat and the compose healthcheck it feeds (plan SM2)."""


def test_heartbeat_is_recorded_while_the_monitor_sleeps(monkeypatch):
    from watcher import vault_monitor as module

    clock = {"now": 0.0}
    beats = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    monkeypatch.setattr(module.service_health, "record_safe", lambda name: beats.append((name, clock["now"])))

    module._sleep_with_heartbeat(40)

    assert [name for name, _ in beats] == ["vault-monitor"] * 4
    assert [at for _, at in beats] == [0.0, 15.0, 30.0, 40.0]  # never more than 15 s apart


def test_every_compose_service_has_a_healthcheck():
    from pathlib import Path

    import yaml

    services = yaml.safe_load((Path(__file__).resolve().parents[1] / "compose.yaml").read_text())["services"]
    assert [name for name, service in services.items() if "healthcheck" not in service] == []
