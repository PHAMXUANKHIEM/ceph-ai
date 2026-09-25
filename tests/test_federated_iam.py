from dashboard.routes import federated_iam


def _login(client):
    response = client.post(
        "/login", data={"username": "admin", "password": "admin", "product": "ceph"}
    )
    # TestClient follows the successful 303 redirect to the dashboard by
    # default, so a successful login is observed as 200 here.
    assert response.status_code == 200
    assert response.url.path == "/"


def _oidc_payload(**overrides):
    payload = {
        "name": "corp-oidc",
        "provider_type": "oidc",
        "issuer_url": "https://sso.example.test/realms/ceph",
        "audience": "ceph-rgw",
        "secret_ref": "vault:secret/ceph/oidc",
        "config": {"groups_claim": "groups", "tls_verify": True},
    }
    payload.update(overrides)
    return payload


def test_preview_rejects_inline_secret(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/api/settings/federated-iam/providers/preview",
        json=_oidc_payload(config={"client_secret": "must-not-be-stored"}),
    )
    assert response.status_code == 400
    assert "secret_ref" in response.json()["detail"]


def test_provider_registry_create_is_secret_safe(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/api/settings/federated-iam/providers", json=_oidc_payload()
    )
    assert response.status_code == 200
    provider_id = response.json()["provider_id"]
    listed = dashboard_client.get("/api/settings/federated-iam/providers")
    assert listed.status_code == 200
    provider = next(item for item in listed.json()["items"] if item["id"] == provider_id)
    assert provider["secret_configured"] is True
    assert "vault:secret" not in str(provider)
    assert "client_secret" not in str(provider)
    assert provider["status"] == "DRAFT"


def test_oidc_validation_records_evidence(dashboard_client, monkeypatch):
    _login(dashboard_client)
    created = dashboard_client.post(
        "/api/settings/federated-iam/providers", json=_oidc_payload(name="validate-oidc")
    )
    provider_id = created.json()["provider_id"]

    async def fake_validate(row):
        return {"kind": "oidc_discovery", "issuer": row.issuer_url, "jwks_uri": "https://sso.test/jwks"}

    monkeypatch.setattr(federated_iam, "_validate_provider", fake_validate)
    validated = dashboard_client.post(
        f"/api/settings/federated-iam/providers/{provider_id}/validate"
    )
    assert validated.status_code == 200
    assert validated.json()["provider"]["status"] == "VALID"

    applied = dashboard_client.post(
        f"/api/settings/federated-iam/providers/{provider_id}/apply",
        json={"confirmation": "validate-oidc"},
    )
    assert applied.status_code == 200
    assert applied.json()["provider"]["status"] == "APPLIED"
    assert applied.json()["provider"]["enabled"] is True


def test_role_mapping_preview_and_register_requires_applied_provider(dashboard_client, monkeypatch):
    _login(dashboard_client)
    created = dashboard_client.post(
        "/api/settings/federated-iam/providers", json=_oidc_payload(name="mapping-oidc")
    )
    provider_id = created.json()["provider_id"]
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": ["arn:aws:s3:::team-data/*"],
        }],
    }
    payload = {
        "name": "engineering-readonly",
        "provider_id": provider_id,
        "source_type": "group",
        "source_key": "groups",
        "match_value": "engineering",
        "policy": policy,
    }
    blocked = dashboard_client.post(
        "/api/settings/federated-iam/mappings/preview", json=payload
    )
    assert blocked.status_code == 200
    assert blocked.json()["mapping"]["policy_sha256"]
    draft = dashboard_client.post("/api/settings/federated-iam/mappings", json=payload)
    assert draft.status_code == 200
    mapping_id = draft.json()["mapping_id"]
    rejected = dashboard_client.post(
        f"/api/settings/federated-iam/mappings/{mapping_id}/register",
        json={"confirmation": payload["name"]},
    )
    assert rejected.status_code == 409

    async def fake_validate(row):
        return {"kind": "oidc_discovery", "issuer": row.issuer_url, "jwks_uri": "https://sso.test/jwks"}

    monkeypatch.setattr(federated_iam, "_validate_provider", fake_validate)
    dashboard_client.post(f"/api/settings/federated-iam/providers/{provider_id}/validate")
    dashboard_client.post(
        f"/api/settings/federated-iam/providers/{provider_id}/apply",
        json={"confirmation": "mapping-oidc"},
    )
    registered = dashboard_client.post(
        f"/api/settings/federated-iam/mappings/{mapping_id}/register",
        json={"confirmation": payload["name"]},
    )
    assert registered.status_code == 200
    assert registered.json()["mapping"]["status"] == "REGISTERED"
    assert registered.json()["reconciliation"]["status"] == "QUEUED"
