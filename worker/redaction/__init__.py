from worker.redaction.base import Redactor
from worker.redaction.noop import NoOpRedactor

# Single binding point (AD-6): Story 2.3's Claude call site imports and calls
# `default_redactor.redact(payload)` exactly once, right before sending to
# the Claude API. Swapping in a real redactor later means changing only this
# line — never the call site.
default_redactor: Redactor = NoOpRedactor()

__all__ = ["Redactor", "NoOpRedactor", "default_redactor"]
