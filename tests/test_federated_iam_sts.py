from datetime import timedelta

from dashboard.routes import federated_iam
from shared import db
from shared.models import RgwFederatedIdentityProvider, RgwFederatedRoleMapping, RgwFederatedStsSession
from shared.time import utc_now


def _login(client):
    response = client.post("/login", data={"username": "admin", "password": "admin", "product": "ceph"})
    assert response.status_code == 200


def test_sts_issue_does_not_persist_returned_secrets(dashboard_client, monkeypatch):
    _login(dashboard_client)
    provider_response = dashboard_client.post(
        "/api/settings/federated-iam/providers",
        json={
            "name": "sts-oidc",
            "provider_type": "oidc",
            "issuer_url": "https://sso.example.test/realms/ceph",
            "secret_ref": "vault:secret/oidc#client_secret",
            "config": {"sts_endpoint": "https://rgw.example.test:8443", "sts_tls_verify": True},
        },
    )
    assert provider_response.status_code == 200
    provider_id = provider_response.json()["provider_id"]
    with db.SessionLocal() as session:
        provider = session.get(RgwFederatedIdentityProvider, provider_id)
        provider.status = "APPLIED"
        provider.enabled = True
        mapping = RgwFederatedRoleMapping(
            provider_id=provider_id,
            name="sts-readonly",
            source_type="claim",
            source_key="groups",
            match_value="storage-readonly",
            policy_json='{"Statement":[{"Action":["s3:GetObject"],"Effect":"Allow","Resource":["*"]}],"Version":"2012-10-17"}',
            status="RECONCILED",
            enabled=True,
            rgw_role_name="ceph-ai-sts-readonly",
            created_by="admin",
        )
        session.add(mapping)
        session.commit()
        mapping_id = mapping.id

    expected_expiration = (utc_now() + timedelta(hours=1)).isoformat()
    captured = {}

    def fake_issue(**kwargs):
        captured.update(kwargs)
        return {
            "access_key_id": "ASIAEXAMPLE",
            "secret_access_key": "returned-secret",
            "session_token": "returned-session-token",
            "expiration": expected_expiration,
        }

    monkeypatch.setattr(federated_iam, "assume_role_with_web_identity", fake_issue)
    response = dashboard_client.post(
        "/api/settings/federated-iam/sts/sessions",
        json={
            "mapping_id": mapping_id,
            "session_name": "web-console",
            "duration_seconds": 3600,
            "session_tags": {"team": "storage"},
            "web_identity_token": "oidc-token-must-not-persist",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["credentials"]["secret_access_key"] == "returned-secret"
    assert body["session"]["status"] == "ACTIVE"
    assert captured["web_identity_token"] == "oidc-token-must-not-persist"
    with db.SessionLocal() as session:
        row = session.get(RgwFederatedStsSession, body["session"]["id"])
        audits = session.query(federated_iam.RgwFederatedIdentityAudit).all()
        assert row.access_key_id == "ASIAEXAMPLE"
        assert "returned-secret" not in str(row.__dict__)
        assert "oidc-token-must-not-persist" not in " ".join(str(a.evidence_json) + str(a.error_message) for a in audits)

    revoke = dashboard_client.post(
        f"/api/settings/federated-iam/sts/sessions/{body['session']['id']}/revoke",
        json={"confirmation": body["session"]["id"]},
    )
    assert revoke.status_code == 200
    assert revoke.json()["session"]["status"] == "REVOKED"


def test_sts_preview_requires_reconciled_mapping(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/api/settings/federated-iam/sts/sessions/preview",
        json={
            "mapping_id": "missing-mapping",
            "session_name": "preview-session",
            "duration_seconds": 900,
            "session_tags": {},
            "web_identity_token": "token",
        },
    )
    assert response.status_code == 404


def test_federated_iam_page_exposes_sts_controls(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/settings/federated-iam")
    assert response.status_code == 200
    assert "STS Temporary Credentials" in response.text
    assert "Cấp temporary credentials" in response.text
