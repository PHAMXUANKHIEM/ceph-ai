class NoOpRedactor:
    """v1 binding for `Redactor` (AD-6) — no transform, returns the payload
    unchanged (same object, not a copy)."""

    def redact(self, payload: dict) -> dict:
        return payload
