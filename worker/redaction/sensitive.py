"""Recursive, non-mutating redaction for payloads sent to AI providers."""

from __future__ import annotations

import re
from typing import Any


REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|"
    r"password|passwd|secret|token|authorization|credential|cookie|keyring|"
    r"private[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)

_TEXT_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTED,
    ),
    (re.compile(r"(?im)(\bauthorization\s*:\s*).*$"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {REDACTED}"),
    (
        re.compile(r"(?i)(https?://[^\s:/@]+:)[^\s/@]+(@)"),
        rf"\1{REDACTED}\2",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bAQ[A-Za-z0-9+/=]{20,}"), REDACTED),
    (
        re.compile(
            r"(?i)(\b(?:api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|"
            r"password|passwd|secret|token|credential|keyring)\b\s*[:=]\s*)"
            r"([\"']?)[^\s,;&\"']+([\"']?)"
        ),
        rf"\1\2{REDACTED}\3",
    ),
    (re.compile(r"(?i)(\bX-Amz-Signature=)[^&\s]+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)(\bX-Amz-Credential=)[^&\s]+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)(\bx-amz-security-token=)[^&\s]+"), rf"\1{REDACTED}"),
)


def redact_text(value: str) -> str:
    """Remove known secret forms embedded in otherwise useful evidence text."""
    for pattern, replacement in _TEXT_REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


class SensitiveDataRedactor:
    """Redact nested payloads without changing the caller's original object.

    Field names are the strongest signal: their entire value is removed.
    Free-form strings are also scanned because credentials commonly appear in
    log excerpts, command output, URLs, and serialized configuration snippets.
    Hostnames and IP addresses are deliberately retained as Ceph RCA evidence.
    """

    def redact(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise TypeError("AI redaction payload must be a dict")
        return self._redact_value(payload)

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if _SENSITIVE_KEY.search(str(key)):
                    result[key] = REDACTED
                else:
                    result[key] = self._redact_value(item)
            return result
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(item) for item in value)
        if isinstance(value, str):
            return redact_text(value)
        return value
