"""Conservative, secret-free assessment of S3 bucket public grants."""

from __future__ import annotations

import json

_PUBLIC_GROUPS = (
    "http://acs.amazonaws.com/groups/global/AllUsers",
    "http://acs.amazonaws.com/groups/global/AuthenticatedUsers",
)


def assess_public_access(policy: object, acl: object, *, policy_available: bool, acl_available: bool) -> dict:
    """Report grants, not effective access: explicit denies and RGW settings can override them."""
    evidence: list[dict[str, str]] = []
    gaps: list[str] = []
    if policy_available and policy is not None:
        try:
            if isinstance(policy, str) and len(policy) > 128 * 1024:
                raise ValueError("policy exceeds assessment limit")
            parsed = json.loads(policy) if isinstance(policy, str) else policy
            if not isinstance(parsed, dict) or "Statement" not in parsed:
                raise ValueError("policy is not an object")
            statements = parsed.get("Statement", [])
            if isinstance(statements, dict):
                statements = [statements]
            if not isinstance(statements, list) or len(statements) > 200 or any(
                not isinstance(statement, dict) for statement in statements
            ):
                raise ValueError("invalid statement list")
            for statement in statements:
                if statement.get("Effect") != "Allow":
                    continue
                principal = statement.get("Principal")
                public = principal == "*" or (
                    isinstance(principal, dict) and any(
                        value == "*" or (isinstance(value, list) and "*" in value)
                        for value in principal.values()
                    )
                )
                if public:
                    evidence.append({
                        "source": "bucket_policy",
                        "scope": "conditional" if statement.get("Condition") else "public_grant",
                        "action": str(statement.get("Action", "unknown"))[:160],
                    })
        except (TypeError, ValueError):
            gaps.append("Không phân tích được Bucket Policy; cần kiểm tra thủ công.")
    elif not policy_available:
        gaps.append("Chưa đọc được Bucket Policy.")

    if acl_available and isinstance(acl, dict):
        grants = acl.get("Grants")
        if isinstance(grants, list) and len(grants) <= 500:
            for grant in grants:
                if not isinstance(grant, dict):
                    gaps.append("Có grant ACL không hợp lệ; cần kiểm tra thủ công.")
                    continue
                grantee = grant.get("Grantee") or {}
                if not isinstance(grantee, dict):
                    gaps.append("Có grantee ACL không hợp lệ; cần kiểm tra thủ công.")
                    continue
                if isinstance(grantee, dict) and grantee.get("URI") in _PUBLIC_GROUPS:
                    evidence.append({
                        "source": "bucket_acl",
                        "scope": "public_grant" if grantee["URI"] == _PUBLIC_GROUPS[0] else "authenticated_users",
                        "permission": str(grant.get("Permission", "unknown"))[:32],
                    })
        else:
            gaps.append("Danh sách ACL không hợp lệ hoặc vượt giới hạn.")
    else:
        gaps.append("Chưa đọc được Bucket ACL.")

    status = "public_grant" if evidence else ("unknown" if gaps else "no_public_grant_observed")
    return {
        "status": status,
        "evidence": evidence[:50],
        "evidence_gaps": gaps,
        "effective_access_verified": False,
        "read_only": True,
        "action_id": None,
    }
