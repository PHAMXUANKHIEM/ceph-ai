from shared.ai_redaction import redact_text


def test_natural_language_provider_payload_redacts_secret_material():
    payload = (
        'cluster=cluster-a password="dont-send-this" '
        'Authorization: Bearer abc.def.ghi '
        'keyring=client.admin secret_key=top-secret'
    )
    redacted = redact_text(payload)
    assert "dont-send-this" not in redacted
    assert "abc.def.ghi" not in redacted
    assert "top-secret" not in redacted
    assert "cluster-a" in redacted
