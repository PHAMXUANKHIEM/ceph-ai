"""Recursive, non-mutating redaction for data sent to AI providers."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = frozenset({
    "apikey", "accesskey", "secretkey", "clientsecret", "password", "passwd",
    "secret", "token", "authorization", "credential", "cookie", "keyring", "privatekey",
})
_TEXT_KEY = (
    r"api[_-]?key|access[_-]?key(?:[_-]?id)?|secret[_-]?access[_-]?key|"
    r"secret[_-]?key|client[_-]?secret|password|passwd|secret|(?:auth[_-]?)?token|"
    r"credential|keyring"
)
_TEXT_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL), REDACTED),
    (re.compile(r"(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*).*$"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {REDACTED}"),
    (re.compile(r"(?i)(https?://[^\s:/@]+:)[^\s/@]+(@)"), rf"\1{REDACTED}\2"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bAQ[A-Za-z0-9+/=]{20,}"), REDACTED),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b"), REDACTED),
    (re.compile(rf"(?i)([\"']?(?:{_TEXT_KEY})[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)(\bX-Amz-(?:Signature|Credential|Security-Token)=)[^&\s]+"), rf"\1{REDACTED}"),
)


def _key_is_sensitive(key: object) -> bool:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()
    compact = re.sub(r"[^a-z0-9]", "", text)
    tokens = set(filter(None, re.split(r"[^a-z0-9]+", text)))
    return bool(tokens & _SENSITIVE_KEY_PARTS) or any(marker in compact for marker in _SENSITIVE_KEY_PARTS)


def redact_text(value: str) -> str:
    for pattern, replacement in _TEXT_REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


class SensitiveDataRedactor:
    """Redact JSON-compatible payloads while preserving operational IDs."""

    def redact(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise TypeError("AI redaction payload must be a dict")
        return self._redact_value(payload)

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: REDACTED if _key_is_sensitive(key) else self._redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(item) for item in value)
        if isinstance(value, str):
            return redact_text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        raise TypeError(f"Unsupported AI payload value: {type(value).__name__}")


default_redactor = SensitiveDataRedactor()
