"""/healthz for checks from outside the host (plan SM4)."""

from dashboard.routes import system_health


def _status(unhealthy=()):
    return lambda name: {"healthy": name not in unhealthy, "pid": 4242, "pid_namespace": "pid:[1]"}


def test_healthz_needs_no_login_and_reveals_no_detail(dashboard_client, monkeypatch):
    monkeypatch.setattr(system_health, "status", _status())
    monkeypatch.setattr(system_health, "_database_answers", lambda: True)

    response = dashboard_client.get("/healthz", follow_redirects=False)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "failing": []}
    assert "4242" not in response.text and "pid" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_healthz_names_what_is_failing_with_503(dashboard_client, monkeypatch):
    monkeypatch.setattr(system_health, "status", _status(unhealthy=("worker",)))
    monkeypatch.setattr(system_health, "_database_answers", lambda: False)

    response = dashboard_client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "failing": ["worker", "database"]}


def test_database_check_reports_false_instead_of_raising(monkeypatch):
    class Broken:
        def __enter__(self):
            raise OSError("connection refused")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(system_health.db, "SessionLocal", Broken)

    assert system_health._database_answers() is False
