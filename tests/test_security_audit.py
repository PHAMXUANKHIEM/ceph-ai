from types import SimpleNamespace

from starlette.requests import Request
from starlette.responses import Response
from sqlalchemy.orm import sessionmaker

from shared import db as db_module
from shared.models import SecurityAuditEvent
from shared.security_audit import record_mutation


def test_mutation_audit_stores_metadata_without_body(monkeypatch, db_session):
    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/settings/save",
            "raw_path": b"/settings/save",
            "query_string": b"secret=password",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "session": {"user": "admin"},
        }
    )
    request.state.request_id = "request-123"

    record_mutation(request, Response(status_code=403))

    with db_module.SessionLocal() as session:
        row = session.query(SecurityAuditEvent).one()
        assert row.actor == "admin"
        assert row.method == "POST"
        assert row.path == "/settings/save"
        assert row.status_code == 403
        assert row.request_id == "request-123"
        assert "password" not in row.path
