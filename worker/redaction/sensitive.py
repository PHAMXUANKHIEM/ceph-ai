"""Backward-compatible exports for the shared AI redaction implementation."""

from shared.ai_redaction import REDACTED, SensitiveDataRedactor, redact_text

__all__ = ["REDACTED", "SensitiveDataRedactor", "redact_text"]
