import pytest

from shared import ldap_identity


class _Entry:
    def __init__(self, dn, attrs=None):
        self.entry_dn = dn
        self.entry_attributes_as_dict = attrs or {}


class _Server:
    def __init__(self, host, port, use_ssl, tls, get_info, connect_timeout):
        self.host = host
        self.port = port
        self.ssl = use_ssl


class _Connection:
    def __init__(self, server, **kwargs):
        self.server = server
        self.result = {"message": ""}
        self.entries = []
        self.bound = False

    def bind(self):
        self.bound = True
        return True

    def search(self, _base_dn, search_filter, **_kwargs):
        if "objectClass=person" in search_filter:
            self.entries = [_Entry("uid=alice,ou=people,dc=example,dc=com")]
        else:
            self.entries = [
                _Entry("cn=platform,ou=groups,dc=example,dc=com", {"cn": ["platform"]}),
                _Entry("cn=storage,ou=groups,dc=example,dc=com", {"cn": ["storage"]}),
            ]
        return True

    def unbind(self):
        self.bound = False
        return True


def test_directory_bind_and_group_lookup_never_returns_password(monkeypatch):
    monkeypatch.setattr(ldap_identity, "Server", _Server)
    monkeypatch.setattr(ldap_identity, "Connection", _Connection)
    monkeypatch.setenv("CEPH_AI_LDAP_TEST_SECRET", "super-secret")
    evidence = ldap_identity.validate_directory_provider(
        "ad",
        "ldaps://ad.example.test:636",
        "env:CEPH_AI_LDAP_TEST_SECRET",
        '{"bind_dn":"svc@example.test","base_dn":"dc=example,dc=test",'
        '"probe_username":"alice","tls_verify":true}',
    )
    assert evidence["bind_verified"] is True
    assert evidence["user_lookup_verified"] is True
    assert evidence["groups"] == ["platform", "storage"]
    assert "super-secret" not in str(evidence)


def test_secret_ref_rejects_unapproved_file_path():
    with pytest.raises(ldap_identity.DirectoryValidationError, match="secret_ref"):
        ldap_identity.resolve_secret("file:/tmp/not-allowed")


def test_directory_requires_bind_secret():
    with pytest.raises(ldap_identity.DirectoryValidationError, match="secret_ref"):
        ldap_identity.validate_directory_provider(
            "ldap",
            "ldap://ldap.example.test:389",
            None,
            '{"bind_dn":"cn=svc,dc=example,dc=com"}',
        )


class _VaultResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        import json

        return json.dumps(self.payload).encode("utf-8")


def test_vault_secret_resolver_supports_kv_v2_without_exposing_token(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example.test:8200")
    monkeypatch.setenv("CEPH_AI_VAULT_TOKEN", "vault-token-secret")
    captured = {}

    def fake_urlopen(request, timeout, context):
        captured["url"] = request.full_url
        captured["token"] = request.get_header("X-vault-token")
        captured["timeout"] = timeout
        assert context is not None
        return _VaultResponse({"data": {"data": {"password": "directory-password"}}})

    monkeypatch.setattr(ldap_identity, "urlopen", fake_urlopen)
    assert ldap_identity.resolve_secret("vault:secret/ceph-ai/ldap#password") == "directory-password"
    assert captured == {
        "url": "https://vault.example.test:8200/v1/secret/ceph-ai/ldap",
        "token": "vault-token-secret",
        "timeout": 5,
    }


def test_vault_secret_ref_requires_https_and_field(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "http://vault.example.test:8200")
    monkeypatch.setenv("CEPH_AI_VAULT_TOKEN", "vault-token-secret")
    with pytest.raises(ldap_identity.DirectoryValidationError, match="HTTPS"):
        ldap_identity.resolve_secret("vault:secret/ceph-ai/ldap#password")
    with pytest.raises(ldap_identity.DirectoryValidationError, match="vault:.*field"):
        ldap_identity.resolve_secret("vault:secret/ceph-ai/ldap")
