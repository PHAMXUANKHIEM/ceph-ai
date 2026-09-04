from shared import db


def test_make_engine_bounds_postgres_pool_and_connect_timeout(monkeypatch):
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(db, "create_engine", fake_create_engine)

    db.make_engine("postgresql+psycopg://user:password@db.example/ceph_aiops")

    assert captured["connect_args"] == {"connect_timeout": 5}
    assert captured["pool_size"] == 1
    assert captured["max_overflow"] == 0
    assert captured["pool_timeout"] == 5
    assert captured["pool_pre_ping"] is True
