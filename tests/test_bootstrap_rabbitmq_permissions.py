import importlib.util
import re
import subprocess
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "bootstrap_container_config", Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_container_config.py"
)
bootstrap = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bootstrap)


def test_app_user_may_publish_through_the_default_exchange_only(monkeypatch):
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="ceph_ai\t[]\n", stderr="")

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(bootstrap, "_read_or_create_secret", lambda _path: "secret")
    monkeypatch.setattr(bootstrap, "set_key", lambda *_args, **_kwargs: None)

    bootstrap._configure_container_rabbitmq({"RABBITMQ_URL": "amqp://guest:guest@localhost:5672/"})

    grant = next(args for args in calls if "set_permissions" in args)
    configure, write, read = grant[-3:]
    # watcher/publisher.py publishes Incidents via channel.default_exchange.
    assert re.match(write, "amq.default")
    assert re.match(write, "incidents")
    assert not re.match(write, "amq.topic")
    assert not re.match(configure, "amq.default")
    assert not re.match(read, "amq.default")
