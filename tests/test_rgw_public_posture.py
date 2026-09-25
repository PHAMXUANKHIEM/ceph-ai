"""Public-access evidence remains conservative and cluster-scoped."""

from watcher.rgw_public_posture import assess_public_access


def test_public_policy_and_acl_are_reported_without_raw_documents():
    policy = {"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject"}]}
    acl = {"Grants": [{"Grantee": {"URI": "http://acs.amazonaws.com/groups/global/AllUsers"},
                       "Permission": "READ"}]}
    result = assess_public_access(policy, acl, policy_available=True, acl_available=True)
    assert result["status"] == "public_grant"
    assert {item["source"] for item in result["evidence"]} == {"bucket_policy", "bucket_acl"}
    assert result["effective_access_verified"] is False
    assert "Principal" not in str(result)


def test_missing_or_invalid_evidence_never_claims_private():
    missing = assess_public_access(None, None, policy_available=False, acl_available=False)
    malformed = assess_public_access("{bad", {"Grants": []}, policy_available=True, acl_available=True)
    assert missing["status"] == "unknown"
    assert malformed["status"] == "unknown"
    assert missing["evidence_gaps"] and malformed["evidence_gaps"]


def test_private_acl_without_policy_is_only_no_grant_observed():
    result = assess_public_access(None, {"Grants": []}, policy_available=True, acl_available=True)
    assert result["status"] == "no_public_grant_observed"
    assert result["effective_access_verified"] is False


def test_conditions_are_not_mistaken_for_unconditional_access():
    policy = {"Statement": [{"Effect": "Allow", "Principal": {"AWS": "*"},
                              "Condition": {"IpAddress": {"aws:SourceIp": "192.0.2.0/24"}}}]}
    result = assess_public_access(policy, {"Grants": []}, policy_available=True, acl_available=True)
    assert result["status"] == "public_grant"
    assert result["evidence"][0]["scope"] == "conditional"


def test_oversized_policy_and_acl_fail_closed():
    result = assess_public_access("x" * (128 * 1024 + 1), {"Grants": [{}] * 501},
                                  policy_available=True, acl_available=True)
    assert result["status"] == "unknown"
    assert len(result["evidence_gaps"]) == 2
