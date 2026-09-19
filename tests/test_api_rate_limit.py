from datetime import datetime, timedelta

from sqlalchemy.orm import sessionmaker

from shared import db as db_module
from shared.api_rate_limit import allow_api_request
from shared.models import ApiRateLimit


def test_api_rate_limit_is_shared_and_resets_after_window(monkeypatch, db_session):
    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False),
    )
    start = datetime(2026, 9, 19, 10, 0, 0)

    assert allow_api_request("10.3.55.10", limit=2, window_seconds=60, now=start)
    assert allow_api_request(
        "10.3.55.10", limit=2, window_seconds=60, now=start + timedelta(seconds=1)
    )
    assert not allow_api_request(
        "10.3.55.10", limit=2, window_seconds=60, now=start + timedelta(seconds=2)
    )

    with db_session.bind.connect() as connection:
        assert connection.execute(
            ApiRateLimit.__table__.select().where(ApiRateLimit.client_key == "10.3.55.10")
        ).first().request_count == 2

    assert allow_api_request(
        "10.3.55.10", limit=2, window_seconds=60, now=start + timedelta(seconds=61)
    )
