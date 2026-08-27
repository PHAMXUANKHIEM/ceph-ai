from worker.redaction.base import Redactor
from worker.redaction.noop import NoOpRedactor
from shared.ai_redaction import REDACTED, SensitiveDataRedactor, default_redactor, redact_text

# Production binding used immediately before incident and backup payloads are
# sent to any configured AI provider. NoOpRedactor remains available only for
# explicitly isolated tests/development code.

__all__ = [
    "REDACTED",
    "Redactor",
    "NoOpRedactor",
    "SensitiveDataRedactor",
    "default_redactor",
    "redact_text",
]
