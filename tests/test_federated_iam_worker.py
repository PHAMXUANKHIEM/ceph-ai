import json

from shared import db
from shared.models import (
    RgwFederatedIdentityProvider,
    RgwFederatedRoleMapping,
)
from worker import federated_iam


def _rows():
    provider = RgwFederatedIdentityProvider(
        name="worker-oidc",
        provider_type="oidc",
        status="APPLIED",
        enabled=True,
        issuer_url="https://sso.example.test/realms/ceph",
        audience="ceph-rgw",
        created_by="admin",
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": ["arn:aws:s3:::team-data/*"],
        }],
    }
    mapping = RgwFederatedRoleMapping(
        provider_id=provider.id,
        name="engineering-readonly",
        source_type="group",
        source_key="groups",
        match_value="engineering",
        policy_json=json.dumps(policy, sort_keys=True),
        status="REGISTERED",
        created_by="admin",
    )
    return provider, mapping, policy


def test_build_reconcile_command_quotes_documents_and_wraps_container():
    provider, mapping, _policy = _rows()
    command, role_name, policy_name = federated_iam.build_reconcile_command(
        provider, mapping, exec_mode="podman", container_name="ceph-rgw-a",
    )
    assert role_name == "ceph-ai-engineering-readonly"
    assert policy_name == role_name
    assert "podman exec ceph-rgw-a sh -lc" in command
    assert "radosgw-admin oidc-provider" in command
    assert "role-policy put" in command
    assert "AssumeRoleWithWebIdentity" in command


def test_reconcile_registered_mapping_updates_state(dashboard_client, monkeypatch):
    provider, mapping, policy = _rows()
    with db.SessionLocal() as session:
        session.add(provider)
        session.flush()
        mapping.provider_id = provider.id
        session.add(mapping)
        session.commit()
        mapping_id = mapping.id

    monkeypatch.setattr(
        federated_iam,
        "configured_nodes",
        lambda _cluster: [{"host": "10.20.1.10", "roles": ["RGW"]}],
    )
    monkeypatch.setattr(
        federated_iam,
        "resolve_ssh_creds",
        lambda _cluster: ("ceph", "/tmp/key", "none", ""),
    )
    monkeypatch.setattr(
        federated_iam,
        "execute_command",
        lambda host, command, user=None, key_path=None: json.dumps(policy),
    )

    assert federated_iam.reconcile_registered_mappings_once() == 1
    with db.SessionLocal() as session:
        row = session.get(RgwFederatedRoleMapping, mapping_id)
        assert row.status == "RECONCILED"
        assert row.enabled is True
        assert row.rgw_role_name == "ceph-ai-engineering-readonly"
        assert row.last_reconcile_error is None
