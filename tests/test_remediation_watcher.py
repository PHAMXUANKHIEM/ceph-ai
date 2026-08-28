from watcher import remediation_main
from config.settings import settings


def test_fast_watcher_polls_and_publishes_every_tick(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ai_remediation_lock_file", str(tmp_path / "remediation.lock"))
    health = {"status": "HEALTH_WARN", "checks": {"OSD_DOWN": {}}}
    calls = []
    class FakeSession:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def get(self, _model, _id):
            return type("Cluster", (), {"ceph_mon_nodes": "10.0.0.1,10.0.0.2"})()
    monkeypatch.setattr(remediation_main.db, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(remediation_main, "get_default_cluster_id", lambda _session: "cluster-1")
    monkeypatch.setattr(remediation_main, "resolve_ssh_creds", lambda cluster: ("root", "/key", "none", ""))
    health_calls = []
    monkeypatch.setattr(
        remediation_main.ceph_client, "query_cluster_health_with",
        lambda *args, **kwargs: health_calls.append((args, kwargs)) or health,
    )
    monkeypatch.setattr(remediation_main, "_resolve_recovered_incidents", lambda checks, **kw: calls.append(("resolve", checks, kw)))
    monkeypatch.setattr(remediation_main, "_reconcile_terminal_actions", lambda: calls.append(("reconcile",)))
    monkeypatch.setattr(remediation_main.verify, "verify_pending_incidents", lambda checks, **kw: calls.append(("verify", checks, kw)))
    monkeypatch.setattr(remediation_main, "build_and_publish_incident", lambda previous, payload, **kw: calls.append(("publish", previous, payload, kw)))
    monkeypatch.setattr(remediation_main.time, "sleep", lambda _seconds: None)

    remediation_main.run(max_iterations=2)

    assert [row[0] for row in calls] == ["resolve", "reconcile", "verify", "publish"] * 2
    assert calls[3][3]["cluster_id"] == "cluster-1"
    assert health_calls[0][0][0] == ["10.0.0.1", "10.0.0.2"]


def test_second_watcher_instance_refuses_to_run(monkeypatch, tmp_path):
    import fcntl

    lock_path = tmp_path / "remediation.lock"
    monkeypatch.setattr(settings, "ai_remediation_lock_file", str(lock_path))
    calls = []
    monkeypatch.setattr(remediation_main, "_run", lambda *_args, **_kwargs: calls.append("run"))
    with lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        remediation_main.run(max_iterations=1)
    assert calls == []
    assert lock_path.exists()
