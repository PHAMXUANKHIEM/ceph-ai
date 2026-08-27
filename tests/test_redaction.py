from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db as db_module
from shared.db import Base
from shared.models import Incident, IncidentStatus
from worker.redaction import (
    REDACTED,
    NoOpRedactor,
    Redactor,
    SensitiveDataRedactor,
    default_redactor,
)

SAMPLE_PAYLOAD = {
    "schema_version": "1.0",
    "incident_id": "abc-123",
    "ceph_code": "MON_CLOCK_SKEW",
    "detected_at": "2026-07-16T10:00:00",
    "nodes": ["10.20.1.249", "khiempx-mon2"],
    "log_excerpt": "mon2 clock skew log — host khiempx-mon2, ip 10.20.1.249",
    "cluster_snapshot": {"status": "HEALTH_WARN", "checks": {"MON_CLOCK_SKEW": {}}},
}


def test_noop_redactor_returns_same_payload_unchanged():
    redactor = NoOpRedactor()

    result = redactor.redact(SAMPLE_PAYLOAD)

    assert result == SAMPLE_PAYLOAD
    assert result is SAMPLE_PAYLOAD  # no copy — v1 has no transform to justify one


def test_noop_redactor_handles_empty_payload():
    redactor = NoOpRedactor()

    assert redactor.redact({}) == {}


def test_noop_redactor_preserves_nested_structures():
    redactor = NoOpRedactor()
    payload = {"nested": {"a": [1, 2, {"b": "c"}]}}

    result = redactor.redact(payload)

    assert result == payload
    assert result is payload


def test_noop_redactor_preserves_cluster_snapshot_field_unchanged():
    redactor = NoOpRedactor()

    result = redactor.redact(SAMPLE_PAYLOAD)

    assert result["cluster_snapshot"] == SAMPLE_PAYLOAD["cluster_snapshot"]
    assert result["cluster_snapshot"] is SAMPLE_PAYLOAD["cluster_snapshot"]


@pytest.mark.parametrize("odd_payload", [None, "a string", 123, ["a", "list"]])
def test_noop_redactor_passes_through_non_dict_payload_unchanged(odd_payload):
    # NoOpRedactor validates nothing (v1 design) — this locks in that its
    # passthrough behavior holds even for input that violates the `dict`
    # type hint, since Python doesn't enforce it at runtime.
    redactor = NoOpRedactor()

    assert redactor.redact(odd_payload) is odd_payload


def test_noop_redactor_satisfies_redactor_protocol_via_duck_typing():
    redactor = NoOpRedactor()

    assert hasattr(redactor, "redact")
    assert callable(redactor.redact)
    assert redactor.redact({"x": 1}) == {"x": 1}


def test_noop_redactor_satisfies_redactor_protocol_via_isinstance():
    redactor = NoOpRedactor()

    assert isinstance(redactor, Redactor)


def test_default_redactor_is_sensitive_and_does_not_mutate_input():
    payload = {**SAMPLE_PAYLOAD, "api_key": "top-secret"}

    result = default_redactor.redact(payload)

    assert isinstance(default_redactor, SensitiveDataRedactor)
    assert result is not payload
    assert result["api_key"] == REDACTED
    assert payload["api_key"] == "top-secret"


def test_sensitive_redactor_scrubs_nested_secret_fields_and_text():
    payload = {
        "password": "db-password",
        "nested": [{"telegram_bot_token": "bot-secret"}],
        "log_excerpt": (
            "Authorization: Bearer abc.def.ghi\n"
            "ceph key=AQBabcdefghijklmnopqrstuvwxyz123456== password=hunter2"
        ),
    }

    result = SensitiveDataRedactor().redact(payload)

    assert result["password"] == REDACTED
    assert result["nested"][0]["telegram_bot_token"] == REDACTED
    assert "abc.def.ghi" not in result["log_excerpt"]
    assert "AQBabcdefghijklmnopqrstuvwxyz123456==" not in result["log_excerpt"]
    assert "hunter2" not in result["log_excerpt"]


def test_sensitive_redactor_scrubs_url_credentials_presigned_url_and_private_key():
    payload = {
        "url": "https://admin:s3cret@example.test/path?X-Amz-Signature=abcdef123",
        "output": "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----",
    }

    result = SensitiveDataRedactor().redact(payload)

    rendered = repr(result)
    assert "s3cret" not in rendered
    assert "abcdef123" not in rendered
    assert "abc123" not in rendered
    assert REDACTED in rendered


@pytest.mark.parametrize(
    "value,secrets",
    [
        ('{"password": "super secret", "apiKey": "camel-secret"}', ("super secret", "camel-secret")),
        ("Cookie: sessionid=cookie-secret; csrftoken=csrf-secret", ("cookie-secret", "csrf-secret")),
        ("aws_access_key_id=ASIA1234567890ABCDEF", ("ASIA1234567890ABCDEF",)),
        ("PASSWORD='secret with spaces'", ("secret with spaces", "with spaces")),
    ],
)
def test_sensitive_redactor_scrubs_review_regressions(value, secrets):
    result = SensitiveDataRedactor().redact({"text": value})["text"]
    assert REDACTED in result
    assert all(secret not in result for secret in secrets)


def test_sensitive_redactor_scrubs_camel_case_and_rejects_unsupported_nested_types():
    result = SensitiveDataRedactor().redact({"telegramBotToken": "secret", "accessKeyId": "value"})
    assert result == {"telegramBotToken": REDACTED, "accessKeyId": REDACTED}
    with pytest.raises(TypeError):
        SensitiveDataRedactor().redact({"unsafe": {"secret-in-a-set"}})


def test_sensitive_redactor_preserves_operational_evidence():
    result = SensitiveDataRedactor().redact(SAMPLE_PAYLOAD)

    assert result["ceph_code"] == "MON_CLOCK_SKEW"
    assert result["nodes"] == ["10.20.1.249", "khiempx-mon2"]
    assert result["cluster_snapshot"] == SAMPLE_PAYLOAD["cluster_snapshot"]


@pytest.mark.parametrize("invalid", [None, "text", 123, ["not", "a", "dict"]])
def test_sensitive_redactor_fails_closed_for_non_dict_payload(invalid):
    with pytest.raises(TypeError):
        SensitiveDataRedactor().redact(invalid)


@pytest.fixture()
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    yield engine


def test_incident_log_excerpt_is_stored_and_read_back_unredacted(isolated_db):
    # Regression guard for AC #2: worker/redaction/ is not imported anywhere
    # in the Watcher/DB/Dashboard path — this just confirms the DB round-trip
    # itself never redacts, independent of the (unwired) Redactor module.
    sensitive_excerpt = "mon2 clock skew log — host khiempx-mon2, ip 10.20.1.249"

    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id="incident-redaction-check",
                ceph_code="MON_CLOCK_SKEW",
                status=IncidentStatus.NEW.value,
                detected_at=datetime.utcnow(),
                log_excerpt=sensitive_excerpt,
            )
        )
        session.commit()

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-redaction-check")
        assert incident.log_excerpt == sensitive_excerpt
        assert "khiempx-mon2" in incident.log_excerpt
        assert "10.20.1.249" in incident.log_excerpt
